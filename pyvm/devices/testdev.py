"""SiFive test device ("sifive,test0"): poweroff / reboot via a register write."""

from ..errors import Halt


class TestDevice:
    size = 0x1000

    def __init__(self, machine):
        self.m = machine

    def read(self, off, size):
        return 0

    def write(self, off, size, v):
        if off != 0:
            return
        cmd = v & 0xFFFF
        if cmd == 0x5555:
            raise Halt(0, 'poweroff')
        if cmd == 0x3333:
            raise Halt(((v >> 16) & 0xFFFF) or 1, 'poweroff (fail)')
        if cmd == 0x7777:
            raise Halt(0, 'reboot')
