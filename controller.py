"""Two-DPU parallel-discharge / charge-rotation controller.

Holds a BLE connection to each of two EcoFlow DPU units, reads each unit's SoC
from its heartbeat, and rotates which unit charges according to policy.py.

ON  = attached to parallel group, discharging (PrStateSet set_self=1)
OFF = detached, charging                       (PrStateSet set_self=0)

Make-before-break: a unit is only detached after the other is attached, so the
load never sees zero units in the parallel group.

Usage:
    python controller.py <user_id> <address_A> <address_B>

Both DPUs must be bonded to the same account (same user_id) and login_key.bin
must be present (same as connect.py).
"""

import asyncio
import sys

from bleak import BleakScanner

import connect
from connect import located_devices, discoveryCallback
import policy

# Seconds to wait after attaching a unit before detaching the other, so the
# newly-attached unit is carrying load before the break.
MAKE_SETTLE_SECONDS = 5
SCAN_SECONDS = 6.0


class ParallelController:
    def __init__(self):
        self._conns = {}        # unit_key -> Connection
        self._actual_on = {}    # unit_key -> bool (last commanded ON state)
        self._charging = None   # unit_key currently charging, or None
        self._initialized = False
        self._lock = asyncio.Lock()

    def add(self, unit_key, conn):
        self._conns[unit_key] = conn
        self._actual_on[unit_key] = True  # provisional; first eval forces real state
        conn.set_heartbeat_callback(self._on_heartbeat)

    async def _on_heartbeat(self, conn):
        await self.evaluate()

    def _soc_map(self):
        return {k: c.last_soc for k, c in self._conns.items() if c.last_soc is not None}

    async def _apply(self, unit_key, want_on):
        await self._conns[unit_key].setParallelBox(set_self=1 if want_on else 0)
        self._actual_on[unit_key] = want_on
        print("CTRL: unit %s -> %s" % (unit_key, "ON" if want_on else "OFF(charging)"))

    async def evaluate(self):
        async with self._lock:
            soc = self._soc_map()
            if len(soc) < len(self._conns):
                return  # wait until both units have reported SoC

            charging_unit = policy.decide_charging_unit(soc, self._charging)
            units = list(self._conns)

            if not self._initialized:
                # Force both units to their desired state once at startup, since
                # we don't actually know the hardware state. Commands are
                # idempotent; emit ON (make) before OFF (break).
                desired_on = {u: True for u in units}
                if charging_unit is not None:
                    desired_on[charging_unit] = False
                order = ([u for u in units if desired_on[u]] +
                         [u for u in units if not desired_on[u]])
                print("CTRL: init soc=%s -> charging=%s" % (soc, charging_unit))
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

            print("CTRL: soc=%s charging %s->%s actions=%s" % (
                soc, self._charging, charging_unit, actions))
            for i, (unit_key, want_on) in enumerate(actions):
                # settle between a make (prev ON) and this break (OFF)
                if i > 0 and actions[i - 1][1] is True and want_on is False:
                    await asyncio.sleep(MAKE_SETTLE_SECONDS)
                await self._apply(unit_key, want_on)
            self._charging = charging_unit


async def main(user_id, addresses):
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
    print("INFO: connecting to: %s" % [k for k, _ in devices])
    for key, dev in devices:
        await dev.connect()
        ctrl.add(key, dev._conn)

    # Run until both connections drop (Connection auto-reconnects on its own).
    await asyncio.gather(*(dev.waitDisconnect() for _, dev in devices))


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("usage: python controller.py <user_id> <address_A> <address_B>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1], [sys.argv[2], sys.argv[3]]))
