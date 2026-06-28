"""Two-DPU parallel-discharge / charge-rotation controller + web portal.

Holds a BLE connection to each of two EcoFlow DPU units, reads each unit's SoC
from its heartbeat, rotates which unit charges according to policy.py, and
serves a status/control portal over HTTP (aiohttp) on the same event loop.

ON  = attached to parallel group, discharging (PrStateSet set_self=1)
OFF = detached, charging                       (PrStateSet set_self=0)

Make-before-break: a unit is only detached after the other is attached, so the
load never sees zero units in the parallel group.

Config (env vars, with CLI fallback):
    ECOFLOW_USER_ID         account user id (required)
    ECOFLOW_ADDR_A          BLE address of unit A (required)
    ECOFLOW_ADDR_B          BLE address of unit B (required)
    ECOFLOW_HTTP_HOST       portal bind host (default 127.0.0.1)
    ECOFLOW_HTTP_PORT       portal bind port (default 8787)

CLI fallback:
    python controller.py <user_id> <address_A> <address_B>
"""

import asyncio
import collections
import os
import signal
import sys
import time

from bleak import BleakScanner
from bleak_retry_connector import close_stale_connections_by_address

import connect
from connect import located_devices, discoveryCallback
import policy
from metrics import Metrics
import betterstack

# Seconds to wait after attaching a unit before detaching the other, so the
# newly-attached unit is carrying load before the break.
MAKE_SETTLE_SECONDS = 5
SCAN_SECONDS = 6.0

# SoC-rate estimation for charge/discharge ETAs.
RATE_WINDOW_S = 1800      # only use SoC samples from this trailing window
RATE_MIN_SPAN_S = 300     # need >= this much time span for a confident rate
SOC_HIST_MAXLEN = 720

