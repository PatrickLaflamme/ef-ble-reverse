"""Tests for the pure charge-rotation policy (policy.py)."""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import policy  # noqa: E402

DAY = datetime(2026, 6, 20, 15, 0, tzinfo=timezone.utc)    # outside window
NIGHT = datetime(2026, 6, 20, 6, 0, tzinfo=timezone.utc)   # inside window


class InWindowTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertTrue(policy.in_window(datetime(2026, 6, 20, 4, 30, tzinfo=timezone.utc)))
        self.assertTrue(policy.in_window(datetime(2026, 6, 20, 11, 59, tzinfo=timezone.utc)))
        self.assertFalse(policy.in_window(datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)))
        self.assertFalse(policy.in_window(datetime(2026, 6, 20, 4, 29, tzinfo=timezone.utc)))
        self.assertFalse(policy.in_window(DAY))


class DecideChargingUnitTests(unittest.TestCase):
    def d(self, soc, charging=None, now=DAY):
        return policy.decide_charging_unit(soc, charging, now)

    def test_both_healthy_day_both_on(self):
        self.assertIsNone(self.d({"A": 70, "B": 65}))

    def test_floor_charges_lower(self):
        self.assertEqual(self.d({"A": 38, "B": 65}), "A")

    def test_early_when_both_below_60(self):
        self.assertEqual(self.d({"A": 50, "B": 55}), "A")

    def test_early_blocked_when_partner_high(self):
        self.assertIsNone(self.d({"A": 50, "B": 62}))

    def test_window_topup_in_window(self):
        self.assertEqual(self.d({"A": 72, "B": 90}, now=NIGHT), "A")

    def test_window_topup_blocked_in_day(self):
        self.assertIsNone(self.d({"A": 72, "B": 90}, now=DAY))

    def test_hysteresis_holds_until_target(self):
        self.assertEqual(self.d({"A": 78, "B": 90}, charging="A"), "A")

    def test_hysteresis_releases_at_target(self):
        self.assertIsNone(self.d({"A": 80, "B": 90}, charging="A"))

    def test_switch_after_target_to_floor_unit(self):
        self.assertEqual(self.d({"A": 80, "B": 38}, charging="A"), "B")

    def test_critical_switch_at_15(self):
        self.assertEqual(self.d({"A": 55, "B": 15}, charging="A"), "B")

    def test_critical_just_above_holds(self):
        self.assertEqual(self.d({"A": 55, "B": 16}, charging="A"), "A")

    def test_critical_overrides_at_night(self):
        self.assertEqual(self.d({"A": 60, "B": 12}, charging="A", now=NIGHT), "B")

    def test_critical_beats_hysteresis(self):
        # A still charging (50, not yet 80) but load-bearing B at 10 -> switch.
        self.assertEqual(self.d({"A": 50, "B": 10}, charging="A"), "B")

    # --- boundary exactness ---
    def test_floor_inclusive_at_40(self):
        self.assertEqual(self.d({"A": 40, "B": 90}), "A")  # <= 40

    def test_floor_exclusive_at_41(self):
        self.assertIsNone(self.d({"A": 41, "B": 90}))

    def test_early_inclusive_at_50_both_below_60(self):
        self.assertEqual(self.d({"A": 50, "B": 59}), "A")

    def test_early_partner_exactly_60_blocks(self):
        self.assertIsNone(self.d({"A": 50, "B": 60}))  # both<60 is strict

    def test_window_strict_below_75(self):
        self.assertIsNone(self.d({"A": 75, "B": 90}, now=NIGHT))  # 75 not < 75
        self.assertEqual(self.d({"A": 74, "B": 90}, now=NIGHT), "A")

    def test_target_exactly_80_releases(self):
        self.assertIsNone(self.d({"A": 80, "B": 90}, charging="A"))

    def test_critical_inclusive_at_15(self):
        self.assertEqual(self.d({"A": 55, "B": 15}, charging="A"), "B")

    def test_tie_picks_a_deterministically(self):
        # equal SoC at floor: min() picks first inserted key ("A")
        self.assertEqual(self.d({"A": 40, "B": 40}), "A")


class PlanTransitionsTests(unittest.TestCase):
    def test_make_before_break_on_switch(self):
        # Switching charging A->B: turn B's partner (A) ON before B OFF.
        acts = policy.plan_transitions("B", ["A", "B"], {"A": False, "B": True})
        self.assertEqual(acts, [("A", True), ("B", False)])

    def test_no_actions_when_already_satisfied(self):
        self.assertEqual(
            policy.plan_transitions(None, ["A", "B"], {"A": True, "B": True}), [])

    def test_start_charging_turns_one_off(self):
        self.assertEqual(
            policy.plan_transitions("A", ["A", "B"], {"A": True, "B": True}),
            [("A", False)])

    def test_both_off_self_heals_to_both_on(self):
        self.assertEqual(
            policy.plan_transitions(None, ["A", "B"], {"A": False, "B": False}),
            [("A", True), ("B", True)])


if __name__ == "__main__":
    unittest.main()
