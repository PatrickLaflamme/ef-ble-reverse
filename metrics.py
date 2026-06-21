"""Energy/usage metrics persistence for the parallel controller.

SQLite-backed running accumulators, bucketed per unit per UTC day, so the
dashboard can show "today" and "all-time" without unbounded storage growth.

Each heartbeat interval contributes power x dt to energy counters (Wh) and dt
to charge/discharge time. Gaps larger than MAX_GAP_S (restart, stall) are
skipped so we never integrate across a blackout in the data.
"""

import sqlite3
from datetime import datetime, timezone

MAX_GAP_S = 120  # ignore intervals longer than this (restart / dropped link)


def _day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class Metrics:
    def __init__(self, path, max_gap_s=MAX_GAP_S):
        self._max_gap_s = max_gap_s
        # check_same_thread=False: the asyncio loop touches it from callbacks.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS energy(
                   unit TEXT NOT NULL,
                   day  TEXT NOT NULL,
                   charge_s    REAL NOT NULL DEFAULT 0,
                   discharge_s REAL NOT NULL DEFAULT 0,
                   in_wh    REAL NOT NULL DEFAULT 0,
                   out_wh   REAL NOT NULL DEFAULT 0,
                   solar_wh REAL NOT NULL DEFAULT 0,
                   grid_wh  REAL NOT NULL DEFAULT 0,
                   PRIMARY KEY (unit, day))""")
        self._db.commit()
        self._last_ts = {}  # unit -> last sample timestamp

    def record(self, unit, ts, charging, watts_in, watts_out, solar_w, grid_w):
        """Integrate one heartbeat sample for a unit. Returns the dt used (or 0)."""
        last = self._last_ts.get(unit)
        self._last_ts[unit] = ts
        if last is None:
            return 0.0
        dt = ts - last
        if dt <= 0 or dt > self._max_gap_s:
            return 0.0  # skip gaps / out-of-order samples
        h = dt / 3600.0
        day = _day(ts)
        self._db.execute(
            "INSERT OR IGNORE INTO energy(unit, day) VALUES (?, ?)", (unit, day))
        self._db.execute(
            """UPDATE energy SET
                   charge_s    = charge_s    + ?,
                   discharge_s = discharge_s + ?,
                   in_wh       = in_wh       + ?,
                   out_wh      = out_wh      + ?,
                   solar_wh    = solar_wh    + ?,
                   grid_wh     = grid_wh     + ?
               WHERE unit = ? AND day = ?""",
            (dt if charging else 0.0,
             0.0 if charging else dt,
             (watts_in or 0.0) * h,
             (watts_out or 0.0) * h,
             (solar_w or 0.0) * h,
             (grid_w or 0.0) * h,
             unit, day))
        self._db.commit()
        return dt

    def _rows(self, where="", params=()):
        q = ("SELECT unit, SUM(charge_s), SUM(discharge_s), SUM(in_wh), "
             "SUM(out_wh), SUM(solar_wh), SUM(grid_wh) FROM energy "
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
                # in - out = energy stored in batteries + conversion/standby loss
                "delta_wh": round((r[3] or 0) - (r[4] or 0), 1),
            }
        return out

    def summary(self, now_ts=None):
        """Per-unit all-time and today totals (+ system totals)."""
        today = _day(now_ts) if now_ts is not None \
            else datetime.now(timezone.utc).strftime("%Y-%m-%d")

        def totals(rows):
            t = {k: 0.0 for k in
                 ("charge_s", "discharge_s", "in_wh", "out_wh", "solar_wh",
                  "grid_wh", "delta_wh")}
            for u in rows.values():
                for k in t:
                    t[k] += u[k]
            return {k: round(v, 1) for k, v in t.items()}

        all_rows = self._rows()
        today_rows = self._rows("WHERE day = ?", (today,))
        return {
            "all_time": {"per_unit": all_rows, "totals": totals(all_rows)},
            "today": {"per_unit": today_rows, "totals": totals(today_rows)},
        }

    def close(self):
        self._db.close()