# Per-battery-pack energy (Wh) for the loss estimate; DPU packs are ~6.14 kWh.
PACK_WH = int(os.environ.get("ECOFLOW_PACK_WH", "6144"))

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class ParallelController:
    def __init__(self, metrics=None, betterstack=None):
        self._conns = {}        # unit_key -> Connection
        self._actual_on = {}    # unit_key -> bool (last commanded ON state)
        self._last_seen = {}    # unit_key -> epoch of last heartbeat
        self._charging = None   # unit_key currently charging, or None
        self._initialized = False
        self._auto_enabled = True
        self._soc_hist = {}     # unit_key -> deque[(ts, soc)] for rate/ETA
        self._metrics = metrics  # optional Metrics() for energy/usage logging
        self._bs = betterstack   # optional BetterStackEmitter for remote metrics
        self._lock = asyncio.Lock()

    def add(self, unit_key, conn):
        self._conns[unit_key] = conn
        self._actual_on[unit_key] = True  # provisional; first eval forces real state
        conn.set_heartbeat_callback(self._on_heartbeat)
        conn.set_event_callback(self._on_conn_event)

    def _on_conn_event(self, conn, event):
        """BLE lifecycle event from a Connection ('connect'/'disconnect'/
        'reconnect_fail'); persist it for drop-rate metrics."""
        now = time.time()
        print("CTRL: unit %s BLE event: %s" % (conn.sn, event))
        if self._metrics is not None:
            self._metrics.record_event(conn.sn, now, event)
        if self._bs is not None:
            self._bs.emit_event(conn.sn, now, event)

    async def _on_heartbeat(self, conn):
        now = time.time()
        self._last_seen[conn.sn] = now
        if conn.last_soc is not None:
            h = self._soc_hist.setdefault(conn.sn, collections.deque(maxlen=SOC_HIST_MAXLEN))
            h.append((now, conn.last_soc))
            # Record energy/usage for the interval that just ended. The charging
            # flag reflects the state held DURING the interval (state only changes
            # in evaluate(), below), so attribution is correct.
            if self._metrics is not None:
                p = conn._last_power or {}
                charging = not self._actual_on.get(conn.sn, True)
                bp = p.get("bp_num")
                cap_wh = bp * PACK_WH if bp else None
                self._metrics.record(conn.sn, now, charging,
                                     p.get("watts_in"), p.get("watts_out"),
                                     p.get("solar"), p.get("grid"),
                                     soc=conn.last_soc, cap_wh=cap_wh,
                                     rssi=getattr(conn, "_rssi", None))
            if self._bs is not None:
                p = conn._last_power or {}
                self._bs.maybe_emit_sample(conn.sn, now, {
                    "soc": conn.last_soc,
                    "watts_in": p.get("watts_in"),
                    "watts_out": p.get("watts_out"),
                    "solar_w": p.get("solar"),
                    "grid_w": p.get("grid"),
                    "rssi": getattr(conn, "_rssi", None),
                })
        await self.evaluate()

    def _rate_pct_per_hr(self, unit_key, now):
        """Least-squares SoC slope (%/hr) over the recent window, or None."""
        h = self._soc_hist.get(unit_key)
        if not h:
            return None
        pts = [(t, s) for (t, s) in h if now - t <= RATE_WINDOW_S]
        if len(pts) < 2 or (pts[-1][0] - pts[0][0]) < RATE_MIN_SPAN_S:
            return None
        n = len(pts)
        tm = sum(t for t, _ in pts) / n
        sm = sum(s for _, s in pts) / n
        denom = sum((t - tm) ** 2 for t, _ in pts)
        if denom == 0:
            return None
        slope = sum((t - tm) * (s - sm) for t, s in pts) / denom  # %/s
        return slope * 3600.0

    def _soc_rate(self, unit_key, now, power, cap_wh):
        """SoC rate in %/hr. Prefer net power / capacity (instant, not
        quantized); fall back to the observed SoC slope when power/capacity
        aren't available."""
        if power and cap_wh:
            net = (power.get("watts_in") or 0.0) - (power.get("watts_out") or 0.0)
            if abs(net) >= 1.0:  # ignore idle noise
                return net / cap_wh * 100.0  # +rising / -falling
        return self._rate_pct_per_hr(unit_key, now)

    def _eta(self, unit_key, soc, is_charging, now, power=None, cap_wh=None):
        """Project time to the next state change for one unit, or None."""
        rate = self._soc_rate(unit_key, now, power, cap_wh)
        if rate is None or soc is None:
            return None
        if is_charging:
            target = policy.TARGET_SOC
            if rate <= 0 or soc >= target:
                return None  # not actually rising / already there
            secs = (target - soc) / rate * 3600.0
            kind = "reactivate"
        else:
            target = policy.FLOOR_SOC
            if rate >= 0 or soc <= target:
                return None  # not actually falling / already at floor
            secs = (soc - target) / (-rate) * 3600.0
            kind = "needs_charge"
        return {"kind": kind, "target": target, "seconds": int(secs),
                "ts": now + secs, "rate_pct_per_hr": round(rate, 2)}

    def _soc_map(self):
        return {k: c.last_soc for k, c in self._conns.items() if c.last_soc is not None}

    async def _apply(self, unit_key, want_on):
        await self._conns[unit_key].setParallelBox(set_self=1 if want_on else 0)
        self._actual_on[unit_key] = want_on
        # Mode boundary: discard SoC history so the rate/ETA re-estimates fresh
        # for the new charge/discharge regime instead of mixing both sides.
        if unit_key in self._soc_hist:
            self._soc_hist[unit_key].clear()
        print("CTRL: unit %s -> %s" % (unit_key, "ON" if want_on else "OFF(charging)"))

    def _seed_initial_state(self):
        """On first run, infer each unit's ON/OFF state and any in-progress
        charger from live telemetry instead of assuming both ON.

        access_5p8_out_type: 1 = attached to parallel box (ON/discharging),
        0 = detached (OFF/charging). Seeding from this means we only send the
        commands actually needed (no needless toggling if already in the right
        state) and we preserve a charge already in progress across a restart.
        """
        if self._initialized:
            return
        detached = []
        for u in self._conns:
            a = self._conns[u]._last_access_5p8_out_type
            if a is None:
                continue  # no telemetry yet for this unit; keep default (ON)
            self._actual_on[u] = (a == 1)
            if a == 0:
                detached.append(u)
        # Exactly one detached unit is the current charger: adopt it as
        # self._charging so the charge-to-TARGET hysteresis continues it instead
        # of yanking it back to discharge on the next evaluation.
        if len(detached) == 1:
            self._charging = detached[0]
        self._initialized = True
        print("CTRL: seeded initial state from telemetry: actual_on=%s charging=%s"
              % (self._actual_on, self._charging))

    async def _transition_to(self, charging_unit):
        """Drive the system to the given charging target. Assumes lock held."""
        units = list(self._conns)
        actions = policy.plan_transitions(charging_unit, units, self._actual_on)
        if not actions:
            self._charging = charging_unit
            return
        print("CTRL: charging %s->%s actions=%s" % (self._charging, charging_unit, actions))
        for i, (unit_key, want_on) in enumerate(actions):
            # settle between a make (prev ON) and this break (OFF)
            if i > 0 and actions[i - 1][1] is True and want_on is False:
                await asyncio.sleep(MAKE_SETTLE_SECONDS)
            await self._apply(unit_key, want_on)
        self._charging = charging_unit

    async def evaluate(self):
        async with self._lock:
            if not self._auto_enabled:
                return  # manual mode: don't auto-act
            soc = self._soc_map()
            if len(soc) < len(self._conns):
                return  # wait until both units have reported SoC
            self._seed_initial_state()
            charging_unit = policy.decide_charging_unit(soc, self._charging)
            await self._transition_to(charging_unit)

    async def set_manual(self, charging_unit):
        """Manually force a charging target ('A'/'B' key) or None for both ON.

        Disables automation until resume_auto() is called.
        """
        async with self._lock:
            if charging_unit is not None and charging_unit not in self._conns:
                raise ValueError("unknown unit %r" % charging_unit)
            self._seed_initial_state()
            self._auto_enabled = False
            await self._transition_to(charging_unit)

    async def resume_auto(self):
        """Re-enable automation and immediately re-evaluate."""
        async with self._lock:
            self._auto_enabled = True
        await self.evaluate()

    def status(self):
        now = time.time()
        units = []
        for k, c in self._conns.items():
            is_charging = (self._charging == k)
            power = getattr(c, "_last_power", None) or {}
            bp = power.get("bp_num")
            cap_wh = bp * PACK_WH if bp else None
            eta = self._eta(k, c.last_soc, is_charging, now, power, cap_wh)
            rate = self._soc_rate(k, now, power, cap_wh)
            units.append({
                "key": k,
                "soc": c.last_soc,
                "on": self._actual_on.get(k),
                "charging": is_charging,
                "watts_in": power.get("watts_in"),
                "watts_out": power.get("watts_out"),
                "solar_w": power.get("solar"),
                "rssi": getattr(c, "_rssi", None),
                "rate_pct_per_hr": round(rate, 2) if rate is not None else None,
                "eta": eta,
                "show_flag": c._last_show_flag,
                "access_5p8_out_type": c._last_access_5p8_out_type,
                "age_s": (round(now - self._last_seen[k], 1)
                          if k in self._last_seen else None),
            })
        return {
            "automation_enabled": self._auto_enabled,
            "initialized": self._initialized,
            "charging_unit": self._charging,
            "in_window": policy.in_window(),
            "units": units,
            "policy": {
                "critical_soc": policy.CRITICAL_SOC,
                "floor_soc": policy.FLOOR_SOC,
                "early_soc": policy.EARLY_SOC,
                "early_both_below": policy.EARLY_BOTH_BELOW,
                "window_soc": policy.WINDOW_SOC,
                "target_soc": policy.TARGET_SOC,
                "window_start": policy.WINDOW_START.strftime("%H:%M"),
                "window_end": policy.WINDOW_END.strftime("%H:%M"),
            },
            "settle_seconds": MAKE_SETTLE_SECONDS,
            "ts": now,
        }


