"""SiFive-compatible PLIC.  Context 0 = hart0 M-mode, context 1 = hart0 S-mode."""

MEIP, SEIP = 1 << 11, 1 << 9
NSRC = 32
CTX_BITS = (MEIP, SEIP)


class PLIC:
    size = 0x600000

    def __init__(self, machine):
        self.m = machine
        self.priority = [0] * NSRC
        self.lines = 0          # current level of each source
        self.pending = 0
        self.claimed = 0
        self.enable = [0, 0]
        self.threshold = [0, 0]

    def set_irq(self, n, level):
        bit = 1 << n
        if level:
            if self.lines & bit:
                return
            self.lines |= bit
            if not self.claimed & bit:
                self.pending |= bit
        else:
            if not self.lines & bit:
                return
            self.lines &= ~bit
            self.pending &= ~bit
        self.update()

    def _best(self, ctx):
        cand = self.pending & self.enable[ctx] & ~self.claimed
        best, best_prio = 0, self.threshold[ctx]
        n = 1
        cand >>= 1
        while cand:
            if cand & 1 and self.priority[n] > best_prio:
                best, best_prio = n, self.priority[n]
            cand >>= 1
            n += 1
        return best

    def update(self):
        cpu = self.m.cpu
        for ctx in (0, 1):
            if self._best(ctx):
                cpu.mip |= CTX_BITS[ctx]
            else:
                cpu.mip &= ~CTX_BITS[ctx]

    def read(self, off, size):
        if off < 0x1000:
            n = off >> 2
            return self.priority[n] if n < NSRC else 0
        if off == 0x1000:
            return self.pending & 0xFFFFFFFF
        if 0x2000 <= off < 0x200000:
            ctx, word = divmod(off - 0x2000, 0x80)
            if ctx < 2 and word == 0:
                return self.enable[ctx]
            return 0
        if off >= 0x200000:
            ctx, reg = divmod(off - 0x200000, 0x1000)
            if ctx >= 2:
                return 0
            if reg == 0:
                return self.threshold[ctx]
            if reg == 4:                      # claim
                n = self._best(ctx)
                if n:
                    self.pending &= ~(1 << n)
                    self.claimed |= 1 << n
                    self.update()
                return n
        return 0

    def write(self, off, size, v):
        v &= 0xFFFFFFFF
        if off < 0x1000:
            n = off >> 2
            if 0 < n < NSRC:
                self.priority[n] = v & 7
        elif 0x2000 <= off < 0x200000:
            ctx, word = divmod(off - 0x2000, 0x80)
            if ctx < 2 and word == 0:
                self.enable[ctx] = v & ~1
        elif off >= 0x200000:
            ctx, reg = divmod(off - 0x200000, 0x1000)
            if ctx >= 2:
                return
            if reg == 0:
                self.threshold[ctx] = v & 7
            elif reg == 4:                    # complete
                if 0 < v < NSRC:
                    bit = 1 << v
                    self.claimed &= ~bit
                    if self.lines & bit:
                        self.pending |= bit
        self.update()
