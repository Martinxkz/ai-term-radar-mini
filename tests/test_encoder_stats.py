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

    def test_gap_resets_derivatives_and_average_window(self):
        calculator = MeasurementCalculator(resolution=1, circumference=1,
                                           max_speed=10, average_seconds=5)
        calculator.add(0, 0)
        calculator.add(1, 1)
        recovered = calculator.add(4, 4, gap=True)
        self.assertEqual(recovered.displacement, 4)
        self.assertEqual(recovered.travel, 4)
        self.assertIsNone(recovered.speed)
        after = calculator.add(5, 5)
        self.assertEqual(after.speed, 1)
        self.assertIsNone(after.acceleration)
        self.assertIsNone(after.average_speed)
        self.assertEqual(calculator.add(9, 9).average_speed, 1)


if __name__ == "__main__":
    unittest.main()