# --- Better Stack heartbeat watchdog ------------------------------------------
# Dead-man's switch: while a unit's BLE link is fresh we ping its Better Stack
# heartbeat URL; if the link goes stale we WITHHOLD the ping, so Better Stack's
# grace window lapses and it pages us — naming the specific unit. Controller
# death stops all pings, so that's covered too. Configure each Better Stack
# heartbeat for ~30s period + ~30s grace to alert at ~60s of BT silence.
WATCHDOG_TICK_S = int(os.environ.get("WATCHDOG_TICK_S", "15"))
HEARTBEAT_FRESH_S = int(os.environ.get("HEARTBEAT_FRESH_S", "30"))


async def _ping(session, url, label):
    """GET a heartbeat URL; never raise (a failed ping must not kill the loop).
    The URL is a secret, so only the label is logged."""
    try:
        async with session.get(url) as resp:
            await resp.read()
    except Exception as e:
        print("WARN: heartbeat ping (%s) failed: %s" % (label, e))


async def _watchdog_tick(now, ctrl, unit_urls, fresh_s, stale, ping):
    """One watchdog pass: ping each fresh unit's heartbeat, withhold for stale
    ones, and log stale<->fresh transitions. `ping(label, url)` is awaited for
    each unit that should ping. Pure of network/sleep so it's unit-testable."""
    for key, url in unit_urls.items():
        seen = ctrl._last_seen.get(key)
        age = (now - seen) if seen is not None else None
        fresh = age is not None and age < fresh_s
        if fresh:
            await ping(key, url)
            if stale[key]:
                print("CTRL: unit %s BT link recovered (age %.0fs); "
                      "resuming heartbeat" % (key, age))
                stale[key] = False
        elif not stale[key]:
            age_str = "%.0fs" % age if age is not None else "never seen"
            print("CTRL: unit %s BT link STALE (age %s); withholding "
                  "Better Stack heartbeat — it will page" % (key, age_str))
            stale[key] = True


