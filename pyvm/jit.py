"""Region compiler: turns hot guest code into Python functions.

The per-instruction closures in decode.py cost one Python call and one trip
round the dispatch loop for every guest instruction.  This module removes most
of that for code that runs often:

* Tier 0.  The first time a block entry point runs, a *stub* is made for it:
  the decoded handlers of the straight-line run starting there (up to and
  including the first control transfer), executed in a small loop.  It costs
  about what the plain interpreter did, and counts how often it runs.

* Tier 1.  When an entry has run HOT times, its whole physical page is
  compiled into one Python function, a *region*: every hot entry of the page,
  plus every branch target, jump target and return address reachable from them
  inside the page, each the start of a sub-block.  Immediates are folded into
  constants, the fast path of every load and store is inlined, conditional
  branches become side exits, and moving between sub-blocks is a jump inside
  the function (``o = target; continue``) instead of a trip through the
  dispatcher.  A sub-block that branches back to its own start becomes a
  ``while`` loop with the guest registers in Python locals.

Regions are built from the page's bytes alone (see build_region), so building
them -- the slow part, ~100 us of ``compile()`` per block -- happens in a
worker process (jitworker.py) while the guest carries on in tier 0, and the
results are kept on disk keyed by the page's contents for the next run.

Regions are entered as ``blk(pc, budget) -> (next_pc, instructions_executed)``
and never depend on the virtual address they run at: everything pc-relative is
computed from the page base derived from ``pc``.

Exceptions stay precise without costing the fast path anything: each guest
instruction occupies its own source lines, so when a trap unwinds out of a
region the traceback's line number says which instruction faulted (and, in a
loop, which registers to write back from the frame's locals; see ``unwind``).
"""

import collections
import hashlib
import importlib.util
import marshal
import os
import struct
import subprocess
import sys
import types
import weakref
import zlib


M64 = (1 << 64) - 1
S64 = 1 << 63
M32 = 0xFFFFFFFF
HI32 = 0xFFFFFFFF00000000

HOT = 32                # tier-0 runs of an entry before its page is compiled
MAX_INSNS = 64          # guest instructions per sub-block
MAX_REGION = 1500       # guest instructions per region (bounds compile time)
MAX_INFLIGHT = 64       # builds outstanding at the workers (more wait in a backlog)
MAX_CODES = 8192        # compiled regions kept in memory for reuse

# Settings (pyvm.__main__ sets these from the command line before a Machine is
# made; the environment variables give the same control to other callers).
ENABLED = os.environ.get('PYVM_JIT', '1') != '0'
# background compiler processes: one per spare core, at most two (0: compile in-process)
WORKERS = int(os.environ.get('PYVM_JIT_WORKERS', min(2, (os.cpu_count() or 1) - 1)))
CACHE = os.environ.get('PYVM_JIT_CACHE', '1') != '0'
CACHE_MAX_BYTES = 256 << 20   # a cache file bigger than this is started afresh


def sx(v, bits):
    m = 1 << (bits - 1)
    return ((v & ((1 << bits) - 1)) ^ m) - m


# ---------------------------------------------------------------------- IR
# Decoded instruction tuples.  Register numbers are architectural (0 = x0).
#   ('alu', op, rd, rs1, rs2)        ('alui', op, rd, rs1, imm)
#   ('lui', rd, value)               ('auipc', rd, imm)
#   ('load', size, signed, rd, rs1, imm)
#   ('store', size, rs1, rs2, imm)   ('br', cond, rs1, rs2, imm)
#   ('jal', rd, imm)                 ('jalr', rd, rs1, imm)
#   ('nop',)                         ('call',)  -- anything else: run its handler
CALL = ('call',)
NOP = ('nop',)

_OP = {(0x00, 0): 'add', (0x20, 0): 'sub', (0x00, 1): 'sll', (0x00, 2): 'slt',
       (0x00, 3): 'sltu', (0x00, 4): 'xor', (0x00, 5): 'srl', (0x20, 5): 'sra',
       (0x00, 6): 'or', (0x00, 7): 'and'}
_MOP = ('mul', 'mulh', 'mulhsu', 'mulhu', 'div', 'divu', 'rem', 'remu')
_OP32 = {(0x00, 0): 'addw', (0x20, 0): 'subw', (0x00, 1): 'sllw', (0x00, 5): 'srlw',
         (0x20, 5): 'sraw'}
_MOP32 = {0: 'mulw', 4: 'divw', 5: 'divuw', 6: 'remw', 7: 'remuw'}
_IMM = {0: 'addi', 2: 'slti', 3: 'sltiu', 4: 'xori', 6: 'ori', 7: 'andi'}
_BR = {0: 'eq', 1: 'ne', 4: 'lt', 5: 'ge', 6: 'ltu', 7: 'geu'}
_LD = {0: (1, True), 1: (2, True), 2: (4, True), 3: (8, False),
       4: (1, False), 5: (2, False), 6: (4, False)}


def ir32(i):
    op = i & 0x7F
    rd = (i >> 7) & 31
    f3 = (i >> 12) & 7
    rs1 = (i >> 15) & 31
    rs2 = (i >> 20) & 31
    f7 = i >> 25
    if op == 0x13:
        if f3 == 1:
            return CALL if i >> 26 else ('alui', 'slli', rd, rs1, (i >> 20) & 63)
        if f3 == 5:
            f6 = i >> 26
            if f6 == 0:
                return ('alui', 'srli', rd, rs1, (i >> 20) & 63)
            if f6 == 0x10:
                return ('alui', 'srai', rd, rs1, (i >> 20) & 63)
            return CALL
        return ('alui', _IMM[f3], rd, rs1, sx(i >> 20, 12))
    if op == 0x03:
        if f3 == 7:
            return CALL
        size, signed = _LD[f3]
        return ('load', size, signed, rd, rs1, sx(i >> 20, 12))
    if op == 0x23:
        if f3 > 3:
            return CALL
        return ('store', 1 << f3, rs1, rs2, sx(((i >> 25) << 5) | rd, 12))
    if op == 0x33:
        if f7 == 1:
            return ('alu', _MOP[f3], rd, rs1, rs2)
        o = _OP.get((f7, f3))
        return ('alu', o, rd, rs1, rs2) if o else CALL
    if op == 0x63:
        c = _BR.get(f3)
        if c is None:
            return CALL
        imm = sx(((i >> 31) << 12) | (((i >> 7) & 1) << 11) |
                 (((i >> 25) & 0x3F) << 5) | (((i >> 8) & 0xF) << 1), 13)
        return ('br', c, rs1, rs2, imm)
    if op == 0x37:
        return ('lui', rd, sx(i & 0xFFFFF000, 32) & M64)
    if op == 0x17:
        return ('auipc', rd, sx(i & 0xFFFFF000, 32))
    if op == 0x6F:
        imm = sx(((i >> 31) << 20) | (((i >> 12) & 0xFF) << 12) |
                 (((i >> 20) & 1) << 11) | (((i >> 21) & 0x3FF) << 1), 21)
        return ('jal', rd, imm)
    if op == 0x67:
        return CALL if f3 else ('jalr', rd, rs1, sx(i >> 20, 12))
    if op == 0x1B:
        if f3 == 0:
            return ('alui', 'addiw', rd, rs1, sx(i >> 20, 12))
        if f3 == 1 and f7 == 0:
            return ('alui', 'slliw', rd, rs1, rs2)
        if f3 == 5 and f7 == 0:
            return ('alui', 'srliw', rd, rs1, rs2)
        if f3 == 5 and f7 == 0x20:
            return ('alui', 'sraiw', rd, rs1, rs2)
        return CALL
    if op == 0x3B:
        if f7 == 1:
            o = _MOP32.get(f3)
        else:
            o = _OP32.get((f7, f3))
        return ('alu', o, rd, rs1, rs2) if o else CALL
    if op == 0x0F and f3 in (0, 1):
        return NOP
    return CALL


