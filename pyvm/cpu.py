"""RV64GC hart: architectural state, CSRs, traps, Sv39 MMU and the run loop.

Performance notes (this is the hot part of the VM):

* Guest RAM is an anonymous mmap viewed through typed memoryviews, so an
  aligned load/store is a single index operation.
* Data TLBs are plain dicts ``vpn -> (ram_offset - vaddr)``, one dict per
  (effective privilege, SUM, MXR) combination, so switching U<->S or toggling
  SUM never needs a flush.  Only RAM pages are cached; MMIO always takes the
  slow path.
* Instruction fetch goes through per-physical-page lists of decoded handlers.
  Pages that hold decoded code are kept out of the write TLB, so any store to
  them takes the slow path, which throws the decoded page away (self-modifying
  code, page reuse, DMA ...).
"""

from .errors import Trap, Break, Refetch, VMError
from . import decode as _decode

M64 = (1 << 64) - 1
RAM_BASE = 0x80000000

# mstatus bits
SIE, MIE, SPIE, MPIE, SPP = 1 << 1, 1 << 3, 1 << 5, 1 << 7, 1 << 8
MPP = 3 << 11
FS = 3 << 13
MPRV, SUM, MXR, TVM, TW, TSR = 1 << 17, 1 << 18, 1 << 19, 1 << 20, 1 << 21, 1 << 22
SD = 1 << 63
UXL_SXL = (2 << 32) | (2 << 34)
MSTATUS_WMASK = SIE | MIE | SPIE | MPIE | SPP | MPP | FS | MPRV | SUM | MXR | TVM | TW | TSR
SSTATUS_WMASK = SIE | SPIE | SPP | FS | SUM | MXR
SSTATUS_RMASK = SSTATUS_WMASK | (3 << 32)

# interrupt bits
SSIP, MSIP, STIP, MTIP, SEIP, MEIP = 1 << 1, 1 << 3, 1 << 5, 1 << 7, 1 << 9, 1 << 11
S_INTS = SSIP | STIP | SEIP
IRQ_ORDER = (11, 3, 7, 9, 1, 5)          # MEI, MSI, MTI, SEI, SSI, STI

PPN_MASK = (1 << 44) - 1
CODE_STORE_LIMIT = 256          # slow-path stores before a code page is demoted to data
MISA = (2 << 62) | sum(1 << (ord(c) - ord('A')) for c in 'ACDFIMSU')

CAUSE_NAMES = {
    0: 'instruction address misaligned', 1: 'instruction access fault',
    2: 'illegal instruction', 3: 'breakpoint', 4: 'load address misaligned',
    5: 'load access fault', 6: 'store/AMO address misaligned', 7: 'store/AMO access fault',
    8: 'ecall from U', 9: 'ecall from S', 11: 'ecall from M',
    12: 'instruction page fault', 13: 'load page fault', 15: 'store/AMO page fault',
}

ABI = ['zero', 'ra', 'sp', 'gp', 'tp', 't0', 't1', 't2', 's0', 's1', 'a0', 'a1', 'a2',
       'a3', 'a4', 'a5', 'a6', 'a7', 's2', 's3', 's4', 's5', 's6', 's7', 's8', 's9',
       's10', 's11', 't3', 't4', 't5', 't6']


