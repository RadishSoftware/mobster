"""Fakes several test modules share."""


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class DeviceLease:
    def __init__(self, identity=None):
        self.identity, self.closed = identity, False

    def close(self):
        self.closed = True


class SimLease:
    def __init__(self, target=None):
        self.target, self.released = target, 0

    def release(self):
        self.released += 1
