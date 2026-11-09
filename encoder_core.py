"""Protocol and measurement calculations for the Briter encoder dashboard."""

import math
from collections import deque
from dataclasses import dataclass


RESOLUTION = 131072
WHEEL_CIRCUMFERENCE_M = 0.2


def calculate_crc(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc.to_bytes(2, "little")


def position_request(address):
    if not 1 <= address <= 255:
        raise ValueError("站号必须在 1 到 255 之间")
    frame = bytes((address, 3, 0, 0, 0, 2))
    return frame + calculate_crc(frame)


def parse_position_response(response, address):
    if len(response) < 5 or calculate_crc(response[:-2]) != response[-2:]:
        raise ValueError("响应长度不足或 CRC 校验失败")
    if response[0] != address:
        raise ValueError("响应站号不匹配")
    if response[1] == 0x83:
        if len(response) != 5:
            raise ValueError("异常响应长度错误")
        raise ValueError(f"编码器返回 Modbus 异常码 0x{response[2]:02X}")
    if len(response) != 9 or response[1:3] != b"\x03\x04":
        raise ValueError("位置响应格式错误")
    return int.from_bytes(response[3:7], "big")


@dataclass(frozen=True)
class Measurement:
    elapsed: float
    count: int
    displacement: float
    travel: float
    speed: float | None
    smooth_speed: float | None
    average_speed: float | None
    acceleration: float | None
    smooth_acceleration: float | None
    average_acceleration: float | None


def _interpolate(history, target):
    """Interpolate a value at target, retaining the point just before it."""
    while len(history) > 2 and history[1][0] <= target:
        history.popleft()
    if history[0][0] > target:
        return None
    t0, v0 = history[0]
    if t0 == target:
        return v0
    t1, v1 = history[1]
    return v0 + (v1 - v0) * (target - t0) / (t1 - t0)


class MeasurementCalculator:
    def __init__(self, *, circumference=WHEEL_CIRCUMFERENCE_M,
                 resolution=RESOLUTION, hardware_turns=None, max_speed=5.0,
                 smoothing_seconds=0.25, average_seconds=3.0):
        if not math.isfinite(circumference) or circumference <= 0 or resolution <= 0:
            raise ValueError("周长和分辨率必须大于零")
        if hardware_turns is not None and (
            not isinstance(hardware_turns, int) or hardware_turns <= 0
            or hardware_turns * resolution > 2**32
        ):
            raise ValueError("硬件圈数必须为正整数，且总计数不能超过 32 位")
        if not all(math.isfinite(x) and x > 0 for x in
                   (max_speed, smoothing_seconds, average_seconds)):
            raise ValueError("速度上限和时间参数必须为有限正数")
        self.metres_per_count = circumference / resolution
        self.modulus = hardware_turns * resolution if hardware_turns else None
        self.max_speed = max_speed
        self.smoothing_seconds = smoothing_seconds
        self.average_seconds = average_seconds
        self.start_time = self.last_time = self.last_count = None
        self.distance = self.travel = 0.0
        self.last_speed = self.last_smooth_speed = self.last_dt = None
        self.distance_history = deque()
        self.speed_history = deque()

    def add(self, timestamp, count):
        if not math.isfinite(timestamp) or not isinstance(count, int) or not 0 <= count < 2**32:
            raise ValueError("时间戳或位置计数无效")
        if self.modulus and count >= self.modulus:
            raise ValueError("位置超出配置的硬件圈数量程，请确认圈数")
        if self.start_time is None:
            self.start_time = self.last_time = timestamp
            self.last_count = count
            self.distance_history.append((0.0, 0.0))
            return Measurement(0.0, count, 0.0, 0.0, None, None, None, None, None, None)

        dt = timestamp - self.last_time
        if dt <= 0:
            raise ValueError("采样时间未递增")
        delta = count - self.last_count
        if self.modulus:
            # Above half a turn range, direction and number of wraps are ambiguous.
            if self.max_speed * dt >= self.modulus * self.metres_per_count / 2:
                raise ValueError("采样间隔过长，无法判断是否跨越计数量程")
            delta = (delta + self.modulus // 2) % self.modulus - self.modulus // 2
        distance_delta = delta * self.metres_per_count
        speed = distance_delta / dt
        if abs(speed) > self.max_speed:
            raise ValueError("速度超过配置的合理上限；可能是计数回绕、设备复位或通信异常")

        elapsed = timestamp - self.start_time
        self.distance += distance_delta
        self.travel += abs(distance_delta)
        alpha = -math.expm1(-dt / self.smoothing_seconds)
        smooth = speed if self.last_smooth_speed is None else (
            self.last_smooth_speed + alpha * (speed - self.last_smooth_speed)
        )
        acceleration = None if self.last_speed is None else (
            (speed - self.last_speed) / ((dt + self.last_dt) / 2)
        )
        smooth_acceleration = None if self.last_smooth_speed is None else (
            smooth - self.last_smooth_speed
        ) / dt
        self.distance_history.append((elapsed, self.distance))
        self.speed_history.append((elapsed, smooth))
        target = elapsed - self.average_seconds
        average_speed = average_acceleration = None
        if target >= 0:
            old_distance = _interpolate(self.distance_history, target)
            if old_distance is not None:
                average_speed = (self.distance - old_distance) / self.average_seconds
            old_speed = _interpolate(self.speed_history, target)
            if old_speed is not None:
                average_acceleration = (smooth - old_speed) / self.average_seconds

        self.last_count, self.last_time = count, timestamp
        self.last_speed, self.last_smooth_speed = speed, smooth
        self.last_dt = dt
        return Measurement(elapsed, count, self.distance, self.travel, speed, smooth,
                           average_speed, acceleration, smooth_acceleration,
                           average_acceleration)
