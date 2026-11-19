#!/usr/bin/env python3
"""Differential test of the region compiler (pyvm/jit.py) and the native
core (pyvm/native).

Generates random RV64GC programs -- ALU and M-extension operations, loads and
stores of every width (some misaligned), forward branches, counted loops,
calls, compressed instructions, F/D arithmetic on awkward values (NaNs,
infinities, subnormals, all rounding modes), and instructions that trap in
the middle of a block (illegal instructions, ecall) -- and runs each one in M-mode with the
plain interpreter, with every entry compiled on its first run, and (when it can
be built) with the native core.  Registers, memory and the number of traps
taken must come out the same.

No toolchain or kernel needed:

    python tests/test_jit.py [--seeds 40] [--seed N] [-v]
"""

import argparse
import os
import random
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from pyvm import native                         # noqa: E402
from pyvm.console import BufferConsole          # noqa: E402
from pyvm.machine import Machine                # noqa: E402

RAM_BASE = 0x80000000
DATA = RAM_BASE + 0x40000           # scratch memory the programs load from and store to
TOHOST = RAM_BASE + 0x80000
HANDLER = RAM_BASE + 0x100          # trap handler
CODE = RAM_BASE + 0x1000            # the random program

# x1 = ra, x2 = trap count, x3 = DATA, x4 = loop counter, x30 = handler scratch
FREE = [r for r in range(5, 32) if r != 30]
CREGS = list(range(8, 16))          # registers compressed instructions can name


