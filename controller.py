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
import os
import sys
import time

from bleak import BleakScanner

import connect
from connect import located_devices, discoveryCallback
import policy

# Seconds to wait after attaching a unit before detaching the other, so the
# newly-attached unit is carrying load before the break.
MAKE_SETTLE_SECONDS = 5
SCAN_SECONDS = 6.0

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class ParallelController:
    def __init__(self):
        self._conns = {}        # unit_key -> Connection
        self._actual_on = {}    # unit_key -> bool (last commanded ON state)
        self._last_seen = {}    # unit_key -> epoch of last heartbeat
        self._charging = None   # unit_key currently charging, or None
        self._initialized = False
        self._auto_enabled = True
        self._lock = asyncio.Lock()

    def add(self, unit_key, conn):
        self._conns[unit_key] = conn
        self._actual_on[unit_key] = True  # provisional; first eval forces real state
        conn.set_heartbeat_callback(self._on_heartbeat)

    async def _on_heartbeat(self, conn):
        self._last_seen[conn.sn] = time.time()
        await self.evaluate()

    def _soc_map(self):
        return {k: c.last_soc for k, c in self._conns.items() if c.last_soc is not None}

    async def _apply(self, unit_key, want_on):
        await self._conns[unit_key].setParallelBox(set_self=1 if want_on else 0)
        self._actual_on[unit_key] = want_on
        print("CTRL: unit %s -> %s" % (unit_key, "ON" if want_on else "OFF(charging)"))

    async def _transition_to(self, charging_unit):
        """Drive the system to the given charging target. Assumes lock held."""
        units = list(self._conns)

        if not self._initialized:
            # Force both units to their desired state once at startup, since we
            # don't actually know the hardware state. Commands are idempotent;
            # emit ON (make) before OFF (break).
            desired_on = {u: True for u in units}
            if charging_unit is not None:
                desired_on[charging_unit] = False
            order = ([u for u in units if desired_on[u]] +
                     [u for u in units if not desired_on[u]])
            print("CTRL: init -> charging=%s" % charging_unit)
            made = False
            for u in order:
                if not desired_on[u] and made:
                    await asyncio.sleep(MAKE_SETTLE_SECONDS)
                await self._apply(u, desired_on[u])
                made = made or desired_on[u]
            self._charging = charging_unit
            self._initialized = True
            return

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
            charging_unit = policy.decide_charging_unit(soc, self._charging)
            await self._transition_to(charging_unit)

    async def set_manual(self, charging_unit):
        """Manually force a charging target ('A'/'B' key) or None for both ON.

        Disables automation until resume_auto() is called.
        """
        async with self._lock:
            if charging_unit is not None and charging_unit not in self._conns:
                raise ValueError("unknown unit %r" % charging_unit)
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
            units.append({
                "key": k,
                "soc": c.last_soc,
                "on": self._actual_on.get(k),
                "charging": (self._charging == k),
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

    async def index(request):
        path = os.path.join(WEB_DIR, "index.html")
        if os.path.exists(path):
            return web.FileResponse(path)
        return web.Response(text="portal frontend missing", status=500)

    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/api/status", get_status),
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


async def main(user_id, addresses, http_host="127.0.0.1", http_port=8787):
    connect.USER_ID = user_id
    connect.CMD = None  # we drive commands ourselves, not the one-shot CLI hook

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

    ctrl = ParallelController()
    await start_web(ctrl, http_host, http_port)

    print("INFO: connecting to: %s" % [k for k, _ in devices])
    for key, dev in devices:
        await dev.connect()
        ctrl.add(key, dev._conn)

    # Run until both connections drop (Connection auto-reconnects on its own).
    await asyncio.gather(*(dev.waitDisconnect() for _, dev in devices))


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
