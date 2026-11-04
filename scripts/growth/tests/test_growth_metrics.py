from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import ROOT  # noqa: F401  (sets sys.path)

from scripts.growth.metrics import MetricsWindow, percentile
from scripts.growth.store import CheckpointStore


class PercentileTests(unittest.TestCase):
    def test_empty_sample_has_no_percentile(self):
        self.assertIsNone(percentile([], 50))

    def test_nearest_rank_p50_and_p90(self):
        values = [10.0, 20.0, 30.0, 40.0]
        self.assertEqual(percentile(values, 50), 20.0)
        self.assertEqual(percentile(values, 90), 40.0)

    def test_single_sample_serves_both_percentiles(self):
        self.assertEqual(percentile([4.0], 50), 4.0)
        self.assertEqual(percentile([4.0], 90), 4.0)


class MetricsWindowTests(unittest.TestCase):
    def window(self) -> MetricsWindow:
        return MetricsWindow(
            window_seconds=3600, max_gap_seconds=120, sample_limit=10,
            kill_sample_limit=10)

    def test_rates_use_only_observed_active_time(self):
        window = self.window()
        window.observe(0, level=5, xp=0, next_level_xp=100,
                       kills=0, deaths=0, stuck_events=0)
        window.observe(60, level=5, xp=60, next_level_xp=100,
                       kills=1, deaths=0, stuck_events=0)
        window.observe(120, level=5, xp=120, next_level_xp=100,
                       kills=1, deaths=1, stuck_events=1)
        snapshot = window.snapshot(now=120)
        self.assertAlmostEqual(snapshot["xp_per_hour"], 3600.0)
        self.assertAlmostEqual(snapshot["death_rate"], 30.0)
        self.assertAlmostEqual(snapshot["stuck_rate"], 30.0)
        self.assertAlmostEqual(snapshot["kills_per_hour"], 30.0)
        self.assertEqual(snapshot["active_seconds"], 120.0)

    def test_downtime_gap_is_not_counted_as_active_time(self):
        window = self.window()
        window.observe(0, level=5, xp=0, next_level_xp=100)
        window.observe(3600, level=5, xp=1000, next_level_xp=100)
        snapshot = window.snapshot(now=3600)
        self.assertEqual(snapshot["active_seconds"], 0.0)
        self.assertIsNone(snapshot["xp_per_hour"])

    def test_level_up_delta_uses_the_previous_level_remainder(self):
        window = self.window()
        window.observe(0, level=4, xp=900, next_level_xp=1000)
        window.observe(60, level=5, xp=50, next_level_xp=2000)
        snapshot = window.snapshot(now=60)
        # 100 XP to finish level 4 plus 50 XP into level 5.
        self.assertAlmostEqual(snapshot["xp_per_hour"], 9000.0)

    def test_kill_time_percentiles_use_sampled_kills(self):
        window = self.window()
        window.record_kill(100, 10.0)
        window.record_kill(110, 20.0)
        window.record_kill(120, 30.0)
        snapshot = window.snapshot(now=120)
        self.assertEqual(snapshot["kill_time_p50"], 20.0)
        self.assertEqual(snapshot["kill_time_p90"], 30.0)

    def test_round_trip_preserves_samples_and_rates(self):
        window = self.window()
        window.observe(0, level=5, xp=0, next_level_xp=100)
        window.observe(60, level=5, xp=60, next_level_xp=100)
        window.record_kill(60, 12.5)
        restored = MetricsWindow.from_dict(window.to_dict())
        self.assertEqual(restored.to_dict(), window.to_dict())
        self.assertAlmostEqual(restored.snapshot(60)["xp_per_hour"], 3600.0)

    def test_samples_are_bounded(self):
        window = MetricsWindow(window_seconds=3600, max_gap_seconds=120,
                               sample_limit=3, kill_sample_limit=2)
        for index in range(10):
            window.observe(index, level=5, xp=index, next_level_xp=100)
            window.record_kill(index, float(index))
        self.assertLessEqual(len(window.observations), 3)
        self.assertLessEqual(len(window.kill_samples), 2)


class MetricsPersistenceTests(unittest.TestCase):
    def test_metrics_round_trip_through_the_character_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "char.db"
            store = CheckpointStore(path)
            store.open()
            window = MetricsWindow(window_seconds=3600, max_gap_seconds=120)
            window.observe(0, level=5, xp=0, next_level_xp=100)
            window.observe(60, level=5, xp=60, next_level_xp=100,
                           kills=1, deaths=0, stuck_events=2)
            window.record_kill(60, 12.5)
            store.save_metrics(window.to_dict())
            store.close()

            reopened = CheckpointStore(path)
            reopened.open()
            self.addCleanup(reopened.close)
            restored = MetricsWindow.from_dict(reopened.load_metrics())
            snapshot = restored.snapshot(60)
            self.assertAlmostEqual(snapshot["xp_per_hour"], 3600.0)
            self.assertEqual(snapshot["kill_time_p50"], 12.5)

    def test_missing_metrics_document_loads_as_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp) / "char.db")
            store.open()
            self.addCleanup(store.close)
            self.assertIsNone(store.load_metrics())


if __name__ == "__main__":
    unittest.main()