def ir16(i):
    op = i & 3
    f3 = i >> 13
    if op == 0:
        rdp = 8 + ((i >> 2) & 7)
        rs1p = 8 + ((i >> 7) & 7)
        imm_w = ((i >> 7) & 0x38) | ((i >> 4) & 0x4) | ((i << 1) & 0x40)
        imm_d = ((i >> 7) & 0x38) | ((i << 1) & 0xC0)
        if f3 == 0:
            imm = ((i >> 7) & 0x30) | ((i >> 1) & 0x3C0) | ((i >> 4) & 0x4) | ((i >> 2) & 0x8)
            return ('alui', 'addi', rdp, 2, imm) if imm else CALL
        if f3 == 2:
            return ('load', 4, True, rdp, rs1p, imm_w)
        if f3 == 3:
            return ('load', 8, False, rdp, rs1p, imm_d)
        if f3 == 6:
            return ('store', 4, rs1p, rdp, imm_w)
        if f3 == 7:
            return ('store', 8, rs1p, rdp, imm_d)
        return CALL
    if op == 1:
        rd = (i >> 7) & 31
        imm6 = sx(((i >> 7) & 0x20) | ((i >> 2) & 0x1F), 6)
        if f3 == 0:
            return ('alui', 'addi', rd, rd, imm6)
        if f3 == 1:
            return ('alui', 'addiw', rd, rd, imm6) if rd else CALL
        if f3 == 2:
            return ('alui', 'addi', rd, 0, imm6)
        if f3 == 3:
            if rd == 2:
                imm = sx(((i >> 3) & 0x200) | ((i >> 2) & 0x10) | ((i << 1) & 0x40) |
                         ((i << 4) & 0x180) | ((i << 3) & 0x20), 10)
                return ('alui', 'addi', 2, 2, imm) if imm else CALL
            imm = sx(((i << 5) & 0x20000) | ((i << 10) & 0x1F000), 18)
            return ('lui', rd, imm & M64) if imm else CALL
        if f3 == 4:
            rdp = 8 + ((i >> 7) & 7)
            f2 = (i >> 10) & 3
            shamt = ((i >> 7) & 0x20) | ((i >> 2) & 0x1F)
            if f2 == 0:
                return ('alui', 'srli', rdp, rdp, shamt)
            if f2 == 1:
                return ('alui', 'srai', rdp, rdp, shamt)
            if f2 == 2:
                return ('alui', 'andi', rdp, rdp, imm6)
            rs2p = 8 + ((i >> 2) & 7)
            f = (i >> 5) & 3
            if not (i >> 12) & 1:
                return ('alu', ('sub', 'xor', 'or', 'and')[f], rdp, rdp, rs2p)
            if f == 0:
                return ('alu', 'subw', rdp, rdp, rs2p)
            if f == 1:
                return ('alu', 'addw', rdp, rdp, rs2p)
            return CALL
        if f3 == 5:
            imm = sx(((i >> 1) & 0x800) | ((i >> 7) & 0x10) | ((i >> 1) & 0x300) |
                     ((i << 2) & 0x400) | ((i >> 1) & 0x40) | ((i << 1) & 0x80) |
                     ((i >> 2) & 0xE) | ((i << 3) & 0x20), 12)
            return ('jal', 0, imm)
        rs1p = 8 + ((i >> 7) & 7)
        imm = sx(((i >> 4) & 0x100) | ((i >> 7) & 0x18) | ((i << 1) & 0xC0) |
                 ((i >> 2) & 0x6) | ((i << 3) & 0x20), 9)
        return ('br', 'eq' if f3 == 6 else 'ne', rs1p, 0, imm)
    if op == 2:
        rd = (i >> 7) & 31
        rs2 = (i >> 2) & 31
        if f3 == 0:
            return ('alui', 'slli', rd, rd, ((i >> 7) & 0x20) | ((i >> 2) & 0x1F))
        if f3 == 2:
            imm = ((i >> 7) & 0x20) | ((i >> 2) & 0x1C) | ((i << 4) & 0xC0)
            return ('load', 4, True, rd, 2, imm) if rd else CALL
        if f3 == 3:
            imm = ((i >> 7) & 0x20) | ((i >> 2) & 0x18) | ((i << 4) & 0x1C0)
            return ('load', 8, False, rd, 2, imm) if rd else CALL
        if f3 == 4:
            if not (i >> 12) & 1:
                if rs2 == 0:
                    return ('jalr', 0, rd, 0) if rd else CALL
                return ('alui', 'addi', rd, rs2, 0)
            if rs2 == 0:
                return ('jalr', 1, rd, 0) if rd else CALL
            return ('alu', 'add', rd, rd, rs2)
        if f3 == 6:
            return ('store', 4, 2, rs2, ((i >> 7) & 0x3C) | ((i >> 1) & 0xC0))
        if f3 == 7:
            return ('store', 8, 2, rs2, ((i >> 7) & 0x38) | ((i >> 1) & 0x1C0))
        return CALL
    return CALL


_IR_CACHE = {}


def ir(insn):
    r = _IR_CACHE.get(insn)
    if r is None:
        r = ir32(insn) if (insn & 3) == 3 else ir16(insn)
        _IR_CACHE[insn] = r
    return r


ENDS = ('br', 'jal', 'jalr', 'call')       # control transfers end a tier-0 run


# ---------------------------------------------------------------------- helpers
def _s32(v):
    return ((v & M32) ^ 0x80000000) - 0x80000000


