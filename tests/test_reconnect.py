"""Reconnect-resilience tests for Connection.

Regression guard for the 2026-06-27 outage: a flaky BLE link dropped one DPU,
the old fire-and-forget reconnect spawned a storm of overlapping connect()
tasks that collided in BlueZ (org.bluez.Error.InProgress) and all died with
unretrieved exceptions, leaving the unit permanently offline. These tests pin
the three fixes: drop-stale-client, single-loop guard, and durable retry.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import connect  # noqa: E402
from _common import make_conn, FakeBleakClient  # noqa: E402


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_loop_retries_until_success(self):
        """A failed reconnect reschedules itself instead of dying silently."""
        c = make_conn()
        c.RECONNECT_BASE_DELAY = 0  # don't actually sleep between attempts
        c.RECONNECT_MAX_DELAY = 0
        calls = {"n": 0}

        async def flaky_connect(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise connect.BleakError("transient")

        c.connect = flaky_connect
        await c._reconnect_loop()

        self.assertEqual(calls["n"], 3)       # failed twice, then succeeded
        self.assertFalse(c._reconnecting)     # guard released on exit

    async def test_reconnect_loop_stops_when_retry_flag_cleared(self):
        """shutdown() clearing the flag ends the loop instead of spinning."""
        c = make_conn()
        c.RECONNECT_BASE_DELAY = 0
        c.RECONNECT_MAX_DELAY = 0
        c._retry_on_disconnect = False
        calls = {"n": 0}

        async def counting_connect(*a, **k):
            calls["n"] += 1

        c.connect = counting_connect
        await c._reconnect_loop()
        self.assertEqual(calls["n"], 0)       # never even attempted

    async def test_disconnect_storm_spawns_one_loop_and_drops_client(self):
        """Many rapid disconnect callbacks must not storm the adapter."""
        c = make_conn()
        c._client = FakeBleakClient()
        c.RECONNECT_BASE_DELAY = 0
        c.RECONNECT_MAX_DELAY = 0
        connect_calls = {"n": 0}
        gate = asyncio.Event()

        async def slow_connect(*a, **k):
            connect_calls["n"] += 1
            await gate.wait()  # hold the single loop open

        c.connect = slow_connect

        for _ in range(10):       # the storm: 10 disconnects in a tight window
            c.disconnected()

        self.assertIsNone(c._client)          # stale client dropped immediately
        for _ in range(20):                   # let every scheduled task run
            await asyncio.sleep(0)

        self.assertTrue(c._reconnecting)       # exactly one loop is live...
        self.assertEqual(connect_calls["n"], 1)  # ...and only it called connect

        gate.set()
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(c._reconnecting)      # released after success


if __name__ == "__main__":
    unittest.main()
