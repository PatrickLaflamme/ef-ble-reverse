"""Tests for metrics.py (SQLite energy/usage accumulation)."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import metrics  # noqa: E402

# 2026-06-20 12:00 UTC and a point ~1h later, same UTC day.
T0 = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc).timestamp()


def fresh(max_gap_s=1e9):
    # default to a huge gap so tests can integrate hour-long intervals; the
    # gap-skip test overrides with the real default.
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    m = metrics.Metrics(path, max_gap_s=max_gap_s)
    return m, path


class MetricsTests(unittest.TestCase):
    def test_first_sample_no_interval(self):
        m, path = fresh()
        try:
            self.assertEqual(m.record("A", T0, False, 100, 50, 30, 70), 0.0)
            self.assertEqual(m.summary(T0)["all_time"]["per_unit"], {})
        finally:
            m.close(); os.unlink(path)

    def test_energy_integration_over_an_hour(self):
        m, path = fresh()
        try:
            m.record("A", T0, True, 0, 0, 0, 0)            # establish t0
            m.record("A", T0 + 3600, True, 1000, 0, 600, 400)  # 1h at these watts
            u = m.summary(T0)["all_time"]["per_unit"]["A"]
            self.assertAlmostEqual(u["in_wh"], 1000, delta=1)   # 1000W * 1h
            self.assertAlmostEqual(u["solar_wh"], 600, delta=1)
            self.assertAlmostEqual(u["grid_wh"], 400, delta=1)
            self.assertAlmostEqual(u["charge_s"], 3600, delta=1)
            self.assertAlmostEqual(u["discharge_s"], 0, delta=1)
        finally:
            m.close(); os.unlink(path)

    def test_discharge_time_and_delta(self):
        m, path = fresh()
        try:
            m.record("A", T0, False, 0, 0, 0, 0)
            m.record("A", T0 + 1800, False, 200, 500, 0, 0)  # 0.5h discharging
            u = m.summary(T0)["all_time"]["per_unit"]["A"]
            self.assertAlmostEqual(u["discharge_s"], 1800, delta=1)
            self.assertAlmostEqual(u["charge_s"], 0, delta=1)
            self.assertAlmostEqual(u["in_wh"], 100, delta=1)   # 200W*0.5h
            self.assertAlmostEqual(u["out_wh"], 250, delta=1)  # 500W*0.5h
            self.assertAlmostEqual(u["delta_wh"], -150, delta=1)
        finally:
            m.close(); os.unlink(path)

    def test_gap_skipped(self):
        m, path = fresh(max_gap_s=metrics.MAX_GAP_S)  # real default (120s)
        try:
            m.record("A", T0, True, 1000, 0, 0, 0)
            # gap > MAX_GAP_S: must not integrate across it
            self.assertEqual(m.record("A", T0 + 10000, True, 1000, 0, 0, 0), 0.0)
            self.assertEqual(m.summary(T0)["all_time"]["per_unit"], {})
        finally:
            m.close(); os.unlink(path)

    def test_totals_sum_units(self):
        m, path = fresh()
        try:
            for u in ("A", "B"):
                m.record(u, T0, True, 0, 0, 0, 0)
                m.record(u, T0 + 3600, True, 500, 0, 500, 0)
            tot = m.summary(T0)["all_time"]["totals"]
            self.assertAlmostEqual(tot["solar_wh"], 1000, delta=2)  # 500+500
            self.assertAlmostEqual(tot["charge_s"], 7200, delta=2)
        finally:
            m.close(); os.unlink(path)

    def test_persists_across_reopen(self):
        m, path = fresh()
        try:
            m.record("A", T0, True, 0, 0, 0, 0)
            m.record("A", T0 + 3600, True, 1000, 0, 0, 0)
            m.close()
            m2 = metrics.Metrics(path)
            u = m2.summary(T0)["all_time"]["per_unit"]["A"]
            self.assertAlmostEqual(u["in_wh"], 1000, delta=1)
            m2.close()
        finally:
            os.unlink(path)

    def test_loss_estimate_from_soc_and_capacity(self):
        m, path = fresh()
        try:
            cap = 6000  # Wh
            # 1h: in 1000Wh, out 0; SoC 50 -> 60 => stored 0.10*6000 = 600Wh.
            # loss = 1000 - 0 - 600 = 400Wh.
            m.record("A", T0, True, 0, 0, 0, 0, soc=50, cap_wh=cap)
            m.record("A", T0 + 3600, True, 1000, 0, 0, 0, soc=60, cap_wh=cap)
            u = m.summary(T0)["all_time"]["per_unit"]["A"]
            self.assertAlmostEqual(u["loss_wh"], 400, delta=2)

        finally:
            m.close(); os.unlink(path)

    def test_loss_zero_without_capacity(self):
        m, path = fresh()
        try:
            m.record("A", T0, True, 0, 0, 0, 0, soc=50, cap_wh=None)
            m.record("A", T0 + 3600, True, 1000, 0, 0, 0, soc=60, cap_wh=None)
            u = m.summary(T0)["all_time"]["per_unit"]["A"]
            self.assertEqual(u["loss_wh"], 0)  # no capacity -> no loss estimate
        finally:
            m.close(); os.unlink(path)

    def test_history_samples_recorded_and_windowed(self):
        m, path = fresh()
        try:
            # samples are downsampled to >= SAMPLE_INTERVAL_S apart
            for i in range(5):
                m.record("A", T0 + i * metrics.SAMPLE_INTERVAL_S, True,
                         100, 0, 50, 0, soc=50 + i, cap_wh=6000)
            hist = m.history(hours=24, now_ts=T0 + 5 * metrics.SAMPLE_INTERVAL_S)
            self.assertIn("A", hist)
            self.assertEqual(len(hist["A"]), 5)
            self.assertEqual(hist["A"][0]["soc"], 50)
            # narrow window excludes older samples
            recent = m.history(hours=0.02, now_ts=T0 + 5 * metrics.SAMPLE_INTERVAL_S)
            self.assertLess(len(recent.get("A", [])), 5)
        finally:
            m.close(); os.unlink(path)

    def test_samples_downsampled(self):
        m, path = fresh()
        try:
            # two records closer than SAMPLE_INTERVAL_S -> only one sample row
            m.record("A", T0, True, 100, 0, 0, 0, soc=50, cap_wh=6000)
            m.record("A", T0 + 5, True, 100, 0, 0, 0, soc=50, cap_wh=6000)
            hist = m.history(hours=24, now_ts=T0 + 5)
            self.assertEqual(len(hist["A"]), 1)
        finally:
            m.close(); os.unlink(path)

    def test_today_vs_all_time_buckets(self):
        m, path = fresh()
        try:
            day2 = T0 + 86400  # next UTC day
            m.record("A", T0, True, 0, 0, 0, 0)
            m.record("A", T0 + 3600, True, 1000, 0, 0, 0)       # day 1
            m.record("A", day2, True, 0, 0, 0, 0)
            m.record("A", day2 + 3600, True, 2000, 0, 0, 0)     # day 2
            s = m.summary(day2)  # "today" = day2
            self.assertAlmostEqual(s["today"]["per_unit"]["A"]["in_wh"], 2000, delta=1)
            self.assertAlmostEqual(s["all_time"]["per_unit"]["A"]["in_wh"], 3000, delta=2)
        finally:
            m.close(); os.unlink(path)


if __name__ == "__main__":
    unittest.main()
