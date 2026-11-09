import unittest

from encoder_core import MeasurementCalculator
from encoder_stats import SpeedStats


class SpeedStatsTests(unittest.TestCase):
    def test_extremes_and_time_window(self):
        stats = SpeedStats()
        for timestamp, speed in [(0, None), (1, 2), (2, -1), (6, 2), (7.01, 0.5)]:
            stats.add(timestamp, speed)
        self.assertEqual(stats.minimum, (2, -1))
        self.assertEqual(stats.maximum, (1, 2))
        self.assertEqual(stats.fluctuation(), (0.5, 2))
        self.assertIsNone(SpeedStats().minimum)

    def test_five_second_average_uses_interpolated_distance(self):
        calculator = MeasurementCalculator(resolution=1, circumference=1,
                                           max_speed=10, average_seconds=5)
        results = [calculator.add(t, count) for t, count in
                   [(0, 0), (2, 2), (4, 6), (7, 9)]]
        self.assertIsNone(results[2].average_speed)
        self.assertAlmostEqual(results[3].average_speed, 7 / 5)


if __name__ == "__main__":
    unittest.main()
