"""Core-local interruptor: msip, mtimecmp and mtime for hart 0."""

MSIP, MTIP = 1 << 3, 1 << 7
M64 = (1 << 64) - 1


class CLINT:
    size = 0x10000

    def __init__(self, machine):
        self.m = machine
        self.mtimecmp = M64

    @staticmethod
    def _get(value, off, size):
        return (value >> (8 * (off & 7))) & ((1 << (8 * size)) - 1)

    @staticmethod
    def _put(old, off, size, v):
        sh = 8 * (off & 7)
        mask = ((1 << (8 * size)) - 1) << sh
        return (old & ~mask) | ((v << sh) & mask)

    def read(self, off, size):
        cpu = self.m.cpu
        if off < 4:
            return 1 if cpu.mip & MSIP else 0
        if 0x4000 <= off < 0x4008:
            return self._get(self.mtimecmp, off, size)
        if 0xBFF8 <= off < 0xC000:
            return self._get(cpu.mtime, off, size)
        return 0

    def write(self, off, size, v):
        cpu = self.m.cpu
        if off < 4:
            if v & 1:
                cpu.mip |= MSIP
            else:
                cpu.mip &= ~MSIP
        elif 0x4000 <= off < 0x4008:
            self.mtimecmp = self._put(self.mtimecmp, off, size, v)
            self.update()
        elif 0xBFF8 <= off < 0xC000:
            cpu.mtime = self._put(cpu.mtime, off, size, v)
            self.update()

    def update(self):
        cpu = self.m.cpu
        if cpu.mtime >= self.mtimecmp:
            cpu.mip |= MTIP
        else:
            cpu.mip &= ~MTIP