async def heartbeat_watchdog(ctrl, unit_urls, controller_url=None,
                             tick_s=WATCHDOG_TICK_S, fresh_s=HEARTBEAT_FRESH_S):
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=10)
    stale = {k: False for k in unit_urls}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async def ping(label, url):
            await _ping(session, url, label)
        while True:
            now = time.time()
            if controller_url:
                await _ping(session, controller_url, "controller")
            await _watchdog_tick(now, ctrl, unit_urls, fresh_s, stale, ping)
            await asyncio.sleep(tick_s)


# --- web portal ---------------------------------------------------------------
def make_web_app(ctrl):
    from aiohttp import web

    async def get_status(request):
        return web.json_response(ctrl.status())

    async def post_control(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        action = body.get("action")
        try:
            if action == "auto":
                await ctrl.resume_auto()
            elif action == "both_on":
                await ctrl.set_manual(None)
            elif action == "charge":
                await ctrl.set_manual(body.get("unit"))
            else:
                return web.json_response({"error": "unknown action"}, status=400)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(ctrl.status())

    async def get_metrics(request):
        if ctrl._metrics is None:
            return web.json_response({"error": "metrics disabled"}, status=404)
        return web.json_response(ctrl._metrics.summary())

    async def get_history(request):
        if ctrl._metrics is None:
            return web.json_response({"error": "metrics disabled"}, status=404)
        try:
            hours = float(request.query.get("hours", "24"))
        except ValueError:
            hours = 24
        return web.json_response(ctrl._metrics.history(hours=hours))

    async def get_health(request):
        """Live per-unit connection health: staleness + RSSI + drop stats.
        Merges live state (age_s, stale, rssi) with persisted drop counts."""
        now = time.time()
        persisted = ctrl._metrics.health(now_ts=now) if ctrl._metrics else {}
        units = []
        for k, c in ctrl._conns.items():
            seen = ctrl._last_seen.get(k)
            age = round(now - seen, 1) if seen is not None else None
            h = persisted.get(k, {})
            units.append({
                "key": k,
                "age_s": age,
                "stale": age is None or age >= HEARTBEAT_FRESH_S,
                "rssi": getattr(c, "_rssi", None),
                "drops": h.get("drops"),
                "reconnect_fails": h.get("reconnect_fails"),
            })
        return web.json_response({
            "units": units, "fresh_threshold_s": HEARTBEAT_FRESH_S, "ts": now})

    async def index(request):
        path = os.path.join(WEB_DIR, "index.html")
        if os.path.exists(path):
            return web.FileResponse(path)
        return web.Response(text="portal frontend missing", status=500)

    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/api/status", get_status),
        web.get("/api/metrics", get_metrics),
        web.get("/api/history", get_history),
        web.get("/api/health", get_health),
        web.post("/api/control", post_control),
    ])
    if os.path.isdir(WEB_DIR):
        app.router.add_static("/static/", WEB_DIR)
    return app


async def start_web(ctrl, host, port):
    from aiohttp import web
    app = make_web_app(ctrl)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print("INFO: portal listening on http://%s:%d" % (host, port))
    return runner


def _db_path():
    """Where to persist metrics. systemd StateDirectory=ecoflow sets
    STATE_DIRECTORY=/var/lib/ecoflow; fall back to ECOFLOW_DB, then cwd."""
    if os.environ.get("ECOFLOW_DB"):
        return os.environ["ECOFLOW_DB"]
    state = os.environ.get("STATE_DIRECTORY")
    if state:
        return os.path.join(state.split(":")[0], "metrics.db")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "metrics.db")