def f_mulh(a, b):
    return ((((a ^ S64) - S64) * ((b ^ S64) - S64)) >> 64) & M64


def f_mulhsu(a, b):
    return ((((a ^ S64) - S64) * b) >> 64) & M64


def f_div(a, b):
    if b == 0:
        return M64
    sa, sb = (a ^ S64) - S64, (b ^ S64) - S64
    q = abs(sa) // abs(sb)
    if (sa < 0) != (sb < 0):
        q = -q
    return q & M64


def f_divu(a, b):
    return a // b if b else M64


def f_rem(a, b):
    if b == 0:
        return a
    sa, sb = (a ^ S64) - S64, (b ^ S64) - S64
    r = abs(sa) % abs(sb)
    return (-r if sa < 0 else r) & M64


def f_remu(a, b):
    return a % b if b else a


def f_divw(a, b):
    sa, sb = _s32(a), _s32(b)
    if sb == 0:
        return M64
    q = abs(sa) // abs(sb)
    if (sa < 0) != (sb < 0):
        q = -q
    return _s32(q) & M64


def f_divuw(a, b):
    a, b = a & M32, b & M32
    if b == 0:
        return M64
    return _s32(a // b) & M64


def f_remw(a, b):
    sa, sb = _s32(a), _s32(b)
    if sb == 0:
        return sa & M64
    r = abs(sa) % abs(sb)
    return (-r if sa < 0 else r) & M64


def f_remuw(a, b):
    a, b = a & M32, b & M32
    if b == 0:
        return _s32(a) & M64
    return _s32(a % b) & M64


_HELPERS = {'f_mulh': f_mulh, 'f_mulhsu': f_mulhsu, 'f_div': f_div, 'f_divu': f_divu,
            'f_rem': f_rem, 'f_remu': f_remu, 'f_divw': f_divw, 'f_divuw': f_divuw,
            'f_remw': f_remw, 'f_remuw': f_remuw}

SEXT = '%s = (%s | 0xFFFFFFFF00000000) if %s & 0x80000000 else %s'


# ---------------------------------------------------------------------- compiler
class JIT:
    def __init__(self, cpu):
        self.cpu = cpu
        g = {'x': cpu.x, 'mB': cpu.mB, 'mb': cpu.mb, 'mH': cpu.mH, 'mh': cpu.mh,
             'mI': cpu.mI, 'mi': cpu.mi, 'mQ': cpu.mQ, 'rget': None, 'wget': None,
             '__builtins__': __builtins__}
        g.update(_HELPERS)
        load_slow, store_slow = cpu.load_slow, cpu.store_slow

        def load(ad, size):
            return load_slow(ad & M64, size)

        def store(ad, size, v):
            store_slow(ad & M64, size, v)
        g['load'] = load
        g['store'] = store

        def ld_b(ad):
            v = load(ad, 1)
            return (v | 0xFFFFFFFFFFFFFF00) if v & 0x80 else v

        def ld_h(ad):
            v = load(ad, 2)
            return (v | 0xFFFFFFFFFFFF0000) if v & 0x8000 else v

        def ld_w(ad):
            v = load(ad, 4)
            return (v | HI32) if v & 0x80000000 else v
        g['ld_b'], g['ld_h'], g['ld_w'] = ld_b, ld_h, ld_w
        self.g = g
        # code object -> line table (see gen_region); weak, so a region nothing
        # runs any more can be freed
        self.lines = _weak_dict()
        self.codes = {}             # page digest -> (entries, code object)
        self.compiled = 0           # regions installed
        self.failed = 0             # region builds that failed
        self.stub_code = None
        # with the C core (pyvm/native) running the hart, this compiler is idle
        on = ENABLED and getattr(cpu, 'native', None) is None
        self.hot = HOT if on else -1
        # background compiler
        self.workers = []
        self.max_workers = WORKERS
        self.use_worker = WORKERS > 0
        self.next_req = 0
        self.inflight = {}          # req id -> (page, digest, seeds, worker)
        self.backlog = collections.deque()  # pages waiting for a free slot
        # on-disk cache: page digest -> (entries, code, lines, calls), as read
        # from the file (zlib-compressed marshal) or built in this run (a tuple)
        self.disk = {}
        self.disk_new = {}
        self.cache_path = None
        if CACHE and on:
            self.cache_path = cache_path()
            self.disk = load_cache(self.cache_path)

    def set_tlbs(self, rtlb, wtlb):
        g = self.g
        g['rget'] = rtlb.get
        g['wget'] = wtlb.get

    # ------------------------------------------------------------ tier 0
    def make_stub(self, page, off):
        """Entry for page offset `off`, called the first time it runs: the
        page's cached region if it has one, else a tier-0 stub (or a
        one-instruction wrapper when the instruction straddles the page end)."""
        cpu = self.cpu
        if page.region is None and not page.looked_up and (self.codes or self.disk):
            page.looked_up = True               # look the page up only once
            if self.install_cached(page):
                fn = page.blocks[off]
                if fn is not page.cold:
                    return fn
        run = scan_mem(cpu.mH, page.ram >> 1, off, 32, None, False)
        if not run:
            decode_at = cpu.decode_at

            def straddle(pc, budget):
                return decode_at(pc, page)(pc), 1
            page.ranges[off] = off + 4
            return straddle
        hs = [cpu.decode(insn) for _, insn, _, _ in run]
        n = len(hs)
        kmap = {o - off: k for k, (o, _, _, _) in enumerate(run)}
        page.ranges[off] = run[-1][0] + run[-1][3]
        jit = self
        hot = self.hot
        cnt = 0

        def stub(pc, budget, kmap=kmap):
            nonlocal cnt
            cnt += 1
            if cnt == hot:
                fn = jit.promote(page, off)
                if fn is not None:
                    return fn(pc, budget)
            for h in hs:
                pc = h(pc)
            return pc, n
        self.stub_code = stub.__code__
        return stub

    # ------------------------------------------------------------ tier 1
    def promote(self, page, off):
        """The entry at `off` is hot: (re)build its page's region.  Returns the
        entry's new code if it is ready now, else None (keep interpreting)."""
        page.hot.add(off)
        if page.building is not None:          # a build is on its way; it is re-checked then
            return None
        self.build(page)
        region = page.region
        fn = page.blocks[off]
        return fn if region is not None and fn is region[1] else None

    def build(self, page):
        # everything that has run in the page so far, hot or not: entries
        # reached from other pages are found no other way, and leaving one out
        # means building the region again when it gets hot
        seeds = set(page.hot)
        seeds.update(page.ranges)
        if page.region is not None:
            seeds.update(page.region[0])
        seeds = sorted(seeds)
        o = page.ram
        data = bytes(self.cpu.mB[o:o + 4096])
        digest = page_digest(data)
        hit = self.codes.get(digest)
        if hit is not None and set(seeds) <= hit[0]:
            page.hot.clear()
            self.install(page, hit[2], hit[1])
            return
        if self.use_worker and self.request(page, digest, data, seeds):
            return
        page.hot.clear()
        res = build_region(data, seeds)
        if res is None:
            return
        entries, src, lines, calls = res
        code = compile_source(src)
        lb = marshal.dumps(lines)
        self.register(code, lb, calls)
        self.store(digest, entries, code, lb, calls)
        self.install(page, entries, code)

    def register(self, code, lines, calls):
        """Make a region's code runnable.  `lines` is its marshalled line
        table, unpacked only if an exception ever needs it (see unwind)."""
        g = self.g
        for insn in calls:
            name = 'h_%x' % insn
            if name not in g:
                g[name] = self.cpu.decode(insn)
        self.lines[code] = lines

    def store(self, digest, entries, code, lines, calls, cb=None):
        codes = self.codes
        if len(codes) >= MAX_CODES:         # forget the older half
            for k in list(codes)[:MAX_CODES // 2]:
                del codes[k]
        codes[digest] = (frozenset(e for e, _ in entries), code, entries)
        if self.cache_path is not None:
            if cb is None:
                cb = marshal.dumps(code)
            ent = (entries, cb, lines, calls)   # marshalled when saved
            self.disk[digest] = self.disk_new[digest] = ent

    def install(self, page, entries, code):
        fn = types.FunctionType(code, self.g)
        blocks = page.blocks
        ranges = page.ranges
        old = page.region
        if old is not None:                 # entries the new region dropped: back to cold
            keep = {e for e, _ in entries}
            for e in old[0]:
                if e not in keep and blocks[e] is old[1]:
                    blocks[e] = page.cold
                    ranges.pop(e, None)
        for e, end in entries:
            blocks[e] = fn
            ranges[e] = end
        page.region = (frozenset(e for e, _ in entries), fn)
        self.compiled += 1

    def install_cached(self, page):
        o = page.ram
        digest = page_digest(bytes(self.cpu.mB[o:o + 4096]))
        hit = self.codes.get(digest)
        if hit is None:
            ent = self.disk.get(digest)
            if ent is None:
                return False
            try:
                if type(ent) is not tuple:
                    ent = marshal.loads(zlib.decompress(ent))
                entries, cb, lines, calls = ent
                code = marshal.loads(cb)
            except Exception:                   # a damaged record: just forget it
                self.disk.pop(digest, None)
                return False
            self.register(code, lines, calls)
            entries = [tuple(e) for e in entries]
            hit = self.codes[digest] = (frozenset(e for e, _ in entries), code, entries)
        self.install(page, hit[2], hit[1])
        return True

    def invalidate(self, page, off, size):
        """Bytes [off, off+size) of a code page were written: drop every block
        that covers them -- for a compiled region, the whole region."""
        ranges = page.ranges
        blocks = page.blocks
        cold = page.cold
        region = page.region
        hit = off + size
        kill = False
        for b, e in list(ranges.items()):
            if b < hit and off < e:
                if region is not None and blocks[b] is region[1]:
                    kill = True
                blocks[b] = cold
                del ranges[b]
        if kill:
            for b in region[0]:
                if blocks[b] is region[1]:
                    blocks[b] = cold
                    ranges.pop(b, None)
            page.region = None

    # ------------------------------------------------------------ background compiler
    def request(self, page, digest, data, seeds):
        """Ask a worker to build the page's region.  False if none can be asked."""
        if len(self.inflight) >= MAX_INFLIGHT:
            if page.building is None:       # busy: queue it, stay in tier 0 meanwhile
                page.building = -1
                self.backlog.append(page)
            return True
        ws = self.workers
        w = min(ws, key=lambda w: w.pending) if ws else None
        if (w is None or w.pending) and len(ws) < self.max_workers:
            w = Worker.start() or w
            if w is not None and w not in ws:
                ws.append(w)
        if w is None:
            self.use_worker = False
            return False
        req = self.next_req
        self.next_req += 1
        if not w.send(marshal.dumps((req, data, seeds))):
            self.lost(w)
            return self.use_worker and self.request(page, digest, data, seeds)
        self.inflight[req] = (page, digest, seeds, w)
        page.building = req
        return True

    def lost(self, w):
        """A worker died: forget what it was doing (those pages rebuild later)."""
        w.stop()
        if w in self.workers:
            self.workers.remove(w)
        for req, ent in list(self.inflight.items()):
            if ent[3] is w:
                ent[0].building = None
                del self.inflight[req]
        if not self.workers:
            self.max_workers = 0
            self.use_worker = False

    def poll(self):
        """Install whatever regions the workers have sent back."""
        for w in self.workers:
            msgs = w.receive()
            if msgs is None:
                self.lost(w)
                break
            for msg in msgs:
                w.pending -= 1
                self.arrived(*msg)
        backlog = self.backlog
        while backlog and len(self.inflight) < MAX_INFLIGHT:
            page = backlog.popleft()
            page.building = None
            if self.cpu.code_pages.get(page.ram >> 12) is page:
                self.build(page)

    def arrived(self, req, entries, cb, lines, calls):
        ent = self.inflight.pop(req, None)
        if ent is None:
            return
        page, digest, seeds, _ = ent
        page.building = None
        hot = page.hot
        hot.difference_update(seeds)        # handled, included or not
        if not cb:
            self.failed += 1                # (the worker's stderr says why)
            return
        code = marshal.loads(cb)
        entries = [tuple(e) for e in entries]
        self.register(code, lines, calls)
        self.store(digest, entries, code, lines, calls, cb)
        o = page.ram
        if self.cpu.code_pages.get(o >> 12) is not page:
            return                          # the page is gone
        if page_digest(bytes(self.cpu.mB[o:o + 4096])) != digest:
            # written while it was being built (the data part of a mixed page,
            # say): build again from what is there now.  Every such write is a
            # slow-path store, and a page takes only CODE_STORE_LIMIT of those.
            hot.update(seeds)
            self.build(page)
            return
        self.install(page, entries, code)
        hot.difference_update(page.region[0])
        if hot:                             # more became hot meanwhile
            self.build(page)

    def close(self):
        for w in self.workers:
            w.stop()
        self.workers = []
        self.inflight.clear()
        self.backlog.clear()
        if self.cache_path is not None and self.disk_new:
            save_cache(self.cache_path, self.disk_new)
            self.disk_new = {}


class Worker:
    """A jitworker.py child process."""

    def __init__(self, proc):
        self.proc = proc
        self.buf = bytearray()
        self.pending = 0

    @classmethod
    def start(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            proc = subprocess.Popen(
                [sys.executable, '-c',
                 'import sys; sys.path.insert(0, %r); '
                 'from pyvm.jitworker import main; main()' % root],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, close_fds=True)
            os.set_blocking(proc.stdout.fileno(), False)
        except OSError:
            return None
        return cls(proc)

    def send(self, blob):
        try:
            self.proc.stdin.write(struct.pack('<I', len(blob)) + blob)
            self.proc.stdin.flush()
        except OSError:
            return False
        self.pending += 1
        return True

    def receive(self):
        """The complete replies that have arrived, or None if the worker died."""
        try:
            data = os.read(self.proc.stdout.fileno(), 1 << 20)
        except BlockingIOError:
            return ()
        except OSError:
            return None
        if not data:
            return None
        buf = self.buf
        buf += data
        out = []
        pos = 0
        while len(buf) - pos >= 4:
            n, = struct.unpack_from('<I', buf, pos)
            if len(buf) - pos - 4 < n:
                break
            out.append(marshal.loads(bytes(buf[pos + 4:pos + 4 + n])))
            pos += 4 + n
        del buf[:pos]
        return out

    def stop(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.kill()
            self.proc.wait(1)
        except Exception:
            pass


def _weak_dict():
    try:
        d = weakref.WeakKeyDictionary()
        d[compile('0', '<probe>', 'eval')] = None    # code objects weakly referenceable?
        d.clear()
        return d
    except TypeError:
        return {}


def page_digest(data):
    return hashlib.blake2b(data, digest_size=16).digest()


# ---------------------------------------------------------------------- scanning
def scan_mem(mH, hbase, off, limit, stops, superblock):
    """Decode the run starting at page offset `off`.  mH is a halfword view and
    hbase the page's first halfword in it.  Returns [(offset, insn, ir, len)].

    The run ends after a control transfer -- for a superblock only after an
    unconditional one, or a branch back to the run's start (a loop) -- before
    an offset in `stops`, at the page end, or after `limit` instructions."""
    out = []
    start = off
    joins = set()                           # forward branch targets inside the run
    while len(out) < limit and off < 4096:
        if stops is not None and off != start and off in stops and off not in joins:
            break
        lo = mH[hbase + (off >> 1)]
        if (lo & 3) == 3:
            if off == 4094:
                break                       # straddles the page: not in a block
            insn = lo | (mH[hbase + (off >> 1) + 1] << 16)
            il = 4
        else:
            insn = lo
            il = 2
        r = ir(insn)
        out.append((off, insn, r, il))
        off += il
        if r[0] in ENDS:
            if not superblock or r[0] != 'br' or off - il + r[4] == start:
                break
            if r[4] > 0:
                joins.add(off - il + r[4])
    return out


def structured(run):
    """Forward branches that can be compiled as ``if``: index -> index of the
    target, for each branch whose target is later in the run and whose
    skipped instructions branch nowhere past that target."""
    pos = {o: k for k, (o, _, _) in enumerate(run)}
    out = {}
    for k, (o, insn, il) in enumerate(run):
        r = ir(insn)
        if r[0] == 'br' and r[4] > 0:
            t = pos.get(o + r[4])
            if t is not None:
                out[k] = t
    # drop branches whose skipped range is crossed by another structured one
    ok = {}
    for k, t in out.items():
        if all(t2 <= t for k2, t2 in out.items() if k < k2 < t):
            ok[k] = t
    # an inner branch that crosses an accepted outer one must go too
    changed = True
    while changed:
        changed = False
        for k, t in list(ok.items()):
            for k2, t2 in ok.items():
                if k2 < k < t2 < t:
                    del ok[k]
                    changed = True
                    break
    return ok


def build_region(data, seeds):
    """Find the region's entries and generate its source.

    Starting from the hot entries (`seeds`), every branch target, jump
    target and return address inside the page becomes an entry too, so that
    control flow staying in the page stays in the region.
    Returns (entries [(offset, end)], source, line table, call insns) or None."""
    H = memoryview(data).cast('H')
    seen = set()
    ents = []
    nseeds = len(seeds)
    todo = collections.deque(seeds)
    covered = set()                         # instruction offsets in the region
    while todo:
        e = todo.popleft()
        if e in seen or not 0 <= e < 4096 or e & 1:
            continue
        nseeds -= 1
        if nseeds < 0 and len(covered) >= MAX_REGION:
            continue                        # hot entries always; the rest while small
        seen.add(e)
        run = scan_mem(H, 0, e, MAX_INSNS, seen, True)
        if not run:
            continue
        ents.append(e)
        for o, insn, r, il in run:
            covered.add(o)
        for o, insn, r, il in run:
            k = r[0]
            if k == 'br':
                todo.append(o + r[4])
            elif k == 'jal':
                todo.append(o + r[2])
                if r[1]:
                    todo.append(o + il)
            elif k == 'jalr':
                if r[1]:
                    todo.append(o + il)
            elif k == 'call':
                todo.append(o + il)
        last = run[-1]
        if last[2][0] not in ('jal', 'jalr', 'call'):
            todo.append(last[0] + last[3])
    if not ents:
        return None
    entset = set(ents)
    runs = []
    for e in sorted(ents):
        run = scan_mem(H, 0, e, MAX_INSNS, entset, True)
        runs.append((e, [(o, insn, il) for o, insn, _, il in run]))
    src, lines, calls = gen_region(runs)
    return [(e, run[-1][0] + run[-1][2]) for e, run in runs], src, lines, calls


# ---------------------------------------------------------------------- code generation
# Pure functions of the instruction words, so they can also run in a worker
# process (see jitworker.py).

M = '0xFFFFFFFFFFFFFFFF'
_LOAD_VIEW = {(1, True): ('mb', 0), (2, True): ('mh', 1), (4, True): ('mi', 2),
              (1, False): ('mB', 0), (2, False): ('mH', 1), (4, False): ('mI', 2),
              (8, False): ('mQ', 3)}
_STORE_VIEW = {1: ('mB', 0), 2: ('mH', 1), 4: ('mI', 2), 8: ('mQ', 3)}
_SLOW_LOAD = {(1, True): 'ld_b(ad)', (2, True): 'ld_h(ad)', (4, True): 'ld_w(ad)',
              (1, False): 'load(ad, 1)', (2, False): 'load(ad, 2)',
              (4, False): 'load(ad, 4)', (8, False): 'load(ad, 8)'}


def compile_source(src):
    code = compile(src, '<pyvm-region>', 'exec')
    return next(c for c in code.co_consts if isinstance(c, types.CodeType))


def gen_region(runs):
    """Python source for a page region.

    runs: [(entry offset, [(offset, insn, length), ...])], offsets within the
    page.  The function is entered at any entry; ``o`` holds the page offset
    of the sub-block to run next and a binary decision tree on it picks the
    code.  Moving to another entry is ``o = target; continue``, so control
    flow that stays in the page never returns to the dispatcher (every move
    backwards checks the instruction budget).  Registers live in the list ``X``
    -- except in a sub-block that loops back to its own start, which keeps
    them in locals for the whole loop and writes them back when it leaves.

    Returns (source, line table, insns run through their handler).  The line
    table maps line number - 1 to (k, offset, loop registers): the guest
    instruction (k-th of its sub-block, at that page offset) the line belongs
    to, and for lines inside a loop the registers held in locals there."""
    entries = set(e for e, _ in runs)
    rset = '{%s}' % ', '.join(str(e) for e in sorted(entries))
    L = []                  # (indent, text, k, offset, loopregs)
    calls = []

    def X(reg):
        return 'X[%d]' % reg if reg else '0'

    def goto(ind, k, o, T, c):
        """Continue at page offset T after c more instructions."""
        if T in entries:
            L.append((ind, 'n += %d' % c if c else 'pass', k, o, None))
            L.append((ind, 'o = %d' % T, k, o, None))
            if T > o:
                # forward: entry offsets only grow along such a chain, so it
                # ends; only a backward move can loop and needs the budget check
                L.append((ind, 'continue', k, o, None))
                return
            L.append((ind, 'if n < budget:', k, o, None))
            L.append((ind + 1, 'continue', k, o, None))
            L.append((ind, 'return pb + %d, n' % T, k, o, None))
        elif 0 <= T < 4096:
            L.append((ind, 'return pb + %d, n + %d' % (T, c), k, o, None))
        else:
            L.append((ind, 'return (pb + %d) & %s, n + %d' % (T, M, c), k, o, None))

    def body(ind, e, run):
        """Code for one sub-block (X-list registers)."""
        nrun = len(run)
        last_off, last_insn, last_il = run[-1]
        lr = ir(last_insn)
        if ((lr[0] == 'br' and last_off + lr[4] == e) or
                (lr[0] == 'jal' and lr[1] == 0 and last_off + lr[2] == e)):
            return loop_body(ind, e, run)
        skips = structured(run)
        closes = []
        base_ind = ind
        for k in range(nrun):
            o, insn, il = run[k]
            r = ir(insn)
            kind = r[0]
            while closes and closes[-1] == k:
                closes.pop()
                ind -= 1

            def E(s, extra=0):
                L.append((ind + extra, s, k, o, None))
            if kind in ('alu', 'alui', 'lui', 'auipc', 'nop', 'load', 'store'):
                straight(E, r, o, X, 'pb')
            elif kind == 'br' and k in skips:
                t = skips[k]
                E('if %s:' % _cond(r[1], X(r[2]), X(r[3])))
                E('n -= %d' % (t - k - 1) if t - k - 1 else 'pass', 1)
                E('else:')
                E('pass', 1)                    # the skipped code may emit nothing
                ind += 1
                closes.append(t)
            elif kind == 'br':
                E('if %s:' % _cond(r[1], X(r[2]), X(r[3])))
                goto(ind + 1, k, o, o + r[4], k + 1)
            elif kind == 'jal':
                if r[1]:
                    E('X[%d] = pb + %d' % (r[1], o + il))
                goto(ind, k, o, o + r[2], k + 1)
                return
            elif kind == 'jalr':
                rd, rs1, imm = r[1], r[2], r[3]
                if imm:
                    E('t = (%s + %d) & 0xFFFFFFFFFFFFFFFE' % (X(rs1), imm))
                else:
                    E('t = %s & 0xFFFFFFFFFFFFFFFE' % X(rs1))
                if rd:
                    E('X[%d] = pb + %d' % (rd, o + il))
                E('n += %d' % (k + 1))
                E('o = t - pb')
                E('if o in %s and n < budget:' % rset)
                E('continue', 1)
                E('return t, n')
                return
            else:                               # 'call': run the handler, leave
                if insn not in calls:
                    calls.append(insn)
                E('return h_%x(pb + %d), n + %d' % (insn, o, k + 1))
                return
        goto(base_ind, nrun - 1, last_off, last_off + last_il, nrun)

    def loop_body(ind, e, run):
        """A sub-block whose last instruction jumps back to its start: a
        `while` loop with the registers in locals."""
        nrun = len(run)
        irs = [ir(insn) for _, insn, _ in run]
        written = []
        used = set()
        for r in irs:
            d = _dst(r)
            if d and d not in written:
                written.append(d)
            used.update(s for s in _srcs(r) if s)
        used.update(written)
        regs = tuple(sorted(used))
        o0 = run[0][0]
        for reg in regs:
            L.append((ind, 'r%d = X[%d]' % (reg, reg), 0, o0, None))
        L.append((ind, 'while True:', 0, o0, None))
        li = ind + 1

        def R(reg):
            return 'r%d' % reg if reg else '0'

        def flush(i2, k, o):
            for reg in written:
                L.append((i2, 'X[%d] = r%d' % (reg, reg), k, o, regs))

        def leave(i2, k, o, T, c):
            """Leave the loop for page offset T, c instructions into this pass."""
            flush(i2, k, o)
            if T in entries:
                L.append((i2, 'n += %d' % c if c else 'pass', k, o, regs))
                L.append((i2, 'o = %d' % T, k, o, regs))
                L.append((i2, 'break', k, o, regs))
            elif 0 <= T < 4096:
                L.append((i2, 'return pb + %d, n + %d' % (T, c), k, o, regs))
            else:
                L.append((i2, 'return (pb + %d) & %s, n + %d' % (T, M, c), k, o, regs))

        skips = structured(run)
        closes = []
        base_li = li
        for k in range(nrun):
            o, insn, il = run[k]
            r = irs[k]
            kind = r[0]
            final = k == nrun - 1
            while closes and closes[-1] == k:
                closes.pop()
                li -= 1

            def E(s, extra=0):
                L.append((li + extra, s, k, o, regs))
            if kind in ('alu', 'alui', 'lui', 'auipc', 'nop', 'load', 'store'):
                straight(E, r, o, R, 'pb')
            elif kind == 'br' and k in skips:
                t = skips[k]
                E('if %s:' % _cond(r[1], R(r[2]), R(r[3])))
                E('n -= %d' % (t - k - 1) if t - k - 1 else 'pass', 1)
                E('else:')
                E('pass', 1)                    # the skipped code may emit nothing
                li += 1
                closes.append(t)
            elif kind == 'br':
                cond = _cond(r[1], R(r[2]), R(r[3]))
                if final:                       # the back edge
                    E('n += %d' % nrun)
                    E('if not (%s):' % cond)
                    leave(li + 1, k, o, o + il, 0)
                    E('if n >= budget:')
                    flush(li + 1, k, o)
                    E('return pb + %d, n' % e, 1)
                else:
                    E('if %s:' % cond)
                    leave(li + 1, k, o, o + r[4], k + 1)
            else:                               # final 'jal' back to the start
                E('n += %d' % nrun)
                E('if n >= budget:')
                flush(li + 1, k, o)
                E('return pb + %d, n' % e, 1)
        last_o = run[-1][0]
        L.append((ind, 'if n < budget:', nrun - 1, last_o, None))
        L.append((ind + 1, 'continue', nrun - 1, last_o, None))
        L.append((ind, 'return pb + o, n', nrun - 1, last_o, None))

    def tree(ind, ents):
        if len(ents) <= 3:
            for i, (e, run) in enumerate(ents):
                L.append((ind, '%s o == %d:' % ('if' if i == 0 else 'elif', e), 0, e, None))
                body(ind + 1, e, run)
            L.append((ind, 'else:', 0, 0, None))
            L.append((ind + 1, 'return pb + o, n', 0, 0, None))
            return
        half = len(ents) // 2
        L.append((ind, 'if o < %d:' % ents[half][0], 0, 0, None))
        tree(ind + 1, ents[:half])
        L.append((ind, 'else:', 0, 0, None))
        tree(ind + 1, ents[half:])

    L.append((0, 'def blk(pc, budget):', 0, 0, None))
    L.append((1, 'X = x', 0, 0, None))
    L.append((1, 'o = pc & 4095', 0, 0, None))
    L.append((1, 'pb = pc - o', 0, 0, None))
    L.append((1, 'n = 0', 0, 0, None))
    L.append((1, 'while True:', 0, 0, None))
    tree(2, sorted(runs))
    src = '\n'.join('    ' * ind + text for ind, text, _, _, _ in L) + '\n'
    lines = [(k, o, regs) for _, _, k, o, regs in L]
    return src, lines, calls


def straight(E, r, o, R, pbname):
    """Code for a non-control-flow instruction.  R names registers."""
    kind = r[0]
    if kind == 'alu' or kind == 'alui':
        d = r[2]
        if d:
            dest = R(d)
            for s in _alu(r, R):
                E(s.replace('@', dest))
    elif kind == 'lui':
        if r[1]:
            E('%s = %d' % (R(r[1]), r[2]))
    elif kind == 'auipc':
        if r[1]:
            E('%s = (%s + %d) & %s' % (R(r[1]), pbname, o + r[2], M))
    elif kind == 'load':
        _, size, signed, rd, rs1, imm = r
        D = R(rd) if rd else '_'
        _addr(E, R(rs1), imm)
        E('d = rget(ad >> 12)')
        E('if d is None:' if size == 1 else 'if d is None or ad & %d:' % (size - 1))
        E('    %s = %s' % (D, _SLOW_LOAD[(size, signed)]))
        E('else:')
        view, sh = _LOAD_VIEW[(size, signed)]
        idx = '(ad + d) >> %d' % sh if sh else 'ad + d'
        if signed:
            E('    %s = %s[%s] & %s' % (D, view, idx, M))
        else:
            E('    %s = %s[%s]' % (D, view, idx))
    elif kind == 'store':
        _, size, rs1, rs2, imm = r
        v = R(rs2)
        if rs2 and size < 8:
            v = '%s & %d' % (v, (1 << (8 * size)) - 1)
        _addr(E, R(rs1), imm)
        E('d = wget(ad >> 12)')
        E('if d is None:' if size == 1 else 'if d is None or ad & %d:' % (size - 1))
        E('    store(ad, %d, %s)' % (size, v))
        E('else:')
        view, sh = _STORE_VIEW[size]
        idx = '(ad + d) >> %d' % sh if sh else 'ad + d'
        E('    %s[%s] = %s' % (view, idx, v))


def _addr(E, a, imm):
    # No 64-bit wrap here: an address outside [0, 2**64) is never in a TLB, so
    # it takes the slow path, which wraps it (see JIT.__init__).
    if a == '0':
        E('ad = %d' % (imm & M64))
    elif imm == 0:
        E('ad = %s' % a)
    elif imm > 0:
        E('ad = %s + %d' % (a, imm))
    else:
        E('ad = %s - %d' % (a, -imm))


def _alu(r, R):
    """Statements computing an ALU op; '@' stands for the destination."""
    kind, op = r[0], r[1]
    A = R(r[3])
    if kind == 'alui':
        imm = r[4]
        if op == 'addi':
            if A == '0':
                return ['@ = %d' % (imm & M64)]
            if imm == 0:
                return ['@ = %s' % A]
            if imm > 0:
                return ['@ = (%s + %d) & %s' % (A, imm, M)]
            return ['@ = (%s - %d) & %s' % (A, -imm, M)]
        if op == 'slti':
            return ['@ = 1 if (%s ^ %d) < %d else 0' % (A, S64, (imm & M64) ^ S64)]
        if op == 'sltiu':
            return ['@ = 1 if %s < %d else 0' % (A, imm & M64)]
        if op == 'xori':
            return ['@ = %s ^ %d' % (A, imm & M64)]
        if op == 'ori':
            return ['@ = %s | %d' % (A, imm & M64)]
        if op == 'andi':
            return ['@ = %s & %d' % (A, imm & M64)]
        if op == 'slli':
            return ['@ = (%s << %d) & %s' % (A, imm, M)]
        if op == 'srli':
            return ['@ = %s >> %d' % (A, imm)]
        if op == 'srai':
            return ['@ = (((%s ^ %d) - %d) >> %d) & %s' % (A, S64, S64, imm, M)]
        if op == 'addiw':
            return ['t = (%s + %d) & 0xFFFFFFFF' % (A, imm), SEXT % ('@', 't', 't', 't')]
        if op == 'slliw':
            return ['t = (%s << %d) & 0xFFFFFFFF' % (A, imm), SEXT % ('@', 't', 't', 't')]
        if op == 'srliw':
            return ['t = (%s & 0xFFFFFFFF) >> %d' % (A, imm), SEXT % ('@', 't', 't', 't')]
        if op == 'sraiw':
            return ['@ = ((((%s & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000) >> %d) & %s'
                    % (A, imm, M)]
        raise AssertionError(op)
    B = R(r[4])
    if op == 'add':
        if A == '0':
            return ['@ = %s' % B]
        if B == '0':
            return ['@ = %s' % A]
        return ['@ = (%s + %s) & %s' % (A, B, M)]
    if op == 'sub':
        return ['@ = (%s - %s) & %s' % (A, B, M)]
    if op == 'sll':
        return ['@ = (%s << (%s & 63)) & %s' % (A, B, M)]
    if op == 'slt':
        return ['@ = 1 if (%s ^ %d) < (%s ^ %d) else 0' % (A, S64, B, S64)]
    if op == 'sltu':
        return ['@ = 1 if %s < %s else 0' % (A, B)]
    if op == 'xor':
        return ['@ = %s ^ %s' % (A, B)]
    if op == 'srl':
        return ['@ = %s >> (%s & 63)' % (A, B)]
    if op == 'sra':
        return ['@ = (((%s ^ %d) - %d) >> (%s & 63)) & %s' % (A, S64, S64, B, M)]
    if op == 'or':
        return ['@ = %s | %s' % (A, B)]
    if op == 'and':
        return ['@ = %s & %s' % (A, B)]
    if op == 'addw':
        return ['t = (%s + %s) & 0xFFFFFFFF' % (A, B), SEXT % ('@', 't', 't', 't')]
    if op == 'subw':
        return ['t = (%s - %s) & 0xFFFFFFFF' % (A, B), SEXT % ('@', 't', 't', 't')]
    if op == 'sllw':
        return ['t = (%s << (%s & 31)) & 0xFFFFFFFF' % (A, B), SEXT % ('@', 't', 't', 't')]
    if op == 'srlw':
        return ['t = (%s & 0xFFFFFFFF) >> (%s & 31)' % (A, B), SEXT % ('@', 't', 't', 't')]
    if op == 'sraw':
        return ['@ = ((((%s & 0xFFFFFFFF) ^ 0x80000000) - 0x80000000) >> (%s & 31)) & %s'
                % (A, B, M)]
    if op == 'mul':
        return ['@ = (%s * %s) & %s' % (A, B, M)]
    if op == 'mulhu':
        return ['@ = (%s * %s) >> 64' % (A, B)]
    if op == 'mulw':
        return ['t = (%s * %s) & 0xFFFFFFFF' % (A, B), SEXT % ('@', 't', 't', 't')]
    return ['@ = f_%s(%s, %s)' % (op, A, B)]


def _srcs(r):
    k = r[0]
    if k == 'alu':
        return (r[3], r[4])
    if k == 'alui':
        return (r[3],)
    if k == 'load':
        return (r[4],)
    if k == 'store':
        return (r[2], r[3])
    if k == 'br':
        return (r[2], r[3])
    if k == 'jalr':
        return (r[2],)
    return ()


def _dst(r):
    k = r[0]
    if k in ('alu', 'alui'):
        return r[2]
    if k in ('lui', 'auipc', 'jal', 'jalr'):
        return r[1]
    if k == 'load':
        return r[3]
    return 0


def _cond(c, A, B):
    if c == 'eq':
        return '%s == %s' % (A, B)
    if c == 'ne':
        return '%s != %s' % (A, B)
    if c == 'ltu':
        return '%s < %s' % (A, B)
    if c == 'geu':
        return '%s >= %s' % (A, B)
    sa = str(S64) if A == '0' else '(%s ^ %d)' % (A, S64)
    sb = str(S64) if B == '0' else '(%s ^ %d)' % (B, S64)
    return '%s %s %s' % (sa, '<' if c == 'lt' else '>=', sb)


# ---------------------------------------------------------------------- unwinding
# ---------------------------------------------------------------------- unwinding
def unwind(jit, cpu, exc, entry_pc):
    """An exception left the block or region entered at entry_pc.

    Returns (instructions completed before the faulting one, pc of the
    faulting one), after writing back any guest registers a loop held in
    locals.  The innermost compiled frame is the one that was running: a stub
    that has just had its region built calls it from inside itself."""
    tb = exc.__traceback__
    lines = jit.lines
    stub_code = jit.stub_code
    found = None
    while tb is not None:
        code = tb.tb_frame.f_code
        if code is stub_code or code in lines:
            found = tb
        tb = tb.tb_next
    if found is None:
        return 0, entry_pc
    f = found.tb_frame
    loc = f.f_locals
    lt = lines.get(f.f_code)
    if type(lt) is bytes:
        lt = lines[f.f_code] = marshal.loads(lt)
    if lt is None:                                  # tier-0 stub
        pc = loc['pc']
        return loc['kmap'].get(pc - entry_pc, 0), pc
    ln = found.tb_lineno - 1
    k, o, regs = lt[ln] if 0 <= ln < len(lt) else (0, entry_pc & 4095, None)
    if regs:
        x = cpu.x
        for reg in regs:
            v = loc.get('r%d' % reg)
            if v is not None:
                x[reg] = v
    pb = loc.get('pb', entry_pc - (entry_pc & 4095))
    return loc.get('n', 0) + k, (pb + o) & M64


# ---------------------------------------------------------------------- on-disk cache
def cache_path():
    """Where compiled blocks are kept between runs.  The name covers this
    module's source and the Python bytecode version, so a change to either
    starts a fresh cache."""
    base = os.environ.get('PYVM_CACHE_DIR') or os.path.join(
        os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache'),
        'pyvm')
    h = hashlib.sha1()
    with open(os.path.abspath(__file__), 'rb') as f:
        h.update(f.read())
    h.update(importlib.util.MAGIC_NUMBER)
    h.update(sys.implementation.cache_tag.encode())
    return os.path.join(base, 'regions-%s.bin' % h.hexdigest()[:16])


_RECORD = struct.Struct('<I16s')      # record length, page digest
_LOADED = {}                            # path -> records, shared by every VM in the process


def load_cache(path):
    """The cache file's records: page digest -> zlib-compressed marshal blob of
    (entries, code, line table, calls).  Records are only ever appended; the
    last one for a digest wins, and a torn record at the end is ignored."""
    d = _LOADED.get(path)
    if d is not None:
        return d
    d = _LOADED[path] = {}
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return d
    if len(data) > CACHE_MAX_BYTES:
        try:
            os.remove(path)
        except OSError:
            pass
        return d
    pos = 0
    while pos + _RECORD.size <= len(data):
        n, digest = _RECORD.unpack_from(data, pos)
        pos += _RECORD.size
        if pos + n > len(data):
            break
        d[digest] = data[pos:pos + n]
        pos += n
    return d


def save_cache(path, new):
    """Append the regions built in this run to the cache file.  One write per
    run, in append mode, so VMs sharing the cache never lose each other's."""
    out = bytearray()
    for digest, ent in new.items():
        blob = zlib.compress(marshal.dumps(ent), 1)
        out += _RECORD.pack(len(blob), digest) + blob
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'ab') as f:
            f.write(out)
    except OSError:
        pass
