"""Tests for the controller orchestration (controller.py).

Uses a FakeConn (no BLE) and drives evaluate()/set_manual() directly. Run from
the repo root so `connect.py` can load login_key.bin:

    .venv/bin/python -m unittest discover -s tests -v
"""

import collections
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import controller  # noqa: E402
import policy       # noqa: E402


class FakeConn:
    """Stand-in for connect.Connection: records setParallelBox calls."""

    def __init__(self, sn, soc=None, a5p8=None):
        self.sn = sn
        self.last_soc = soc
        self._address = sn
        self._last_show_flag = None
        self._last_access_5p8_out_type = a5p8
        self.calls = []

    def set_heartbeat_callback(self, cb):
        self._cb = cb

    async def setParallelBox(self, set_self=None, set_para=None):
        self.calls.append(set_self)


def make_ctrl(units):
    """units: list of (key, soc, access_5p8_out_type). Returns (ctrl, conns, labels)."""
    controller.MAKE_SETTLE_SECONDS = 0  # no real sleeps in tests
    c = controller.ParallelController()
    conns = {}
    labels = []
    for key, soc, a5p8 in units:
        fc = FakeConn(key, soc, a5p8)
        conns[key] = fc
        c.add(key, fc)
    orig = c._apply

    async def logged(u, on):
        labels.append((u, "ON" if on else "OFF"))
        await orig(u, on)

    c._apply = logged
    return c, conns, labels


class _CtrlBase(unittest.IsolatedAsyncioTestCase):
    """Base that forces out-of-window and restores policy.in_window after each
    test (it is a module global, so leaving it stubbed would leak into other
    test modules)."""

    def setUp(self):
        self._orig_in_window = policy.in_window
        policy.in_window = lambda now=None: False
        self.addCleanup(setattr, policy, "in_window", self._orig_in_window)


class StartupSeedingTests(_CtrlBase):
    async def test_both_attached_healthy_sends_no_commands(self):
        c, _, labels = make_ctrl([("A", 70, 1), ("B", 65, 1)])
        await c.evaluate()
        self.assertIsNone(c._charging)
        self.assertEqual(labels, [])
        self.assertEqual(c._actual_on, {"A": True, "B": True})

    async def test_in_progress_charge_preserved_across_restart(self):
        # A detached/charging (a5p8=0) at restart -> keep charging, no commands.
        c, _, labels = make_ctrl([("A", 55, 0), ("B", 70, 1)])
        await c.evaluate()
        self.assertEqual(c._charging, "A")
        self.assertEqual(labels, [])
        self.assertEqual(c._actual_on, {"A": False, "B": True})

    async def test_attached_but_at_floor_starts_charge(self):
        c, _, labels = make_ctrl([("A", 38, 1), ("B", 65, 1)])
        await c.evaluate()
        self.assertEqual(c._charging, "A")
        self.assertEqual(labels, [("A", "OFF")])

    async def test_both_detached_self_heals_to_both_on(self):
        c, _, labels = make_ctrl([("A", 70, 0), ("B", 65, 0)])
        await c.evaluate()
        self.assertTrue(c._actual_on["A"] and c._actual_on["B"])

    async def test_partial_telemetry_defaults_missing_to_on(self):
        # B reports no access_5p8_out_type (None) -> stays default ON.
        c, _, _ = make_ctrl([("A", 55, 0), ("B", 70, None)])
        c._seed_initial_state()
        self.assertEqual(c._charging, "A")          # A (a5p8=0) is the charger
        self.assertFalse(c._actual_on["A"])
        self.assertTrue(c._actual_on["B"])          # None -> default ON

    async def test_pd303_output_type_not_treated_as_charger(self):
        # a5p8 == 2 (OUT_PD303): seeded off (not ==1) but NOT adopted as charger.
        c, _, _ = make_ctrl([("A", 70, 2), ("B", 65, 1)])
        c._seed_initial_state()
        self.assertIsNone(c._charging)              # only a5p8==0 becomes charger
        self.assertFalse(c._actual_on["A"])
        self.assertTrue(c._actual_on["B"])


class TransitionTests(_CtrlBase):
    async def test_floor_hysteresis_switch_sequence(self):
        c, conns, labels = make_ctrl([("A", 70, 1), ("B", 65, 1)])
        await c.evaluate()
        self.assertIsNone(c._charging)

        conns["A"].last_soc = 38
        labels.clear()
        await c.evaluate()
        self.assertEqual(c._charging, "A")
        self.assertEqual(labels, [("A", "OFF")])

        # hysteresis: keep charging A, no new commands
        conns["A"].last_soc, conns["B"].last_soc = 78, 90
        labels.clear()
        await c.evaluate()
        self.assertEqual(c._charging, "A")
        self.assertEqual(labels, [])

        # A reaches target, B now at floor -> switch with make-before-break
        conns["A"].last_soc, conns["B"].last_soc = 80, 38
        labels.clear()
        await c.evaluate()
        self.assertEqual(c._charging, "B")
        self.assertEqual(labels, [("A", "ON"), ("B", "OFF")])

    async def test_critical_override_switch_e2e(self):
        c, conns, labels = make_ctrl([("A", 70, 1), ("B", 65, 1)])
        await c.evaluate()
        conns["A"].last_soc = 38
        await c.evaluate()
        self.assertEqual(c._charging, "A")
        # A still charging (55), B load-bearing crashes to 15 -> switch to B
        conns["A"].last_soc, conns["B"].last_soc = 55, 15
        labels.clear()
        await c.evaluate()
        self.assertEqual(c._charging, "B")
        self.assertEqual(labels, [("A", "ON"), ("B", "OFF")])


