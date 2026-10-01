"""NS16550A UART (the serial console, Linux `ttyS0`)."""

from collections import deque

LSR_DR, LSR_THRE, LSR_TEMT = 0x01, 0x20, 0x40


class UART16550:
    size = 0x100

    def __init__(self, machine, irq):
        self.m = machine
        self.irq = irq
        self.rx = deque()
        self.ier = 0
        self.lcr = 0x03
        self.mcr = 0
        self.scr = 0
        self.fcr = 0
        self.dll = 0x0C
        self.dlm = 0
        self.thr_pending = False    # "THR empty" interrupt condition

    # ------------------------------------------------------------------ guest side
    def read(self, off, size):
        dlab = self.lcr & 0x80
        if off == 0:
            if dlab:
                return self.dll
            v = self.rx.popleft() if self.rx else 0
            self.update()
            return v
        if off == 1:
            return self.dlm if dlab else self.ier
        if off == 2:
            iir = self._iir()
            if iir == 0x02:
                self.thr_pending = False
                self.update()
            return iir | (0xC0 if self.fcr & 1 else 0)
        if off == 3:
            return self.lcr
        if off == 4:
            return self.mcr
        if off == 5:
            return (LSR_DR if self.rx else 0) | LSR_THRE | LSR_TEMT
        if off == 6:
            if self.mcr & 0x10:              # loopback
                m = self.mcr
                return ((m & 2) << 3) | ((m & 1) << 5) | ((m & 4) << 4) | ((m & 8) << 4)
            return 0xB0                      # DCD | DSR | CTS
        if off == 7:
            return self.scr
        return 0

    def write(self, off, size, v):
        v &= 0xFF
        dlab = self.lcr & 0x80
        if off == 0:
            if dlab:
                self.dll = v
                return
            if self.mcr & 0x10:
                self.rx.append(v)
            else:
                self.m.console.write(bytes((v,)))
            self.thr_pending = True
            self.update()
        elif off == 1:
            if dlab:
                self.dlm = v
                return
            old = self.ier
            self.ier = v & 0x0F
            if (self.ier & 2) and not (old & 2):
                self.thr_pending = True
            self.update()
        elif off == 2:
            self.fcr = v & 0xC9
            if v & 2:
                self.rx.clear()
            self.update()
        elif off == 3:
            self.lcr = v
        elif off == 4:
            self.mcr = v & 0x1F
        elif off == 7:
            self.scr = v

    def _iir(self):
        if (self.ier & 1) and self.rx:
            return 0x04
        if (self.ier & 2) and self.thr_pending:
            return 0x02
        return 0x01

    def update(self):
        level = bool(((self.ier & 1) and self.rx) or ((self.ier & 2) and self.thr_pending))
        self.m.plic.set_irq(self.irq, level)

    # ------------------------------------------------------------------ host side
    def poll(self, console):
        if len(self.rx) >= 16 or not console.has_input():
            return
        while len(self.rx) < 16:
            b = console.getc()
            if b is None:
                break
            self.rx.append(b)
        self.update()
