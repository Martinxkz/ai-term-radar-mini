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
