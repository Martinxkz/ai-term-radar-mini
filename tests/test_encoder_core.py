import math
import unittest

from encoder_core import (
    MeasurementCalculator, calculate_crc, parse_position_response, position_request,
)


class ProtocolTests(unittest.TestCase):
    def test_manual_position_example(self):
        self.assertEqual(position_request(1).hex(), "010300000002c40b")
        response = bytes.fromhex("0103040001763bcc40")
        self.assertEqual(parse_position_response(response, 1), 95803)

    def test_invalid_and_exception_responses(self):
        with self.assertRaisesRegex(ValueError, "CRC"):
            parse_position_response(bytes.fromhex("0103040001763bcc41"), 1)
        frame = bytes.fromhex("018302")
        with self.assertRaisesRegex(ValueError, "0x02"):
            parse_position_response(frame + calculate_crc(frame), 1)
        with self.assertRaisesRegex(ValueError, "站号"):
            parse_position_response(bytes.fromhex("0103040001763bcc40"), 2)


class MeasurementTests(unittest.TestCase):
    def test_average_speed_and_warmup_with_irregular_sampling(self):
        calc = MeasurementCalculator(resolution=10, circumference=1, max_speed=10)
        calc.add(100.0, 0)
        first = calc.add(101.0, 10)
        self.assertIsNone(first.average_speed)
        self.assertIsNone(first.acceleration)
        calc.add(102.0, 20)
        result = calc.add(104.0, 40)
        self.assertAlmostEqual(result.displacement, 4)
        self.assertAlmostEqual(result.speed, 1)
        self.assertAlmostEqual(result.smooth_speed, 1)
        self.assertAlmostEqual(result.average_speed, 1)
        self.assertAlmostEqual(result.average_acceleration, 0)

    def test_average_speed_interpolates_boundary(self):
        calc = MeasurementCalculator(resolution=10, circumference=1, max_speed=10)
        calc.add(0, 0)
        calc.add(2, 20)
        result = calc.add(4, 60)
        self.assertAlmostEqual(result.average_speed, 5 / 3)

    def test_speed_smoothing_and_acceleration(self):
        calc = MeasurementCalculator(resolution=10, circumference=1,
                                     max_speed=10, smoothing_seconds=1)
        calc.add(0, 0)
        calc.add(1, 10)
        result = calc.add(3, 50)
        smooth = 1 + (1 - math.exp(-2))
        self.assertAlmostEqual(result.speed, 2)
        self.assertAlmostEqual(result.smooth_speed, smooth)
        self.assertAlmostEqual(result.acceleration, 1 / 1.5)
        self.assertAlmostEqual(result.smooth_acceleration, (smooth - 1) / 2)

    def test_reverse_motion_and_cumulative_travel(self):
        calc = MeasurementCalculator(resolution=10, circumference=1, max_speed=10)
        calc.add(0, 10)
        calc.add(1, 20)
        result = calc.add(2, 15)
        self.assertAlmostEqual(result.speed, -0.5)
        self.assertAlmostEqual(result.displacement, 0.5)
        self.assertAlmostEqual(result.travel, 1.5)

    def test_known_modulus_wraps_both_directions(self):
        calc = MeasurementCalculator(resolution=10, circumference=1,
                                     hardware_turns=2, max_speed=0.5)
        calc.add(0, 19)
        forward = calc.add(1, 1)
        reverse = calc.add(2, 19)
        self.assertAlmostEqual(forward.speed, 0.2)
        self.assertAlmostEqual(reverse.speed, -0.2)
        self.assertAlmostEqual(reverse.travel, 0.4)

    def test_unknown_modulus_jump_is_rejected_if_above_limit(self):
        calc = MeasurementCalculator(resolution=10, circumference=1, max_speed=1)
        calc.add(0, 19)
        with self.assertRaisesRegex(ValueError, "回绕"):
            calc.add(1, 1)

    def test_ambiguous_gap_and_bad_configuration_are_rejected(self):
        calc = MeasurementCalculator(resolution=10, circumference=1,
                                     hardware_turns=2, max_speed=2)
        calc.add(0, 1)
        with self.assertRaisesRegex(ValueError, "采样间隔过长"):
            calc.add(1, 2)
        with self.assertRaises(ValueError):
            MeasurementCalculator(hardware_turns=32769)
        with self.assertRaises(ValueError):
            MeasurementCalculator(max_speed=float("nan"))

    def test_nonmonotonic_time_and_out_of_range_count_are_rejected(self):
        calc = MeasurementCalculator(hardware_turns=16)
        calc.add(1, 0)
        with self.assertRaisesRegex(ValueError, "采样时间"):
            calc.add(1, 1)
        with self.assertRaisesRegex(ValueError, "量程"):
            calc.add(2, 16 * 131072)


if __name__ == "__main__":
    unittest.main()
