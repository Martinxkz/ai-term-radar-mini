"""Online extrema and recent signed-speed fluctuation for one test run."""

from collections import deque


class SpeedStats:
    def __init__(self, window_seconds=5.0):
        self.window_seconds = window_seconds
        self.minimum = None
        self.maximum = None
        self.window = deque()

    def add(self, elapsed, speed):
        if speed is None:
            return
        if self.minimum is None or speed < self.minimum[1]:
            self.minimum = (elapsed, speed)
        if self.maximum is None or speed > self.maximum[1]:
            self.maximum = (elapsed, speed)
        self.window.append((elapsed, speed))
        while self.window and self.window[0][0] < elapsed - self.window_seconds:
            self.window.popleft()

    def fluctuation(self):
        if not self.window:
            return None
        speeds = (value for _, value in self.window)
        first = next(speeds)
        low = high = first
        for value in speeds:
            low, high = min(low, value), max(high, value)
        return low, high

    def run_summary(self, elapsed, displacement):
        if elapsed <= 0 or self.minimum is None:
            return None
        average = displacement / elapsed
        return average, self.minimum[1] - average, self.maximum[1] - average

    def run_deviation_percent(self, elapsed, displacement):
        summary = self.run_summary(elapsed, displacement)
        if summary is None or summary[0] == 0:
            return None
        return (summary[1] / abs(summary[0]) * 100,
                summary[2] / abs(summary[0]) * 100)
