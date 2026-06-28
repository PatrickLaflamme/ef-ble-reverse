"""Energy/usage metrics persistence for the parallel controller.

Two tables in one SQLite db:
  * energy  - running accumulators bucketed per unit per UTC day (totals).
  * samples - downsampled time series (one row/unit/~minute) for charts,
              pruned to RETENTION_S.

Each heartbeat interval contributes power x dt to energy counters (Wh) and dt
to charge/discharge time. With SoC + battery capacity we also estimate true
loss = energy_in - energy_out - delta_stored, where delta_stored is the
change in stored energy (delta_SoC/100 * capacity). Summed over an interval
this telescopes to in - out - stored over any window. Gaps > MAX_GAP_S
(restart, stall) are skipped so we never integrate across a data blackout.
"""

import sqlite3
from datetime import datetime, timezone

MAX_GAP_S = 120          # ignore intervals longer than this
SAMPLE_INTERVAL_S = 60   # store at most one time-series row per unit per minute
RETENTION_S = 14 * 86400  # keep ~2 weeks of samples


def _day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class Metrics:
    def __init__(self, path, max_gap_s=MAX_GAP_S):
        self._max_gap_s = max_gap_s
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS energy(
                   unit TEXT NOT NULL, day TEXT NOT NULL,
                   charge_s    REAL NOT NULL DEFAULT 0,
                   discharge_s REAL NOT NULL DEFAULT 0,
                   in_wh    REAL NOT NULL DEFAULT 0,
                   out_wh   REAL NOT NULL DEFAULT 0,
                   solar_wh REAL NOT NULL DEFAULT 0,
                   grid_wh  REAL NOT NULL DEFAULT 0,
                   loss_wh  REAL NOT NULL DEFAULT 0,
                   PRIMARY KEY (unit, day))""")
        # migrate older dbs that predate loss_wh
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(energy)")]
        if "loss_wh" not in cols:
            self._db.execute("ALTER TABLE energy ADD COLUMN loss_wh REAL NOT NULL DEFAULT 0")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS samples(
                   unit TEXT NOT NULL, ts REAL NOT NULL,
                   soc REAL, watts_in REAL, watts_out REAL,
                   solar_w REAL, grid_w REAL, rssi REAL)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts)")
        # migrate older dbs that predate rssi
        scols = [r[1] for r in self._db.execute("PRAGMA table_info(samples)")]
        if "rssi" not in scols:
            self._db.execute("ALTER TABLE samples ADD COLUMN rssi REAL")
        # BLE connection lifecycle events for drop-rate / uptime stats.
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS conn_events(
                   unit TEXT NOT NULL, ts REAL NOT NULL, event TEXT NOT NULL)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS conn_events_ts ON conn_events(ts)")
        self._db.commit()
        self._last_ts = {}      # unit -> last sample timestamp
        self._last_soc = {}     # unit -> last soc
        self._last_sample = {}  # unit -> last time-series row timestamp

    def _maybe_sample(self, unit, ts, soc, watts_in, watts_out, solar_w, grid_w,
                      rssi=None):
        last = self._last_sample.get(unit)
        if last is not None and (ts - last) < SAMPLE_INTERVAL_S:
            return
        self._last_sample[unit] = ts
        self._db.execute(
            "INSERT INTO samples(unit, ts, soc, watts_in, watts_out, solar_w, grid_w, rssi) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (unit, ts, soc, watts_in, watts_out, solar_w, grid_w, rssi))
        self._db.execute("DELETE FROM samples WHERE ts < ?", (ts - RETENTION_S,))

    def record_event(self, unit, ts, event):
        """Log a BLE lifecycle event ('connect'/'disconnect'/'reconnect_fail')."""
        self._db.execute(
            "INSERT INTO conn_events(unit, ts, event) VALUES (?,?,?)",
            (unit, ts, event))
        self._db.execute("DELETE FROM conn_events WHERE ts < ?", (ts - RETENTION_S,))
        self._db.commit()

    def record(self, unit, ts, charging, watts_in, watts_out, solar_w, grid_w,
               soc=None, cap_wh=None, rssi=None):
        """Integrate one heartbeat sample for a unit. Returns the dt used (or 0)."""
        last = self._last_ts.get(unit)
        last_soc = self._last_soc.get(unit)
        self._last_ts[unit] = ts
        self._last_soc[unit] = soc

        self._maybe_sample(unit, ts, soc, watts_in, watts_out, solar_w, grid_w, rssi)

        if last is None:
            self._db.commit()
            return 0.0
        dt = ts - last
        if dt <= 0 or dt > self._max_gap_s:
            self._db.commit()
            return 0.0  # skip gaps / out-of-order (baseline already advanced)

        h = dt / 3600.0
        in_wh = (watts_in or 0.0) * h
        out_wh = (watts_out or 0.0) * h
        solar_wh = (solar_w or 0.0) * h
        grid_wh = (grid_w or 0.0) * h

        loss_wh = 0.0
        if soc is not None and last_soc is not None and cap_wh:
            delta_stored = (soc - last_soc) / 100.0 * cap_wh
            loss_wh = in_wh - out_wh - delta_stored

        day = _day(ts)
        self._db.execute(
            "INSERT OR IGNORE INTO energy(unit, day) VALUES (?, ?)", (unit, day))
        self._db.execute(
            """UPDATE energy SET
                   charge_s = charge_s + ?, discharge_s = discharge_s + ?,
                   in_wh = in_wh + ?, out_wh = out_wh + ?,
                   solar_wh = solar_wh + ?, grid_wh = grid_wh + ?,
                   loss_wh = loss_wh + ?
               WHERE unit = ? AND day = ?""",
            (dt if charging else 0.0, 0.0 if charging else dt,
             in_wh, out_wh, solar_wh, grid_wh, loss_wh, unit, day))
        self._db.commit()
        return dt

    def _rows(self, where="", params=()):
        q = ("SELECT unit, SUM(charge_s), SUM(discharge_s), SUM(in_wh), "
             "SUM(out_wh), SUM(solar_wh), SUM(grid_wh), SUM(loss_wh) FROM energy "
             + where + " GROUP BY unit")
        out = {}
        for r in self._db.execute(q, params):
            out[r[0]] = {
                "charge_s": round(r[1] or 0, 1),
                "discharge_s": round(r[2] or 0, 1),
                "in_wh": round(r[3] or 0, 1),
                "out_wh": round(r[4] or 0, 1),
                "solar_wh": round(r[5] or 0, 1),
                "grid_wh": round(r[6] or 0, 1),
                "delta_wh": round((r[3] or 0) - (r[4] or 0), 1),
                "loss_wh": round(r[7] or 0, 1),
            }
        return out

    def summary(self, now_ts=None):
        today = _day(now_ts) if now_ts is not None \
            else datetime.now(timezone.utc).strftime("%Y-%m-%d")

        def totals(rows):
            keys = ("charge_s", "discharge_s", "in_wh", "out_wh", "solar_wh",
                    "grid_wh", "delta_wh", "loss_wh")
            t = {k: 0.0 for k in keys}
            for u in rows.values():
                for k in keys:
                    t[k] += u[k]
            return {k: round(v, 1) for k, v in t.items()}

        all_rows = self._rows()
        today_rows = self._rows("WHERE day = ?", (today,))
        return {
            "all_time": {"per_unit": all_rows, "totals": totals(all_rows)},
            "today": {"per_unit": today_rows, "totals": totals(today_rows)},
        }

    def history(self, hours=24, now_ts=None):
        """Time-series samples within the window, grouped by unit."""
        import time as _time
        now = now_ts if now_ts is not None else _time.time()
        cutoff = now - hours * 3600
        out = {}
        for r in self._db.execute(
                "SELECT unit, ts, soc, watts_in, watts_out, solar_w, grid_w, rssi "
                "FROM samples WHERE ts >= ? ORDER BY ts", (cutoff,)):
            out.setdefault(r[0], []).append({
                "ts": r[1], "soc": r[2], "watts_in": r[3], "watts_out": r[4],
                "solar_w": r[5], "grid_w": r[6], "rssi": r[7],
            })
        return out

    def health(self, now_ts=None, windows=(3600, 86400)):
        """Per-unit connection health: drop counts per window, reconnect
        failures, and the most recent RSSI sample. Units are any seen in
        either conn_events or samples."""
        import time as _time
        now = now_ts if now_ts is not None else _time.time()

        units = set()
        for r in self._db.execute("SELECT DISTINCT unit FROM conn_events"):
            units.add(r[0])
        for r in self._db.execute("SELECT DISTINCT unit FROM samples"):
            units.add(r[0])

        out = {}
        for u in units:
            drops = {}
            rfails = {}
            for w in windows:
                cutoff = now - w
                label = "%dh" % (w // 3600)
                drops[label] = self._db.execute(
                    "SELECT COUNT(*) FROM conn_events "
                    "WHERE unit=? AND event='disconnect' AND ts>=?",
                    (u, cutoff)).fetchone()[0]
                rfails[label] = self._db.execute(
                    "SELECT COUNT(*) FROM conn_events "
                    "WHERE unit=? AND event='reconnect_fail' AND ts>=?",
                    (u, cutoff)).fetchone()[0]
            row = self._db.execute(
                "SELECT rssi, ts FROM samples "
                "WHERE unit=? AND rssi IS NOT NULL ORDER BY ts DESC LIMIT 1",
                (u,)).fetchone()
            out[u] = {
                "drops": drops,
                "reconnect_fails": rfails,
                "last_rssi": row[0] if row else None,
                "last_rssi_ts": row[1] if row else None,
            }
        return out

    def close(self):
        self._db.close()
