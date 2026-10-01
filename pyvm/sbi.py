"""Built-in SBI firmware, implemented in Python (replaces OpenSBI).

The kernel runs in S-mode; its `ecall`s arrive here instead of trapping into
guest M-mode code.  Implements SBI v1.0: the legacy extensions plus BASE,
TIME, IPI, RFENCE, HSM, SRST and DBCN.
"""

from .errors import Halt

M64 = (1 << 64) - 1
STIP = 1 << 5
SSIP = 1 << 1

SUCCESS, ERR_FAILED, ERR_NOT_SUPPORTED, ERR_INVALID_PARAM = 0, -1, -2, -3
ERR_ALREADY_AVAILABLE = -6

EXT_BASE = 0x10
EXT_TIME = 0x54494D45
EXT_IPI = 0x735049
EXT_RFENCE = 0x52464E43
EXT_HSM = 0x48534D
EXT_SRST = 0x53525354
EXT_DBCN = 0x4442434E
SUPPORTED = {EXT_BASE, EXT_TIME, EXT_IPI, EXT_RFENCE, EXT_HSM, EXT_SRST, EXT_DBCN,
             0, 1, 2, 3, 4, 5, 6, 7, 8}

IMPL_ID = 0x5059   # "PY" (not a registered implementation id)


class SBI:
    def __init__(self, machine):
        self.m = machine

    def call(self, cpu):
        x = cpu.x
        eid, fid = x[17], x[16]
        a0, a1, a2 = x[10], x[11], x[12]
        if eid <= 8:
            x[10] = self.legacy(cpu, eid, a0) & M64
            return
        err, val = self.dispatch(cpu, eid, fid, a0, a1, a2)
        x[10] = err & M64
        x[11] = val & M64

    # ------------------------------------------------------------------ legacy (v0.1)
    def legacy(self, cpu, eid, a0):
        if eid == 0:                       # set_timer
            self.set_timer(cpu, a0)
            return 0
        if eid == 1:                       # console_putchar
            self.m.console.write(bytes((a0 & 0xFF,)))
            return 0
        if eid == 2:                       # console_getchar
            b = self.m.console.getc()
            return -1 if b is None else b
        if eid == 3:                       # clear_ipi
            cpu.mip &= ~SSIP
            return 0
        if eid == 4:                       # send_ipi (only hart 0 exists)
            cpu.mip |= SSIP
            return 0
        if eid in (5, 6, 7):               # remote fences
            cpu.flush_tlb()
            return 0
        if eid == 8:                       # shutdown
            raise Halt(0, 'shutdown')
        return ERR_NOT_SUPPORTED

    def set_timer(self, cpu, t):
        cpu.stimecmp = t
        cpu.mip &= ~STIP

    # ------------------------------------------------------------------ v0.2+
    def dispatch(self, cpu, eid, fid, a0, a1, a2):
        if eid == EXT_BASE:
            if fid == 0:
                return SUCCESS, 1 << 24           # spec version 1.0
            if fid == 1:
                return SUCCESS, IMPL_ID
            if fid == 2:
                return SUCCESS, 0x10000
            if fid == 3:
                return SUCCESS, 1 if a0 in SUPPORTED else 0
            if fid in (4, 5, 6):                  # mvendorid / marchid / mimpid
                return SUCCESS, 0
            return ERR_NOT_SUPPORTED, 0
        if eid == EXT_TIME and fid == 0:
            self.set_timer(cpu, a0)
            return SUCCESS, 0
        if eid == EXT_IPI and fid == 0:
            if self._targets_hart0(a0, a1):
                cpu.mip |= SSIP
            return SUCCESS, 0
        if eid == EXT_RFENCE:
            if fid == 0:                          # remote_fence_i
                return SUCCESS, 0
            if fid <= 6:
                cpu.flush_tlb()
                return SUCCESS, 0
            return ERR_NOT_SUPPORTED, 0
        if eid == EXT_HSM:
            if fid == 0:                          # hart_start
                return (ERR_ALREADY_AVAILABLE if a0 == 0 else ERR_INVALID_PARAM), 0
            if fid == 1:                          # hart_stop
                return ERR_FAILED, 0
            if fid == 2:                          # hart_get_status
                return (SUCCESS, 0) if a0 == 0 else (ERR_INVALID_PARAM, 0)
            return ERR_NOT_SUPPORTED, 0
        if eid == EXT_SRST and fid == 0:
            if a0 == 0:
                raise Halt(0, 'shutdown')
            if a0 in (1, 2):
                raise Halt(0, 'reboot')
            return ERR_INVALID_PARAM, 0
        if eid == EXT_DBCN:
            con = self.m.console
            if fid == 0:                          # console_write(len, addr_lo, addr_hi)
                data = self.m.phys_read(a1 | (a2 << 32), a0)
                if data is None:
                    return ERR_INVALID_PARAM, 0
                con.write(data)
                return SUCCESS, len(data)
            if fid == 1:                          # console_read
                out = bytearray()
                while len(out) < a0:
                    b = con.getc()
                    if b is None:
                        break
                    out.append(b)
                if not self.m.phys_write(a1 | (a2 << 32), bytes(out)):
                    return ERR_INVALID_PARAM, 0
                return SUCCESS, len(out)
            if fid == 2:                          # console_write_byte
                con.write(bytes((a0 & 0xFF,)))
                return SUCCESS, 0
        return ERR_NOT_SUPPORTED, 0

    @staticmethod
    def _targets_hart0(mask, base):
        if base == M64:                           # all harts
            return True
        return base == 0 and (mask & 1)