class CPU:
    def __init__(self, machine):
        self.m = machine
        self.x = [0] * 33          # x[32] is the junk sink for writes to x0
        self.f = [0] * 32
        self.pc = 0
        self.priv = 3
        self.fflags = 0
        self.frm = 0
        self.mstatus = UXL_SXL
        self.medeleg = self.mideleg = 0
        self.mie = self.mip = 0
        self.mtvec = self.mscratch = self.mepc = self.mcause = self.mtval = 0
        self.stvec = self.sscratch = self.sepc = self.scause = self.stval = 0
        self.satp = 0
        self.mcounteren = self.scounteren = 0
        self.menvcfg = self.senvcfg = 0
        self.mcountinhibit = 0
        self.pmpcfg = [0] * 16
        self.pmpaddr = [0] * 64
        self.mtime = 0              # timer ticks
        self.instret = 0
        self.stimecmp = M64         # supervisor timer used by the built-in SBI
        self.reservation = -1
        self.wfi = False
        self.sbi = None             # built-in firmware (None = guest M-mode code)

        self.ram_size = machine.ram_size
        mv = memoryview(machine.ram)
        self.mB = mv
        self.mb = mv.cast('b')
        self.mH = mv.cast('H')
        self.mh = mv.cast('h')
        self.mI = mv.cast('I')
        self.mi = mv.cast('i')
        self.mQ = mv.cast('Q')

        # TLBs / decoded code
        self.tlb_sets = {}
        self.itlbs, self.rtlbs, self.wtlbs = [], [], []
        self.super_regions = {}
        self.code_pages = {}
        self.itlb = self.rtlb = self.wtlb = None
        self.set_tlb_cells = None       # installed by the decoder
        self.update_tlbs()

        cpu = self

        def refetch(pc):
            cpu.pc = pc
            raise Refetch()
        self.refetch_list = [refetch] * 4096

        self.csrs = self._build_csrs()
        self.decode = _decode.build(self)
        self.update_tlbs()

    # ------------------------------------------------------------------ run loop
    def run_batch(self, n):
        """Execute up to n instructions.  Returns the number executed."""
        pc = self.pc
        itlb = self.itlb
        vpn = -1
        page = None
        i = 0
        try:
            for i in range(n):
                if pc >> 12 != vpn:
                    page = itlb.get(pc >> 12)
                    if page is None:
                        page = self.fetch_page(pc)
                    vpn = pc >> 12
                pc = page[pc & 4095](pc)
        except Refetch:
            return i
        except Break:
            return i + 1
        except Trap as t:
            self.trap(t.cause, t.tval, pc)
            return i
        self.pc = pc
        return n

    def fetch_page(self, pc):
        pa, level = self.translate(pc, 0)
        o = pa - RAM_BASE
        if o < 0 or o >= self.ram_size:
            raise Trap(1, pc)
        rp = o >> 12
        pg = self.code_pages.get(rp)
        if pg is None:
            pg = self.new_code_page(rp)
            for d in self.wtlbs:        # stores to this page must now take the slow path
                d.clear()
        vpn = pc >> 12
        self.itlb[vpn] = pg
        if level:
            self._note_super(vpn, level)
        return pg

    def new_code_page(self, rp):
        """Decoded-handler list for RAM page rp: slot [pc & 4095] holds the
        handler for the instruction at pc; slot 4096 holds the page's RAM offset.
        Slots start out as a stub that decodes the instruction on first use."""
        decode_fill = self.decode_fill

        def stub(pc):
            return decode_fill(pc, pg)(pc)
        pg = [stub] * 4096
        pg += [rp << 12, stub, 0]       # 4096: RAM offset, 4097: stub, 4098: store count
        self.code_pages[rp] = pg
        return pg

    def decode_fill(self, pc, page):
        off = pc & 4095
        o = page[4096] + off
        lo = self.mH[o >> 1]
        if (lo & 3) != 3:
            h = self.decode(lo)
        elif off == 4094:
            # 32-bit instruction straddling a page boundary: never cached
            pa, _ = self.translate((pc + 2) & M64, 0)
            o2 = pa - RAM_BASE
            if o2 < 0 or o2 >= self.ram_size:
                raise Trap(1, (pc + 2) & M64)
            return self.decode(lo | (self.mH[o2 >> 1] << 16))
        else:
            h = self.decode(lo | (self.mH[(o >> 1) + 1] << 16))
        page[off] = h
        return h

    def invalidate_code(self, rp):
        pg = self.code_pages.pop(rp)
        pg[:4096] = self.refetch_list
        for d in self.itlbs:
            d.clear()

    # ------------------------------------------------------------------ TLB management
    def update_tlbs(self):
        """Select the TLB dicts matching the current privilege/mstatus."""
        priv = self.priv
        st = self.mstatus
        ts = self.tlb_sets
        d = ts.get(priv)
        if d is None:
            d = ts[priv] = {}
            self.itlbs.append(d)
        self.itlb = d
        dp = ((st >> 11) & 3) if (priv == 3 and st & MPRV) else priv
        su = 1 if (dp == 1 and st & SUM) else 0
        mxr = 1 if st & MXR else 0
        k = 16 + (dp << 2) + (su << 1) + mxr
        d = ts.get(k)
        if d is None:
            d = ts[k] = {}
            self.rtlbs.append(d)
        self.rtlb = d
        k = 32 + (dp << 2) + (su << 1)
        d = ts.get(k)
        if d is None:
            d = ts[k] = {}
            self.wtlbs.append(d)
        self.wtlb = d
        if self.set_tlb_cells is not None:
            self.set_tlb_cells(self.rtlb, d)

    def flush_tlb(self, va=None):
        if va is None:
            for d in self.tlb_sets.values():
                d.clear()
            self.super_regions.clear()
            return
        vpn = (va & M64) >> 12
        vpns = [vpn]
        for level in (1, 2):
            lst = self.super_regions.pop((level, vpn >> (9 * level)), None)
            if lst:
                vpns.extend(lst)
        for d in self.tlb_sets.values():
            for v in vpns:
                d.pop(v, None)

    def _note_super(self, vpn, level):
        key = (level, vpn >> (9 * level))
        lst = self.super_regions.get(key)
        if lst is None:
            self.super_regions[key] = [vpn]
        else:
            lst.append(vpn)

    # ------------------------------------------------------------------ MMU
    def translate(self, a, acc):
        """Virtual -> physical.  acc: 0 fetch, 1 load, 2 store.  -> (pa, level)"""
        priv = self.priv
        st = self.mstatus
        if acc and priv == 3 and st & MPRV:
            priv = (st >> 11) & 3
        satp = self.satp
        if priv == 3 or not satp:
            return a, 0
        fault = 12 if acc == 0 else (13 if acc == 1 else 15)
        top = a >> 38
        if top and top != 0x3FFFFFF:
            raise Trap(fault, a)
        pt = (satp & PPN_MASK) << 12
        mQ = self.mQ
        size = self.ram_size
        level = 2
        while True:
            pte_addr = pt + (((a >> (12 + 9 * level)) & 511) << 3) - RAM_BASE
            if pte_addr < 0 or pte_addr >= size:
                raise Trap(1 if acc == 0 else (5 if acc == 1 else 7), a)
            pte = mQ[pte_addr >> 3]
            if not pte & 1 or (pte & 6) == 4:
                raise Trap(fault, a)
            if pte & 0xA:
                break
            level -= 1
            if level < 0:
                raise Trap(fault, a)
            pt = ((pte >> 10) & PPN_MASK) << 12
        # leaf permission checks
        if acc == 0:
            if not pte & 8:
                raise Trap(fault, a)
            if pte & 0x10:
                if priv != 0:
                    raise Trap(fault, a)
            elif priv == 0:
                raise Trap(fault, a)
        else:
            if acc == 1:
                if not (pte & 2 or (st & MXR and pte & 8)):
                    raise Trap(fault, a)
            elif not pte & 4:
                raise Trap(fault, a)
            if pte & 0x10:
                if priv == 1 and not st & SUM:
                    raise Trap(fault, a)
            elif priv == 0:
                raise Trap(fault, a)
        ppn = (pte >> 10) & PPN_MASK
        if level and ppn & ((1 << (9 * level)) - 1):
            raise Trap(fault, a)                     # misaligned superpage
        if not pte & 0x40 or (acc == 2 and not pte & 0x80):
            pte |= 0x40 | (0x80 if acc == 2 else 0)  # hardware A/D update
            mQ[pte_addr >> 3] = pte
        return (ppn << 12) | (a & ((1 << (12 + 9 * level)) - 1)), level

    # ------------------------------------------------------------------ slow memory paths
    def load_slow(self, a, size):
        """Load `size` bytes at virtual address a; returns the unsigned value."""
        if a & (size - 1):
            return self._load_misaligned(a, size)
        pa, level = self.translate(a, 1)
        o = pa - RAM_BASE
        if 0 <= o < self.ram_size:
            vpn = a >> 12
            self.rtlb[vpn] = o - a
            if level:
                self._note_super(vpn, level)
            if size == 8:
                return self.mQ[o >> 3]
            if size == 4:
                return self.mI[o >> 2]
            if size == 2:
                return self.mH[o >> 1]
            return self.mB[o]
        v = self.m.mmio_read(pa, size)
        if v is None:
            raise Trap(5, a)
        return v

    def store_slow(self, a, size, v):
        if a & (size - 1):
            return self._store_misaligned(a, size, v)
        pa, level = self.translate(a, 2)
        o = pa - RAM_BASE
        if 0 <= o < self.ram_size:
            rp = o >> 12
            pg = self.code_pages.get(rp)
            if pg is not None:
                if pg[4098] < CODE_STORE_LIMIT:
                    # store into a page holding decoded code: forget only the
                    # handlers overlapping the written bytes, keep the page
                    pg[4098] += 1
                    off = o & 4095
                    stub = pg[4097]
                    for k in range(max((off - 2) & ~1, 0), min(off + size, 4096), 2):
                        pg[k] = stub
                    self._write_ram(o, size, v)
                    return
                self.invalidate_code(rp)    # page is mostly data now: demote it
            vpn = a >> 12
            self.wtlb[vpn] = o - a
            if level:
                self._note_super(vpn, level)
            self._write_ram(o, size, v)
            return
        if not self.m.mmio_write(pa, size, v):
            raise Trap(7, a)

    def _write_ram(self, o, size, v):
        if size == 8:
            self.mQ[o >> 3] = v
        elif size == 4:
            self.mI[o >> 2] = v
        elif size == 2:
            self.mH[o >> 1] = v
        else:
            self.mB[o] = v

    def amo_offset(self, a):
        """RAM offset for an atomic read-modify-write at (aligned) address a."""
        d = self.wtlb.get(a >> 12)
        if d is not None:
            return a + d
        pa, level = self.translate(a, 2)
        o = pa - RAM_BASE
        if not 0 <= o < self.ram_size:
            raise Trap(7, a)
        rp = o >> 12
        if rp in self.code_pages:
            self.invalidate_code(rp)
        vpn = a >> 12
        self.wtlb[vpn] = o - a
        if level:
            self._note_super(vpn, level)
        return o

    def _load_misaligned(self, a, size):
        v = 0
        for k in range(size):
            ad = (a + k) & M64
            pa, _ = self.translate(ad, 1)
            o = pa - RAM_BASE
            if not 0 <= o < self.ram_size:
                raise Trap(5, ad)
            v |= self.mB[o] << (8 * k)
        return v

    def _store_misaligned(self, a, size, v):
        offs = []
        for k in range(size):               # translate everything before writing
            ad = (a + k) & M64
            pa, _ = self.translate(ad, 2)
            o = pa - RAM_BASE
            if not 0 <= o < self.ram_size:
                raise Trap(7, ad)
            offs.append(o)
        for k, o in enumerate(offs):
            if (o >> 12) in self.code_pages:
                self.invalidate_code(o >> 12)
            self.mB[o] = (v >> (8 * k)) & 0xFF

    # ------------------------------------------------------------------ traps
    def trap(self, cause, tval, pc):
        self.reservation = -1
        priv = self.priv
        is_irq = cause >> 63
        code = cause & 0xFFF
        deleg = self.mideleg if is_irq else self.medeleg
        st = self.mstatus
        if priv <= 1 and (deleg >> code) & 1:
            self.scause = cause
            self.sepc = pc
            self.stval = tval & M64
            self.mstatus = (st & ~(SPIE | SPP | SIE)) | ((st & SIE) << 4) | (priv << 8)
            tv = self.stvec
            self.pc = (tv & ~3) + (4 * code if (is_irq and tv & 1) else 0)
            self.priv = 1
        else:
            if self.sbi is not None:
                if cause == 9:
                    self.sbi.call(self)
                    self.pc = (pc + 4) & M64
                    return
                raise VMError('unhandled trap to M-mode: %s (cause=%#x tval=%#x pc=%#x)'
                              % (CAUSE_NAMES.get(code, '?'), cause, tval & M64, pc))
            self.mcause = cause
            self.mepc = pc
            self.mtval = tval & M64
            self.mstatus = (st & ~(MPIE | MPP | MIE)) | ((st & MIE) << 4) | (priv << 11)
            tv = self.mtvec
            self.pc = (tv & ~3) + (4 * code if (is_irq and tv & 1) else 0)
            self.priv = 3
        self.update_tlbs()

    def check_irq(self, take=True):
        """Take (or just report) a pending, enabled interrupt."""
        p = self.mip & self.mie
        if not p:
            return False
        if take:
            self.wfi = False
        priv = self.priv
        st = self.mstatus
        pend = p & ~self.mideleg
        if not (pend and (priv < 3 or st & MIE)):
            pend = p & self.mideleg
            if not (pend and (priv == 0 or (priv == 1 and st & SIE))):
                return False
        if take:
            for code in IRQ_ORDER:
                if (pend >> code) & 1:
                    self.trap((1 << 63) | code, 0, self.pc)
                    break
        return True

    # ------------------------------------------------------------------ privileged instructions
    def op_sret(self, pc, il, insn):
        priv = self.priv
        st = self.mstatus
        if priv == 0 or (priv == 1 and st & TSR):
            raise Trap(2, insn)
        spp = (st >> 8) & 1
        st = (st & ~(SIE | SPP | MPRV)) | ((st >> 4) & SIE) | SPIE
        self.mstatus = st
        self.priv = spp
        self.pc = self.sepc
        self.update_tlbs()
        raise Break()

    def op_mret(self, pc, il, insn):
        if self.priv != 3:
            raise Trap(2, insn)
        st = self.mstatus
        mpp = (st >> 11) & 3
        st = (st & ~(MIE | MPP)) | ((st >> 4) & MIE) | MPIE
        if mpp != 3:
            st &= ~MPRV
        self.mstatus = st
        self.priv = mpp
        self.pc = self.mepc
        self.update_tlbs()
        raise Break()

    def op_wfi(self, pc, il, insn):
        priv = self.priv
        if priv == 0 or (priv == 1 and self.mstatus & TW):
            raise Trap(2, insn)
        self.pc = pc + il
        if not (self.mip & self.mie):
            self.wfi = True
        raise Break()

    def op_sfence_vma(self, pc, il, insn, rs1, rs2):
        priv = self.priv
        if priv == 0 or (priv == 1 and self.mstatus & TVM):
            raise Trap(2, insn)
        self.flush_tlb(self.x[rs1] if rs1 else None)
        self.pc = pc + il
        raise Break()

    # ------------------------------------------------------------------ CSRs
    def csr_op(self, csr, op, v, do_write, insn):
        """op: 1 = rw, 2 = rs, 3 = rc.  Returns (old value, need_break)."""
        if ((csr >> 8) & 3) > self.priv or (do_write and (csr >> 10) == 3):
            raise Trap(2, insn)
        ent = self.csrs.get(csr)
        if ent is None:
            raise Trap(2, insn)
        rd, wr = ent
        old = rd(insn)
        if not do_write:
            return old, False
        if op == 1:
            new = v
        elif op == 2:
            new = old | v
        else:
            new = old & ~v
        return old, wr(new & M64, insn)

    def _build_csrs(self):
        cpu = self
        c = {}

        def ignore(v, insn):
            return False

        def const(val):
            return (lambda insn: val), ignore

        def fp_check(insn):
            if not cpu.mstatus & FS:
                raise Trap(2, insn)

        # --- floating point
        def r_fflags(insn):
            fp_check(insn)
            return cpu.fflags

        def w_fflags(v, insn):
            fp_check(insn)
            cpu.fflags = v & 0x1F
            cpu.mstatus |= FS
            return False

        def r_frm(insn):
            fp_check(insn)
            return cpu.frm

        def w_frm(v, insn):
            fp_check(insn)
            cpu.frm = v & 7
            cpu.mstatus |= FS
            return False

        def r_fcsr(insn):
            fp_check(insn)
            return (cpu.frm << 5) | cpu.fflags

        def w_fcsr(v, insn):
            fp_check(insn)
            cpu.frm = (v >> 5) & 7
            cpu.fflags = v & 0x1F
            cpu.mstatus |= FS
            return False
        c[0x001] = (r_fflags, w_fflags)
        c[0x002] = (r_frm, w_frm)
        c[0x003] = (r_fcsr, w_fcsr)

        # --- counters
        def counter(idx):
            def r(insn):
                p = cpu.priv
                if p < 3 and not (cpu.mcounteren >> idx) & 1:
                    raise Trap(2, insn)
                if p == 0 and not (cpu.scounteren >> idx) & 1:
                    raise Trap(2, insn)
                if idx == 1:
                    return cpu.mtime & M64
                if idx == 0 or idx == 2:
                    return cpu.instret & M64
                return 0
            return r
        for i in range(32):
            c[0xC00 + i] = (counter(i), ignore)

        def w_instret(v, insn):
            cpu.instret = v
            return False
        c[0xB00] = (lambda insn: cpu.instret & M64, w_instret)
        c[0xB02] = (lambda insn: cpu.instret & M64, w_instret)
        for i in range(3, 32):
            c[0xB00 + i] = const(0)
            c[0x320 + i] = const(0)          # mhpmevent3..31

        # --- supervisor
        def r_sstatus(insn):
            st = cpu.mstatus
            v = st & SSTATUS_RMASK
            if (st & FS) == FS:
                v |= SD
            return v

        def w_sstatus(v, insn):
            old = cpu.mstatus
            new = (old & ~SSTATUS_WMASK) | (v & SSTATUS_WMASK)
            cpu.mstatus = new
            if (old ^ new) & (SUM | MXR):
                cpu.update_tlbs()
            return cpu.check_irq(False)
        c[0x100] = (r_sstatus, w_sstatus)

        def w_sie(v, insn):
            dl = cpu.mideleg
            cpu.mie = (cpu.mie & ~dl) | (v & dl)
            return cpu.check_irq(False)
        c[0x104] = (lambda insn: cpu.mie & cpu.mideleg, w_sie)

        def w_stvec(v, insn):
            cpu.stvec = (v & ~3) | (v & 1)
            return False
        c[0x105] = (lambda insn: cpu.stvec, w_stvec)

        def w_scounteren(v, insn):
            cpu.scounteren = v & 0xFFFFFFFF
            return False
        c[0x106] = (lambda insn: cpu.scounteren, w_scounteren)

        def w_senvcfg(v, insn):
            cpu.senvcfg = v & 1
            return False
        c[0x10A] = (lambda insn: cpu.senvcfg, w_senvcfg)

        def w_sscratch(v, insn):
            cpu.sscratch = v
            return False
        c[0x140] = (lambda insn: cpu.sscratch, w_sscratch)

        def w_sepc(v, insn):
            cpu.sepc = v & ~1
            return False
        c[0x141] = (lambda insn: cpu.sepc, w_sepc)

        def w_scause(v, insn):
            cpu.scause = v
            return False
        c[0x142] = (lambda insn: cpu.scause, w_scause)

        def w_stval(v, insn):
            cpu.stval = v
            return False
        c[0x143] = (lambda insn: cpu.stval, w_stval)

        def w_sip(v, insn):
            mask = SSIP & cpu.mideleg
            cpu.mip = (cpu.mip & ~mask) | (v & mask)
            return cpu.check_irq(False)
        c[0x144] = (lambda insn: cpu.mip & cpu.mideleg, w_sip)

        def r_satp(insn):
            if cpu.priv == 1 and cpu.mstatus & TVM:
                raise Trap(2, insn)
            return cpu.satp

        def w_satp(v, insn):
            if cpu.priv == 1 and cpu.mstatus & TVM:
                raise Trap(2, insn)
            mode = v >> 60
            if mode == 0:
                cpu.satp = 0
            elif mode == 8:                   # Sv39 only; ASIDs not implemented
                cpu.satp = (8 << 60) | (v & PPN_MASK)
            else:
                return False                  # unsupported mode: write ignored
            cpu.flush_tlb()
            return True
        c[0x180] = (r_satp, w_satp)

        # --- machine
        def r_mstatus(insn):
            st = cpu.mstatus
            return st | SD if (st & FS) == FS else st

        def w_mstatus(v, insn):
            old = cpu.mstatus
            new = (old & ~MSTATUS_WMASK) | (v & MSTATUS_WMASK)
            if ((new >> 11) & 3) == 2:
                new = (new & ~MPP) | (old & MPP)
            cpu.mstatus = new
            if (old ^ new) & (MPRV | MPP | SUM | MXR):
                cpu.update_tlbs()
            return cpu.check_irq(False)
        c[0x300] = (r_mstatus, w_mstatus)
        c[0x301] = const(MISA)

        def w_medeleg(v, insn):
            cpu.medeleg = v & 0xB3FF
            return False
        c[0x302] = (lambda insn: cpu.medeleg, w_medeleg)

        def w_mideleg(v, insn):
            cpu.mideleg = v & S_INTS
            return cpu.check_irq(False)
        c[0x303] = (lambda insn: cpu.mideleg, w_mideleg)

        def w_mie(v, insn):
            cpu.mie = v & 0xAAA
            return cpu.check_irq(False)
        c[0x304] = (lambda insn: cpu.mie, w_mie)

        def w_mtvec(v, insn):
            cpu.mtvec = (v & ~3) | (v & 1)
            return False
        c[0x305] = (lambda insn: cpu.mtvec, w_mtvec)

        def w_mcounteren(v, insn):
            cpu.mcounteren = v & 0xFFFFFFFF
            return False
        c[0x306] = (lambda insn: cpu.mcounteren, w_mcounteren)

        def w_menvcfg(v, insn):
            cpu.menvcfg = v & 1
            return False
        c[0x30A] = (lambda insn: cpu.menvcfg, w_menvcfg)

        def w_mcountinhibit(v, insn):
            cpu.mcountinhibit = v & 0xFFFFFFFD
            return False
        c[0x320] = (lambda insn: cpu.mcountinhibit, w_mcountinhibit)

        def w_mscratch(v, insn):
            cpu.mscratch = v
            return False
        c[0x340] = (lambda insn: cpu.mscratch, w_mscratch)

        def w_mepc(v, insn):
            cpu.mepc = v & ~1
            return False
        c[0x341] = (lambda insn: cpu.mepc, w_mepc)

        def w_mcause(v, insn):
            cpu.mcause = v
            return False
        c[0x342] = (lambda insn: cpu.mcause, w_mcause)

        def w_mtval(v, insn):
            cpu.mtval = v
            return False
        c[0x343] = (lambda insn: cpu.mtval, w_mtval)

        def w_mip(v, insn):
            mask = S_INTS
            cpu.mip = (cpu.mip & ~mask) | (v & mask)
            return cpu.check_irq(False)
        c[0x344] = (lambda insn: cpu.mip, w_mip)

        def pmpcfg(idx):
            def w(v, insn):
                cpu.pmpcfg[idx] = v
                return False
            return (lambda insn: cpu.pmpcfg[idx]), w
        for i in range(0, 16, 2):
            c[0x3A0 + i] = pmpcfg(i)

        def pmpaddr(idx):
            def w(v, insn):
                cpu.pmpaddr[idx] = v & ((1 << 54) - 1)
                return False
            return (lambda insn: cpu.pmpaddr[idx]), w
        for i in range(64):
            c[0x3B0 + i] = pmpaddr(i)

        for csr in (0xF11, 0xF12, 0xF13, 0xF14, 0xF15):   # vendor/arch/imp/hart id, configptr
            c[csr] = const(0)
        # debug triggers: none implemented (tselect hard-wired, tdata1 type 0)
        c[0x7A0] = const(1)
        for csr in (0x7A1, 0x7A2, 0x7A3, 0x7A5):
            c[csr] = const(0)
        c[0x7A4] = const(1)                                # tinfo: type 0 only
        return c

    # ------------------------------------------------------------------ debugging
    def dump(self):
        lines = ['pc=%016x priv=%s' % (self.pc, 'USHM'[self.priv])]
        for i in range(0, 32, 4):
            lines.append('  '.join('%-4s=%016x' % (ABI[j], self.x[j]) for j in range(i, i + 4)))
        lines.append('mstatus=%016x mcause=%x mepc=%x mtval=%x' %
                     (self.mstatus, self.mcause, self.mepc, self.mtval))
        lines.append('sstatus=%016x scause=%x sepc=%x stval=%x satp=%x' %
                     (self.mstatus & SSTATUS_RMASK, self.scause, self.sepc, self.stval, self.satp))
        lines.append('mip=%x mie=%x mideleg=%x medeleg=%x stvec=%x' %
                     (self.mip, self.mie, self.mideleg, self.medeleg, self.stvec))
        return '\n'.join(lines)