class ManualOverrideTests(_CtrlBase):
    async def test_manual_pauses_auto_then_resume(self):
        c, conns, _ = make_ctrl([("A", 70, 1), ("B", 65, 1)])
        await c.evaluate()
        await c.set_manual("B")
        self.assertFalse(c._auto_enabled)
        self.assertEqual(c._charging, "B")
        # while paused, evaluate() is a no-op
        conns["A"].last_soc = 30
        await c.evaluate()
        self.assertEqual(c._charging, "B")
        await c.resume_auto()
        self.assertTrue(c._auto_enabled)

    async def test_unknown_unit_raises(self):
        c, _, _ = make_ctrl([("A", 70, 1), ("B", 65, 1)])
        await c.evaluate()
        with self.assertRaises(ValueError):
            await c.set_manual("Z")


class EtaTests(unittest.IsolatedAsyncioTestCase):
    async def test_charging_eta_to_target(self):
        c, _, _ = make_ctrl([("A", 55, 0), ("B", 70, 1)])
        now = 1_000_000.0
        c._soc_hist["A"] = collections.deque([(now - 600, 50), (now - 300, 52.5), (now, 55)])
        eta = c._eta("A", 55, True, now)  # +30%/hr -> (80-55)/30*3600 = 3000s
        self.assertEqual(eta["kind"], "reactivate")
        self.assertEqual(eta["target"], 80)
        self.assertAlmostEqual(eta["seconds"], 3000, delta=60)

    async def test_discharging_eta_to_floor(self):
        c, _, _ = make_ctrl([("A", 58, 1), ("B", 70, 1)])
        now = 1_000_000.0
        c._soc_hist["A"] = collections.deque([(now - 600, 60), (now - 300, 59), (now, 58)])
        eta = c._eta("A", 58, False, now)  # -12%/hr -> (58-40)/12*3600 = 5400s
        self.assertEqual(eta["kind"], "needs_charge")
        self.assertEqual(eta["target"], 40)
        self.assertAlmostEqual(eta["seconds"], 5400, delta=120)

    async def test_short_span_yields_no_eta(self):
        c, _, _ = make_ctrl([("A", 51, 1), ("B", 70, 1)])
        now = 1_000_000.0
        c._soc_hist["A"] = collections.deque([(now - 60, 50), (now, 51)])
        self.assertIsNone(c._eta("A", 51, True, now))

    async def test_flat_soc_no_eta(self):
        c, _, _ = make_ctrl([("A", 60, 1), ("B", 70, 1)])
        now = 1_000_000.0
        c._soc_hist["A"] = collections.deque([(now - 600, 60), (now - 300, 60), (now, 60)])
        self.assertIsNone(c._eta("A", 60, True, now))   # rate 0 -> no ETA
        self.assertIsNone(c._eta("A", 60, False, now))

    async def test_wrong_sign_no_eta(self):
        c, _, _ = make_ctrl([("A", 60, 1), ("B", 70, 1)])
        now = 1_000_000.0
        # SoC rising but unit marked discharging -> no needs_charge ETA
        c._soc_hist["A"] = collections.deque([(now - 600, 55), (now - 300, 57), (now, 60)])
        self.assertIsNone(c._eta("A", 60, False, now))

    async def test_old_samples_pruned_from_window(self):
        c, _, _ = make_ctrl([("A", 55, 0), ("B", 70, 1)])
        now = 1_000_000.0
        # one ancient sample (outside RATE_WINDOW_S) + recent rising ones
        c._soc_hist["A"] = collections.deque([
            (now - 5000, 5), (now - 600, 50), (now - 300, 52.5), (now, 55)])
        eta = c._eta("A", 55, True, now)
        self.assertAlmostEqual(eta["seconds"], 3000, delta=120)  # ~+30%/hr, ancient ignored

    async def test_history_cleared_on_mode_switch(self):
        c, conns, _ = make_ctrl([("A", 70, 1), ("B", 65, 1)])
        await c.evaluate()
        c._soc_hist["A"] = collections.deque([(1.0, 70), (2.0, 69)])
        await c._apply("A", False)  # mode switch
        self.assertEqual(len(c._soc_hist["A"]), 0)


class WebPortalTests(_CtrlBase):
    async def test_status_control_index(self):
        from aiohttp.test_utils import TestClient, TestServer
        c, _, _ = make_ctrl([("A", 70, 1), ("B", 65, 1)])
        await c.evaluate()
        app = controller.make_web_app(c)
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/api/status")
            self.assertEqual(r.status, 200)
            st = await r.json()
            self.assertEqual(len(st["units"]), 2)
            self.assertEqual(st["policy"]["critical_soc"], 15)
            self.assertIn("eta", st["units"][0])

            r = await client.post("/api/control", json={"action": "charge", "unit": "B"})
            self.assertEqual(r.status, 200)
            st = await r.json()
            self.assertFalse(st["automation_enabled"])
            self.assertEqual(st["charging_unit"], "B")

            r = await client.post("/api/control", json={"action": "auto"})
            self.assertEqual(r.status, 200)

            r = await client.post("/api/control", json={"action": "nope"})
            self.assertEqual(r.status, 400)

            r = await client.get("/")
            self.assertEqual(r.status, 200)
            self.assertIn("Parallel Controller", await r.text())


if __name__ == "__main__":
    unittest.main()
