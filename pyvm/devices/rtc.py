"""Goldfish RTC (wall-clock time for the guest, Linux `rtc-goldfish`)."""

import time


class GoldfishRTC:
    size = 0x1000

    def __init__(self, machine):
        self.m = machine
        self.high = 0
        self.alarm = 0
        self.irq_enabled = 0

    def read(self, off, size):
        if off == 0x00:                       # TIME_LOW (latches TIME_HIGH)
            t = time.time_ns()
            self.high = t >> 32
            return t & 0xFFFFFFFF
        if off == 0x04:
            return self.high
        if off == 0x08:
            return self.alarm & 0xFFFFFFFF
        if off == 0x0C:
            return self.alarm >> 32
        if off == 0x10:
            return self.irq_enabled
        return 0

    def write(self, off, size, v):
        if off == 0x08:
            self.alarm = (self.alarm & ~0xFFFFFFFF) | (v & 0xFFFFFFFF)
        elif off == 0x0C:
            self.alarm = (self.alarm & 0xFFFFFFFF) | ((v & 0xFFFFFFFF) << 32)
        elif off == 0x10:
            self.irq_enabled = v & 1