async def main(user_id, addresses, http_host="127.0.0.1", http_port=8787):
    connect.USER_ID = user_id
    connect.CMD = None  # we drive commands ourselves, not the one-shot CLI hook

    # On an unclean restart (SIGKILL / crash) BlueZ can still hold a connection
    # to a target device. A connected peripheral does not advertise, so the scan
    # below would miss it and we'd fail to start. Proactively drop any stale link
    # so the device advertises again and we get a clean session.
    for addr in addresses:
        try:
            await close_stale_connections_by_address(addr.upper())
            print("INFO: cleared any stale BT connection to %s" % addr.upper())
        except Exception as e:
            print("WARN: close_stale_connections(%s): %s" % (addr.upper(), e))

    scanner = BleakScanner(discoveryCallback)
    print("INFO: scanning for %d device(s)..." % len(addresses))
    async with scanner:
        await asyncio.sleep(SCAN_SECONDS)

    devices = []
    for i, addr in enumerate(addresses):
        a = addr.upper()
        if a not in located_devices:
            print("ERROR: device %s not found in scan (found: %s)" % (
                a, list(located_devices)))
            return
        dev = located_devices[a]
        key = dev._sn or ("unit%d" % i)
        devices.append((key, dev))

    db_path = _db_path()
    print("INFO: metrics db: %s" % db_path)
    bs = betterstack.from_env(os.environ)
    print("INFO: Better Stack metrics %s" %
          ("enabled" if bs else "disabled (no BETTERSTACK_SOURCE_URL/TOKEN)"))
    ctrl = ParallelController(metrics=Metrics(db_path), betterstack=bs)
    await start_web(ctrl, http_host, http_port)

    print("INFO: connecting to: %s" % [k for k, _ in devices])
    for key, dev in devices:
        await dev.connect()
        ctrl.add(key, dev._conn)

    # Better Stack heartbeat watchdog. Map the positional BETTERSTACK_HEARTBEAT_A/B
    # env URLs onto the units in the same order as ECOFLOW_ADDR_A/B.
    hb_env = [os.environ.get("BETTERSTACK_HEARTBEAT_A"),
              os.environ.get("BETTERSTACK_HEARTBEAT_B")]
    unit_urls = {key: url for (key, _), url in zip(devices, hb_env) if url}
    controller_url = os.environ.get("BETTERSTACK_HEARTBEAT_CONTROLLER")
    wd_task = None
    if unit_urls or controller_url:
        wd_task = asyncio.create_task(
            heartbeat_watchdog(ctrl, unit_urls, controller_url))
        print("INFO: Better Stack watchdog on: units=%s controller=%s "
              "(tick=%ds, fresh<%ds)" % (list(unit_urls), bool(controller_url),
                                         WATCHDOG_TICK_S, HEARTBEAT_FRESH_S))
    else:
        print("INFO: Better Stack watchdog disabled "
              "(no BETTERSTACK_HEARTBEAT_* set)")

    # Graceful shutdown: on SIGTERM/SIGINT (systemd stop/restart) disconnect the
    # BLE links cleanly so the next start finds advertising devices.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    # Run until a stop signal or all connections drop for good.
    waiters = [asyncio.create_task(dev.waitDisconnect()) for _, dev in devices]
    stop_task = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait(set(waiters) | {stop_task},
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        print("INFO: shutting down — disconnecting BLE links cleanly")
        if wd_task is not None:
            wd_task.cancel()
        if bs is not None:
            await bs.close()
        for _, dev in devices:
            conn = getattr(dev, "_conn", None)
            if conn is not None:
                try:
                    await conn.shutdown()
                except Exception as e:
                    print("WARN: shutdown %s: %s" % (dev._address, e))
        for t in (*waiters, stop_task):
            t.cancel()


if __name__ == "__main__":
    user_id = os.environ.get("ECOFLOW_USER_ID") or (sys.argv[1] if len(sys.argv) > 1 else None)
    addr_a = os.environ.get("ECOFLOW_ADDR_A") or (sys.argv[2] if len(sys.argv) > 2 else None)
    addr_b = os.environ.get("ECOFLOW_ADDR_B") or (sys.argv[3] if len(sys.argv) > 3 else None)
    http_host = os.environ.get("ECOFLOW_HTTP_HOST", "127.0.0.1")
    http_port = int(os.environ.get("ECOFLOW_HTTP_PORT", "8787"))

    if not (user_id and addr_a and addr_b):
        print("usage: python controller.py <user_id> <address_A> <address_B>")
        print("   or set ECOFLOW_USER_ID / ECOFLOW_ADDR_A / ECOFLOW_ADDR_B")
        sys.exit(1)

    asyncio.run(main(user_id, [addr_a, addr_b], http_host, http_port))
