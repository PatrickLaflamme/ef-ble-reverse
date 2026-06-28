"""Tests for the monitoring stack: RSSI + drop-rate metrics, the Better Stack
emitter, and the heartbeat watchdog's freshness gating.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import betterstack  # noqa: E402
import controller  # noqa: E402
from metrics import Metrics  # noqa: E402


class MetricsHealthTests(unittest.TestCase):
    def setUp(self):
        self.m = Metrics(":memory:")

    def tearDown(self):
        self.m.close()

    def test_rssi_persisted_in_samples(self):
        self.m.record("U", 1000.0, False, 100, 200, 0, 0, soc=50, rssi=-72)
        h = self.m.history(hours=24, now_ts=1000.0)
        self.assertEqual(h["U"][0]["rssi"], -72)

    def test_drop_counts_windowed(self):
        now = 100000.0
        # two disconnects in the last hour, one ~2h ago
        self.m.record_event("U", now - 60, "disconnect")
        self.m.record_event("U", now - 600, "disconnect")
        self.m.record_event("U", now - 7200, "disconnect")
        self.m.record_event("U", now - 30, "reconnect_fail")
        h = self.m.health(now_ts=now)
        self.assertEqual(h["U"]["drops"]["1h"], 2)
        self.assertEqual(h["U"]["drops"]["24h"], 3)
        self.assertEqual(h["U"]["reconnect_fails"]["1h"], 1)

    def test_health_reports_last_rssi(self):
        self.m.record("U", 1000.0, False, 0, 0, 0, 0, soc=50, rssi=-80)
        self.m.record("U", 1000.0 + 61, False, 0, 0, 0, 0, soc=50, rssi=-65)
        h = self.m.health(now_ts=1000.0 + 120)
        self.assertEqual(h["U"]["last_rssi"], -65)  # most recent sample


class BetterStackEmitterTests(unittest.TestCase):
    def _emitter(self):
        e = betterstack.BetterStackEmitter("http://x", "tok", interval_s=60)
        sent = []
        e._spawn = lambda body: sent.append(body)  # capture instead of POST
        return e, sent

    def test_sample_rate_limited_per_unit(self):
        e, sent = self._emitter()
        e.maybe_emit_sample("A", 1000.0, {"soc": 50})
        e.maybe_emit_sample("A", 1030.0, {"soc": 51})  # within 60s -> dropped
        e.maybe_emit_sample("A", 1061.0, {"soc": 52})  # past interval -> sent
        e.maybe_emit_sample("B", 1005.0, {"soc": 80})  # other unit, independent
        socs = [(b["unit"], b["soc"]) for b in sent]
        self.assertEqual(socs, [("A", 50), ("A", 52), ("B", 80)])

    def test_sample_drops_none_fields(self):
        e, sent = self._emitter()
        e.maybe_emit_sample("A", 1000.0, {"soc": 50, "rssi": None, "solar_w": 0})
        body = sent[0]
        self.assertNotIn("rssi", body)   # None filtered out
        self.assertEqual(body["solar_w"], 0)  # zero kept
        self.assertEqual(body["kind"], "sample")

    def test_events_not_rate_limited(self):
        e, sent = self._emitter()
        e.emit_event("A", 1000.0, "disconnect")
        e.emit_event("A", 1001.0, "reconnect_fail")
        self.assertEqual([b["event"] for b in sent], ["disconnect", "reconnect_fail"])

    def test_from_env_requires_both(self):
        self.assertIsNone(betterstack.from_env({}))
        self.assertIsNone(betterstack.from_env({"BETTERSTACK_SOURCE_URL": "u"}))
        e = betterstack.from_env(
            {"BETTERSTACK_SOURCE_URL": "u", "BETTERSTACK_SOURCE_TOKEN": "t"})
        self.assertIsInstance(e, betterstack.BetterStackEmitter)


class _FakeCtrl:
    def __init__(self, last_seen):
        self._last_seen = last_seen


class WatchdogTickTests(unittest.IsolatedAsyncioTestCase):
    async def _tick(self, last_seen, stale, fresh_s=30, now=1000.0):
        ctrl = _FakeCtrl(last_seen)
        urls = {"A": "urlA", "B": "urlB"}
        pinged = []

        async def ping(label, url):
            pinged.append(label)

        await controller._watchdog_tick(now, ctrl, urls, fresh_s, stale, ping)
        return pinged

    async def test_pings_fresh_withholds_stale(self):
        # A fresh (5s), B stale (90s)
        stale = {"A": False, "B": False}
        pinged = await self._tick({"A": 995.0, "B": 910.0}, stale)
        self.assertEqual(pinged, ["A"])        # only the fresh unit pinged
        self.assertTrue(stale["B"])            # B marked stale -> Better Stack pages
        self.assertFalse(stale["A"])

    async def test_never_seen_is_stale(self):
        stale = {"A": False, "B": False}
        pinged = await self._tick({"A": 995.0}, stale)  # B never seen
        self.assertEqual(pinged, ["A"])
        self.assertTrue(stale["B"])

    async def test_recovery_resumes_ping(self):
        stale = {"A": False, "B": True}        # B was stale
        pinged = await self._tick({"A": 995.0, "B": 995.0}, stale)  # B now fresh
        self.assertEqual(sorted(pinged), ["A", "B"])
        self.assertFalse(stale["B"])           # cleared


class _FakeConn:
    def __init__(self, sn, soc=None, rssi=None):
        self.sn = sn
        self.last_soc = soc
        self._rssi = rssi
        self._last_power = {"watts_in": 10, "watts_out": 900, "solar": 5, "grid": 0}
        self._last_show_flag = None
        self._last_access_5p8_out_type = 1

    def set_heartbeat_callback(self, cb):
        pass

    def set_event_callback(self, cb):
        pass


class _RecordingMetrics:
    def __init__(self):
        self.samples = []
        self.events = []

    def record(self, unit, ts, charging, *a, **k):
        self.samples.append((unit, k.get("rssi")))

    def record_event(self, unit, ts, event):
        self.events.append((unit, event))


class _RecordingBS:
    def __init__(self):
        self.samples = []
        self.events = []

    def maybe_emit_sample(self, unit, ts, fields):
        self.samples.append((unit, fields))

    def emit_event(self, unit, ts, event):
        self.events.append((unit, event))


class ControllerWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_and_event_fan_out_to_db_and_betterstack(self):
        controller.MAKE_SETTLE_SECONDS = 0
        m, bs = _RecordingMetrics(), _RecordingBS()
        c = controller.ParallelController(metrics=m, betterstack=bs)
        conn = _FakeConn("U", soc=55, rssi=-70)
        c.add("U", conn)

        await c._on_heartbeat(conn)
        # both stores got the sample, RSSI included
        self.assertEqual(m.samples, [("U", -70)])
        self.assertEqual(bs.samples[0][0], "U")
        self.assertEqual(bs.samples[0][1]["soc"], 55)
        self.assertEqual(bs.samples[0][1]["rssi"], -70)

        c._on_conn_event(conn, "disconnect")
        self.assertEqual(m.events, [("U", "disconnect")])
        self.assertEqual(bs.events, [("U", "disconnect")])


if __name__ == "__main__":
    unittest.main()