# ---------------------------------------------------------------- encoders
def r_type(f7, rs2, rs1, f3, rd, op):
    return (f7 << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | op


def i_type(imm, rs1, f3, rd, op):
    return ((imm & 0xFFF) << 20) | (rs1 << 15) | (f3 << 12) | (rd << 7) | op


def s_type(imm, rs2, rs1, f3, op):
    imm &= 0xFFF
    return ((imm >> 5) << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | ((imm & 31) << 7) | op


def b_type(imm, rs2, rs1, f3):
    imm &= 0x1FFF
    return (((imm >> 12) & 1) << 31) | (((imm >> 5) & 0x3F) << 25) | (rs2 << 20) | \
        (rs1 << 15) | (f3 << 12) | (((imm >> 1) & 0xF) << 8) | (((imm >> 11) & 1) << 7) | 0x63


def j_type(imm, rd):
    imm &= 0x1FFFFF
    return (((imm >> 20) & 1) << 31) | (((imm >> 1) & 0x3FF) << 21) | \
        (((imm >> 11) & 1) << 20) | (((imm >> 12) & 0xFF) << 12) | (rd << 7) | 0x6F


def u_type(imm20, rd, op):
    return ((imm20 & 0xFFFFF) << 12) | (rd << 7) | op


class Prog:
    """Instructions as (size, encoding-or-callable) so jumps can be fixed up."""

    def __init__(self):
        self.items = []             # [size, value or fn(pc_of_item) -> value]
        self.labels = {}

    def here(self):
        return sum(s for s, _ in self.items)

    def emit(self, word):
        self.items.append([4, word])

    def emitc(self, half):
        self.items.append([2, half])

    def label(self, name):
        self.labels[name] = self.here()

    def jump(self, size, fn):
        self.items.append([size, fn])

    def assemble(self):
        out = bytearray()
        pc = 0
        for size, v in self.items:
            if callable(v):
                v = v(pc)
            out += struct.pack('<I' if size == 4 else '<H', v)
            pc += size
        return bytes(out)


def li(p, rd, value):
    """Load a 64-bit constant (lui/addi pairs and shifts)."""
    value &= (1 << 64) - 1
    if value >= 1 << 63:
        value -= 1 << 64
    if -2048 <= value < 2048:
        p.emit(i_type(value, 0, 0, rd, 0x13))
        return
    hi, lo = value >> 32, value & 0xFFFFFFFF
    li32(p, rd, hi if hi < 1 << 31 else hi - (1 << 32))
    p.emit(i_type(11, rd, 1, rd, 0x13))                         # slli rd, 11
    p.emit(i_type((lo >> 21) & 0x7FF, rd, 6, rd, 0x13))        # ori
    p.emit(i_type(11, rd, 1, rd, 0x13))
    p.emit(i_type((lo >> 10) & 0x7FF, rd, 6, rd, 0x13))
    p.emit(i_type(10, rd, 1, rd, 0x13))
    p.emit(i_type(lo & 0x3FF, rd, 6, rd, 0x13))


def li32(p, rd, v):
    lo = v & 0xFFF
    if lo >= 0x800:
        lo -= 0x1000
    hi = ((v - lo) >> 12) & 0xFFFFF
    p.emit(u_type(hi, rd, 0x37))
    p.emit(i_type(lo, rd, 0, rd, 0x1B))                        # addiw


# ---------------------------------------------------------------- generator
ALU = [(0x00, 0), (0x20, 0), (0x00, 1), (0x00, 2), (0x00, 3), (0x00, 4), (0x00, 5),
       (0x20, 5), (0x00, 6), (0x00, 7)] + [(0x01, f3) for f3 in range(8)]
ALUW = [(0x00, 0), (0x20, 0), (0x00, 1), (0x00, 5), (0x20, 5)] + \
    [(0x01, f3) for f3 in (0, 4, 5, 6, 7)]
INTERESTING = [0, 1, -1, 2, 0x7FFFFFFF, -0x80000000, 0xFFFFFFFF, 1 << 63, (1 << 63) - 1,
               0x8000000000000000 - 1, 0x123456789ABCDEF0, -2]


def rreg(rng, allow_zero=True):
    return rng.choice(([0] if allow_zero else []) + FREE + [3])


def straight(p, rng, depth=0):
    """One random non-control-flow instruction (or a small group)."""
    k = rng.random()
    rd = rng.choice(FREE)
    if k < 0.25:
        f7, f3 = rng.choice(ALU)
        p.emit(r_type(f7, rreg(rng), rreg(rng), f3, rd, 0x33))
    elif k < 0.35:
        f7, f3 = rng.choice(ALUW)
        p.emit(r_type(f7, rreg(rng), rreg(rng), f3, rd, 0x3B))
    elif k < 0.50:
        f3 = rng.choice([0, 2, 3, 4, 6, 7, 1, 5])
        if f3 == 1:
            p.emit(i_type(rng.randrange(64), rreg(rng), 1, rd, 0x13))
        elif f3 == 5:
            p.emit(i_type(rng.randrange(64) | (0x400 if rng.random() < .5 else 0),
                          rreg(rng), 5, rd, 0x13))
        else:
            p.emit(i_type(rng.randrange(-2048, 2048), rreg(rng), f3, rd, 0x13))
    elif k < 0.55:
        f3 = rng.choice([0, 1, 5])
        if f3 == 0:
            p.emit(i_type(rng.randrange(-2048, 2048), rreg(rng), 0, rd, 0x1B))
        else:
            sh = rng.randrange(32) | (0x400 if f3 == 5 and rng.random() < .5 else 0)
            p.emit(i_type(sh, rreg(rng), f3, rd, 0x1B))
    elif k < 0.58:
        p.emit(u_type(rng.randrange(1 << 20), rd, rng.choice([0x37, 0x17])))
    elif k < 0.72:                                              # load
        f3 = rng.choice([0, 1, 2, 3, 4, 5, 6])
        size = 1 << (f3 & 3)
        off = rng.randrange(-256, 256) * (size if rng.random() < .85 else 1)
        p.emit(i_type(off, 3, f3, rd if rng.random() < .97 else 0, 0x03))
    elif k < 0.84:                                              # store
        f3 = rng.randrange(4)
        size = 1 << f3
        off = rng.randrange(-256, 256) * (size if rng.random() < .85 else 1)
        p.emit(s_type(off, rreg(rng), 3, f3, 0x23))
    elif k < 0.95:                                              # compressed
        compressed(p, rng)
    elif k < 0.975:
        p.emit(0xFFFFFFFF)                                      # illegal: traps
    else:
        p.emit(0x00000073)                                      # ecall: traps


FP_PATTERNS = [0, 1 << 63, 0x3FF0000000000000, 0xBFF8000000000000, 0x7FF0000000000000,
               0xFFF0000000000000, 0x7FF8000000000000, 0x7FF0000000000001, 0x7FEFFFFFFFFFFFFF,
               0x0000000000000001, 0x0010000000000000, 0x3FB999999999999A, 0x43E0000000000000,
               0x41DFFFFFFFE00000, 0xC3E0000000000000, 0x41F0000000000000,
               0xFFFFFFFF3F800000, 0xFFFFFFFF7F800000, 0xFFFFFFFF7FC00000, 0xFFFFFFFF7F800001,
               0xFFFFFFFF00000001, 0xFFFFFFFF80000000, 0xFFFFFFFF4F000000, 0xFFFFFFFFCF000000,
               0xFFFFFFFF3DCCCCCD, 0x000000003F800000]


def fp_value(p, rng, fd):
    """Put an interesting bit pattern in f[fd] (via x31)."""
    v = rng.choice(FP_PATTERNS) if rng.random() < .8 else rng.getrandbits(64)
    li(p, 31, v)
    p.emit(r_type(0x79, 0, 31, 0, fd, 0x53))                   # fmv.d.x


def fp_op(p, rng):
    fd, fa, fb, fc = (rng.randrange(32) for _ in range(4))
    rd = rng.choice(FREE)
    fmt = rng.randrange(2)
    rm = rng.choice([0, 1, 2, 3, 4, 7, 7, 7])
    k = rng.randrange(13)
    if k < 4:                                                   # add sub mul div
        p.emit(r_type((k << 2) | fmt, fb, fa, rm, fd, 0x53))
    elif k == 4:                                                # sqrt
        p.emit(r_type((0x0B << 2) | fmt, 0, fa, rm, fd, 0x53))
    elif k == 5:                                                # sgnj*
        p.emit(r_type((0x04 << 2) | fmt, fb, fa, rng.randrange(3), fd, 0x53))
    elif k == 6:                                                # min/max
        p.emit(r_type((0x05 << 2) | fmt, fb, fa, rng.randrange(2), fd, 0x53))
    elif k == 7:                                                # fcvt.s.d / fcvt.d.s
        p.emit(r_type((0x08 << 2) | fmt, 1 - fmt, fa, rm, fd, 0x53))
    elif k == 8:                                                # compares -> x
        p.emit(r_type((0x14 << 2) | fmt, fb, fa, rng.randrange(3), rd, 0x53))
    elif k == 9:                                                # fcvt.{w,wu,l,lu}
        p.emit(r_type((0x18 << 2) | fmt, rng.randrange(4), fa, rm, rd, 0x53))
    elif k == 10:                                               # fcvt from integer
        p.emit(r_type((0x1A << 2) | fmt, rng.randrange(4), rreg(rng), rm, fd, 0x53))
    elif k == 11:                                               # fclass / fmv.x
        p.emit(r_type((0x1C << 2) | fmt, 0, fa, rng.randrange(2), rd, 0x53))
    else:                                                       # fused multiply-add
        op = rng.choice([0x43, 0x47, 0x4B, 0x4F])
        p.emit((fc << 27) | (fmt << 25) | (fb << 20) | (fa << 15) | (rm << 12) | (fd << 7) | op)


def fp_block(p, rng):
    for _ in range(rng.randrange(1, 4)):
        fp_value(p, rng, rng.randrange(32))
    if rng.random() < .3:
        p.emit(i_type(0x002, rng.randrange(5), 5, 0, 0x73))   # csrwi frm, 0..4
    for _ in range(rng.randrange(1, 6)):
        fp_op(p, rng)
    if rng.random() < .5:
        p.emit(i_type(0x001, 0, 2, rng.choice(FREE), 0x73))   # csrr rd, fflags


def compressed(p, rng):
    k = rng.randrange(9)
    rd = rng.choice(FREE)
    cd = rng.choice(CREGS)
    cs = rng.choice(CREGS)
    if k == 0:                                                  # c.addi
        imm = rng.choice([i for i in range(-32, 32) if i])
        p.emitc(0x0001 | ((imm & 0x20) << 7) | (rd << 7) | ((imm & 0x1F) << 2))
    elif k == 1:                                                # c.li
        imm = rng.randrange(-32, 32)
        p.emitc(0x4001 | ((imm & 0x20) << 7) | (rd << 7) | ((imm & 0x1F) << 2))
    elif k == 2:                                                # c.mv
        p.emitc(0x8002 | (rd << 7) | (rng.choice(FREE) << 2))
    elif k == 3:                                                # c.add
        p.emitc(0x9002 | (rd << 7) | (rng.choice(FREE) << 2))
    elif k == 4:                                                # c.slli
        sh = rng.randrange(1, 64)
        p.emitc(0x0002 | ((sh & 0x20) << 7) | (rd << 7) | ((sh & 0x1F) << 2))
    elif k == 5:                                                # c.srli / c.srai
        sh = rng.randrange(1, 64)
        f2 = rng.randrange(2)
        p.emitc(0x8001 | ((sh & 0x20) << 7) | (f2 << 10) | ((cd - 8) << 7) | ((sh & 0x1F) << 2))
    elif k == 6:                                                # c.andi
        imm = rng.randrange(-32, 32)
        p.emitc(0x8801 | ((imm & 0x20) << 7) | ((cd - 8) << 7) | ((imm & 0x1F) << 2))
    elif k == 7:                                                # c.sub/xor/or/and
        f = rng.randrange(4)
        p.emitc(0x8C01 | ((cd - 8) << 7) | (f << 5) | ((cs - 8) << 2))
    else:                                                       # c.subw / c.addw
        f = rng.randrange(2)
        p.emitc(0x9C01 | ((cd - 8) << 7) | (f << 5) | ((cs - 8) << 2))


def forward_branch(p, rng, depth):
    """if (cond) skip a few instructions -- the shape compiled as a Python if."""
    f3 = rng.choice([0, 1, 4, 5, 6, 7])
    a, b = rreg(rng), rreg(rng)
    if rng.random() < .3 and depth == 0:
        # compressed beqz/bnez over the body
        reg = rng.choice(CREGS)
        start = p.here()
        idx = len(p.items)
        p.emitc(0)
        body(p, rng, rng.randrange(1, 5), 3)          # short: c.beqz reaches 256 bytes
        dist = p.here() - start

        def enc(pc, dist=dist, reg=reg, f=rng.randrange(2)):
            imm = dist & 0x1FF
            return (0xC001 if f == 0 else 0xE001) | ((reg - 8) << 7) | \
                (((imm >> 8) & 1) << 12) | (((imm >> 3) & 3) << 10) | \
                (((imm >> 6) & 3) << 5) | (((imm >> 1) & 3) << 3) | (((imm >> 5) & 1) << 2)
        p.items[idx][1] = enc
        return
    start = p.here()
    idx = len(p.items)
    p.emit(0)
    body(p, rng, rng.randrange(1, 6), depth + 1)
    dist = p.here() - start
    p.items[idx][1] = b_type(dist, b, a, f3)


def loop(p, rng, depth):
    """A counted loop on x4 (never nested, so x4 is free to use)."""
    li(p, 4, rng.randrange(1, 12))
    top = p.here()
    body(p, rng, rng.randrange(1, 8), depth + 1, in_loop=True)
    p.emit(i_type(-1, 4, 0, 4, 0x13))                           # addi x4, x4, -1
    p.emit(b_type(top - p.here(), 0, 4, 1))                     # bnez x4, top


def body(p, rng, n, depth, in_loop=False):
    for _ in range(n):
        k = rng.random()
        if k < 0.12 and depth < 3:
            forward_branch(p, rng, depth)
        elif k < 0.17 and depth == 0 and not in_loop:
            loop(p, rng, depth)
        elif k < 0.26 and FP and depth < 3:     # (depth 3: inside a short c.beqz)
            fp_block(p, rng)
        elif k < 0.29 and depth == 0:
            sub = rng.randrange(NSUBS)
            idx = len(p.items)
            p.emit(0)
            p.items[idx][1] = lambda pc, sub=sub: j_type(p.labels['sub%d' % sub] - pc, 1)
        else:
            straight(p, rng)


NSUBS = 4
FP = True


def make_program(seed):
    rng = random.Random(seed)
    p = Prog()
    li(p, 3, DATA)
    for r in FREE:
        li(p, r, rng.choice(INTERESTING) if rng.random() < .5 else rng.getrandbits(64))
    body(p, rng, rng.randrange(80, 200), 0)
    li(p, 29, TOHOST)
    p.emit(i_type(1, 0, 0, 28, 0x13))                           # li x28, 1
    p.emit(s_type(0, 28, 29, 3, 0x23))                          # sd x28, 0(x29)
    p.emit(j_type(0, 0))                                        # j .
    for s in range(NSUBS):
        p.label('sub%d' % s)
        body(p, rng, rng.randrange(2, 10), 1)
        p.emit(i_type(0, 1, 0, 0, 0x67))                        # ret
    return p.assemble(), rng.getrandbits(64)


def handler():
    p = Prog()
    p.emit(i_type(0x341, 0, 2, 30, 0x73))                      # csrr x30, mepc
    p.emit(i_type(4, 30, 0, 30, 0x13))                         # addi x30, x30, 4
    p.emit(i_type(0x341, 30, 1, 0, 0x73))                      # csrw mepc, x30
    p.emit(i_type(1, 2, 0, 2, 0x13))                           # addi x2, x2, 1
    p.emit(0x30200073)                                         # mret
    return p.assemble()


def run(code, data_seed, mode):
    """mode: 'interp', 'jit' or 'native'."""
    native.ENABLED = mode == 'native'
    compiled = mode == 'jit'
    m = Machine(BufferConsole(), mem_mb=16, realtime=False)
    if mode == 'native' and m.cpu.native is None:
        return None, 0
    jit = m.cpu.jit
    jit.use_worker = False
    jit.max_workers = 0
    jit.cache_path = None
    jit.codes.clear()
    jit.disk = {}
    jit.hot = 1 if compiled else -1
    m.load_bytes(HANDLER, handler())
    m.load_bytes(CODE, code)
    rng = random.Random(data_seed)
    m.load_bytes(DATA - 4096, bytes(rng.getrandbits(8) for _ in range(8192)))
    cpu = m.cpu
    cpu.priv = 3
    cpu.pc = CODE
    cpu.mtvec = HANDLER
    cpu.mstatus |= 3 << 13                                      # FPU on
    cpu.update_tlbs()
    m.htif = (TOHOST, None)
    m.run(2_000_000)
    reason = getattr(m, 'halt_reason', '')
    o = DATA - 4096 - RAM_BASE
    return (reason, list(cpu.x[:32]), bytes(m.ram[o:o + 8192]),
            list(cpu.f[:32]), cpu.fflags, cpu.frm), cpu.jit.compiled


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seeds', type=int, default=40, help='number of programs')
    ap.add_argument('--seed', type=int, help='run only this program')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args()
    seeds = [a.seed] if a.seed is not None else range(a.seeds)
    bad = 0
    regions = 0
    natives = 0
    for seed in seeds:
        code, dseed = make_program(seed)
        want, _ = run(code, dseed, 'interp')
        got, n = run(code, dseed, 'jit')
        regions += n
        nat, _ = run(code, dseed, 'native')
        if nat is not None and nat != want and got == want:
            got, label = nat, 'native core'
        else:
            label = 'compiled'
        natives += nat is not None
        if want[0] != 'htif exit':
            print('seed %d: interpreter did not finish (%s)' % (seed, want[0]))
            bad += 1
            continue
        if got != want or (nat is not None and nat != want):
            bad += 1
            print('seed %d: MISMATCH (%s)' % (seed, label))
            if got[0] != want[0]:
                print('  halt: %s %r, interpreted %r' % (label, got[0], want[0]))
            for r in range(32):
                if got[1][r] != want[1][r]:
                    print('  x%d: %s %#x, interpreted %#x' % (r, label, got[1][r], want[1][r]))
            for r in range(32):
                if got[3][r] != want[3][r]:
                    print('  f%d: %s %#x, interpreted %#x' % (r, label, got[3][r], want[3][r]))
            if got[4:] != want[4:]:
                print('  fflags/frm: %s %s, interpreted %s' % (label, got[4:], want[4:]))
            if got[2] != want[2]:
                diff = [i for i in range(len(got[2])) if got[2][i] != want[2][i]]
                print('  memory differs at %d bytes, first at DATA%+d'
                      % (len(diff), diff[0] - 4096))
        elif a.verbose:
            print('seed %d: ok (%d bytes of code, %d traps)' % (seed, len(code), want[1][2]))
    print('%d/%d programs agree (%d regions compiled, native core %s)'
          % (len(seeds) - bad, len(seeds), regions,
             'checked' if natives else 'not available: %s' % native.error))
    return 1 if bad or not regions else 0


if __name__ == '__main__':
    sys.exit(main())
