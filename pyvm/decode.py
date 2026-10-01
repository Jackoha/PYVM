"""Instruction decoder for RV64GC.

Every instruction word is turned into a small Python closure ``h(pc) -> next_pc``
that performs the instruction.  Handlers only depend on the instruction bits
(never on the address they live at), so they are memoized per instruction word
and shared by every location that contains the same instruction.

Register writes to x0 are redirected to a junk slot (index 32), which saves a
branch in every handler.
"""

from . import fpu
from .fpu import F32, F64, BOX, unbox
from .errors import Trap, Break

M64 = (1 << 64) - 1
ME = M64 - 1            # mask that also clears bit 0 (JALR)
S64 = 1 << 63
M32 = 0xFFFFFFFF
HI32 = 0xFFFFFFFF00000000
FS = 3 << 13            # mstatus.FS


def sx(v, bits):
    m = 1 << (bits - 1)
    return ((v & ((1 << bits) - 1)) ^ m) - m


def build(cpu):
    """Return decode(insn) -> handler, bound to one CPU."""
    x = cpu.x
    fr = cpu.f
    mB, mb, mH, mh, mI, mi, mQ = cpu.mB, cpu.mb, cpu.mH, cpu.mh, cpu.mI, cpu.mi, cpu.mQ
    load = cpu.load_slow
    store = cpu.store_slow
    # data-TLB lookups live in closure cells; the CPU rebinds them whenever it
    # switches TLB sets (privilege / SUM / MXR changes)
    rget = wget = None

    def set_tlb_cells(rtlb, wtlb):
        nonlocal rget, wget
        rget = rtlb.get
        wget = wtlb.get
    cpu.set_tlb_cells = set_tlb_cells

    # ================================================================ misc
    def illegal(insn):
        def h(pc):
            raise Trap(2, insn)
        return h

    def _nop(il):
        def h(pc):
            return pc + il
        return h
    NOP = {2: _nop(2), 4: _nop(4)}

    # ================================================================ ALU imm
    def ADDI(rd, a, imm, il):
        if rd == 0:
            return NOP[il]
        if a == 0:
            v = imm & M64

            def h(pc):
                x[rd] = v
                return pc + il
        elif imm == 0:
            def h(pc):
                x[rd] = x[a]
                return pc + il
        else:
            def h(pc):
                x[rd] = (x[a] + imm) & M64
                return pc + il
        return h

    def SLTI(rd, a, imm, il):
        rd = rd or 32
        k = (imm & M64) ^ S64

        def h(pc):
            x[rd] = 1 if (x[a] ^ S64) < k else 0
            return pc + il
        return h

    def SLTIU(rd, a, imm, il):
        rd = rd or 32
        k = imm & M64

        def h(pc):
            x[rd] = 1 if x[a] < k else 0
            return pc + il
        return h

    def XORI(rd, a, imm, il):
        rd = rd or 32
        k = imm & M64

        def h(pc):
            x[rd] = x[a] ^ k
            return pc + il
        return h

    def ORI(rd, a, imm, il):
        rd = rd or 32
        k = imm & M64

        def h(pc):
            x[rd] = x[a] | k
            return pc + il
        return h

    def ANDI(rd, a, imm, il):
        rd = rd or 32
        k = imm & M64

        def h(pc):
            x[rd] = x[a] & k
            return pc + il
        return h

    def SLLI(rd, a, sh, il):
        rd = rd or 32

        def h(pc):
            x[rd] = (x[a] << sh) & M64
            return pc + il
        return h

    def SRLI(rd, a, sh, il):
        rd = rd or 32

        def h(pc):
            x[rd] = x[a] >> sh
            return pc + il
        return h

    def SRAI(rd, a, sh, il):
        rd = rd or 32

        def h(pc):
            x[rd] = (((x[a] ^ S64) - S64) >> sh) & M64
            return pc + il
        return h

    def ADDIW(rd, a, imm, il):
        rd = rd or 32

        def h(pc):
            v = (x[a] + imm) & M32
            x[rd] = (v | HI32) if v & 0x80000000 else v
            return pc + il
        return h

    def SLLIW(rd, a, sh, il):
        rd = rd or 32

        def h(pc):
            v = (x[a] << sh) & M32
            x[rd] = (v | HI32) if v & 0x80000000 else v
            return pc + il
        return h

    def SRLIW(rd, a, sh, il):
        rd = rd or 32

        def h(pc):
            v = (x[a] & M32) >> sh
            x[rd] = (v | HI32) if v & 0x80000000 else v
            return pc + il
        return h

    def SRAIW(rd, a, sh, il):
        rd = rd or 32

        def h(pc):
            v = x[a] & M32
            x[rd] = (((v ^ 0x80000000) - 0x80000000) >> sh) & M64
            return pc + il
        return h

    def LUI(rd, imm, il):
        if rd == 0:
            return NOP[il]
        v = imm & M64

        def h(pc):
            x[rd] = v
            return pc + il
        return h

    def AUIPC(rd, imm, il):
        if rd == 0:
            return NOP[il]

        def h(pc):
            x[rd] = (pc + imm) & M64
            return pc + il
        return h

    # ================================================================ ALU reg
    def ADD(rd, a, b, il):
        if rd == 0:
            return NOP[il]
        if a == 0:
            return ADDI(rd, b, 0, il)

        def h(pc):
            x[rd] = (x[a] + x[b]) & M64
            return pc + il
        return h

    def SUB(rd, a, b, il):
        rd = rd or 32

        def h(pc):
            x[rd] = (x[a] - x[b]) & M64
            return pc + il
        return h

    def SLL(rd, a, b, il):
        rd = rd or 32

        def h(pc):
            x[rd] = (x[a] << (x[b] & 63)) & M64
            return pc + il
        return h

    def SLT(rd, a, b, il):
        rd = rd or 32

        def h(pc):
            x[rd] = 1 if (x[a] ^ S64) < (x[b] ^ S64) else 0
            return pc + il
        return h

    def SLTU(rd, a, b, il):
        rd = rd or 32

        def h(pc):
            x[rd] = 1 if x[a] < x[b] else 0
            return pc + il
        return h

    def XOR(rd, a, b, il):
        rd = rd or 32

        def h(pc):
            x[rd] = x[a] ^ x[b]
            return pc + il
        return h

    def SRL(rd, a, b, il):
        rd = rd or 32

        def h(pc):
            x[rd] = x[a] >> (x[b] & 63)
            return pc + il
        return h

    def SRA(rd, a, b, il):
        rd = rd or 32

        def h(pc):
            x[rd] = (((x[a] ^ S64) - S64) >> (x[b] & 63)) & M64
            return pc + il
        return h

    def OR(rd, a, b, il):
        rd = rd or 32

        def h(pc):
            x[rd] = x[a] | x[b]
            return pc + il
        return h

    def AND(rd, a, b, il):
        rd = rd or 32

        def h(pc):
            x[rd] = x[a] & x[b]
            return pc + il
        return h

    def ADDW(rd, a, b, il):
        rd = rd or 32

        def h(pc):
            v = (x[a] + x[b]) & M32
            x[rd] = (v | HI32) if v & 0x80000000 else v
            return pc + il
        return h

    def SUBW(rd, a, b, il):
        rd = rd or 32

        def h(pc):
            v = (x[a] - x[b]) & M32
            x[rd] = (v | HI32) if v & 0x80000000 else v
            return pc + il
        return h

    def SLLW(rd, a, b, il):
        rd = rd or 32

        def h(pc):
            v = (x[a] << (x[b] & 31)) & M32
            x[rd] = (v | HI32) if v & 0x80000000 else v
            return pc + il
        return h

    def SRLW(rd, a, b, il):
        rd = rd or 32

        def h(pc):
            v = (x[a] & M32) >> (x[b] & 31)
            x[rd] = (v | HI32) if v & 0x80000000 else v
            return pc + il
        return h

    def SRAW(rd, a, b, il):
        rd = rd or 32

        def h(pc):
            v = x[a] & M32
            x[rd] = (((v ^ 0x80000000) - 0x80000000) >> (x[b] & 31)) & M64
            return pc + il
        return h

    def RR(rd, a, b, il, fn):
        """Generic register-register op (used for the M extension)."""
        rd = rd or 32

        def h(pc):
            x[rd] = fn(x[a], x[b])
            return pc + il
        return h

    # ================================================================ loads
    def LB(rd, a, imm, il):
        rd = rd or 32

        def h(pc):
            ad = (x[a] + imm) & M64
            d = rget(ad >> 12)
            if d is None:
                v = load(ad, 1)
                x[rd] = (v | 0xFFFFFFFFFFFFFF00) if v & 0x80 else v
            else:
                x[rd] = mb[ad + d] & M64
            return pc + il
        return h

    def LH(rd, a, imm, il):
        rd = rd or 32

        def h(pc):
            ad = (x[a] + imm) & M64
            d = rget(ad >> 12)
            if d is None or ad & 1:
                v = load(ad, 2)
                x[rd] = (v | 0xFFFFFFFFFFFF0000) if v & 0x8000 else v
            else:
                x[rd] = mh[(ad + d) >> 1] & M64
            return pc + il
        return h

    def LW(rd, a, imm, il):
        rd = rd or 32

        def h(pc):
            ad = (x[a] + imm) & M64
            d = rget(ad >> 12)
            if d is None or ad & 3:
                v = load(ad, 4)
                x[rd] = (v | HI32) if v & 0x80000000 else v
            else:
                x[rd] = mi[(ad + d) >> 2] & M64
            return pc + il
        return h

    def LD(rd, a, imm, il):
        rd = rd or 32

        def h(pc):
            ad = (x[a] + imm) & M64
            d = rget(ad >> 12)
            if d is None or ad & 7:
                x[rd] = load(ad, 8)
            else:
                x[rd] = mQ[(ad + d) >> 3]
            return pc + il
        return h

    def LBU(rd, a, imm, il):
        rd = rd or 32

        def h(pc):
            ad = (x[a] + imm) & M64
            d = rget(ad >> 12)
            if d is None:
                x[rd] = load(ad, 1)
            else:
                x[rd] = mB[ad + d]
            return pc + il
        return h

    def LHU(rd, a, imm, il):
        rd = rd or 32

        def h(pc):
            ad = (x[a] + imm) & M64
            d = rget(ad >> 12)
            if d is None or ad & 1:
                x[rd] = load(ad, 2)
            else:
                x[rd] = mH[(ad + d) >> 1]
            return pc + il
        return h

    def LWU(rd, a, imm, il):
        rd = rd or 32

        def h(pc):
            ad = (x[a] + imm) & M64
            d = rget(ad >> 12)
            if d is None or ad & 3:
                x[rd] = load(ad, 4)
            else:
                x[rd] = mI[(ad + d) >> 2]
            return pc + il
        return h

    # ================================================================ stores
    def SB(a, b, imm, il):
        def h(pc):
            ad = (x[a] + imm) & M64
            d = wget(ad >> 12)
            if d is None:
                store(ad, 1, x[b] & 0xFF)
            else:
                mB[ad + d] = x[b] & 0xFF
            return pc + il
        return h

    def SH(a, b, imm, il):
        def h(pc):
            ad = (x[a] + imm) & M64
            d = wget(ad >> 12)
            if d is None or ad & 1:
                store(ad, 2, x[b] & 0xFFFF)
            else:
                mH[(ad + d) >> 1] = x[b] & 0xFFFF
            return pc + il
        return h

    def SW(a, b, imm, il):
        def h(pc):
            ad = (x[a] + imm) & M64
            d = wget(ad >> 12)
            if d is None or ad & 3:
                store(ad, 4, x[b] & M32)
            else:
                mI[(ad + d) >> 2] = x[b] & M32
            return pc + il
        return h

    def SD(a, b, imm, il):
        def h(pc):
            ad = (x[a] + imm) & M64
            d = wget(ad >> 12)
            if d is None or ad & 7:
                store(ad, 8, x[b])
            else:
                mQ[(ad + d) >> 3] = x[b]
            return pc + il
        return h

    # ================================================================ control
    def BEQ(a, b, imm, il):
        if b == 0:
            def h(pc):
                if x[a]:
                    return pc + il
                return (pc + imm) & M64
        else:
            def h(pc):
                if x[a] == x[b]:
                    return (pc + imm) & M64
                return pc + il
        return h

    def BNE(a, b, imm, il):
        if b == 0:
            def h(pc):
                if x[a]:
                    return (pc + imm) & M64
                return pc + il
        else:
            def h(pc):
                if x[a] != x[b]:
                    return (pc + imm) & M64
                return pc + il
        return h

    def BLT(a, b, imm, il):
        def h(pc):
            if (x[a] ^ S64) < (x[b] ^ S64):
                return (pc + imm) & M64
            return pc + il
        return h

    def BGE(a, b, imm, il):
        def h(pc):
            if (x[a] ^ S64) >= (x[b] ^ S64):
                return (pc + imm) & M64
            return pc + il
        return h

    def BLTU(a, b, imm, il):
        def h(pc):
            if x[a] < x[b]:
                return (pc + imm) & M64
            return pc + il
        return h

    def BGEU(a, b, imm, il):
        def h(pc):
            if x[a] >= x[b]:
                return (pc + imm) & M64
            return pc + il
        return h

    def JAL(rd, imm, il):
        if rd == 0:
            def h(pc):
                return (pc + imm) & M64
        else:
            def h(pc):
                x[rd] = pc + il
                return (pc + imm) & M64
        return h

    def JALR(rd, a, imm, il):
        if rd == 0:
            def h(pc):
                return (x[a] + imm) & ME
        else:
            def h(pc):
                t = (x[a] + imm) & ME
                x[rd] = pc + il
                return t
        return h

    # ================================================================ atomics
    def _amo_fn(f5, w):
        if w:
            def smin(o, v):
                return o if (o ^ 0x80000000) < (v ^ 0x80000000) else v

            def smax(o, v):
                return o if (o ^ 0x80000000) > (v ^ 0x80000000) else v
            m = M32
        else:
            def smin(o, v):
                return o if (o ^ S64) < (v ^ S64) else v

            def smax(o, v):
                return o if (o ^ S64) > (v ^ S64) else v
            m = M64
        return {
            0x01: lambda o, v: v,
            0x00: lambda o, v: (o + v) & m,
            0x04: lambda o, v: o ^ v,
            0x0C: lambda o, v: o & v,
            0x08: lambda o, v: o | v,
            0x10: smin,
            0x14: smax,
            0x18: lambda o, v: o if o < v else v,
            0x1C: lambda o, v: o if o > v else v,
        }.get(f5)

    def AMO_W(rd, a, b, fn, il):
        rd = rd or 32

        def h(pc):
            ad = x[a]
            if ad & 3:
                raise Trap(6, ad)
            o = cpu.amo_offset(ad) >> 2
            old = mI[o]
            mI[o] = fn(old, x[b] & M32)
            x[rd] = (old | HI32) if old & 0x80000000 else old
            return pc + il
        return h

    def AMO_D(rd, a, b, fn, il):
        rd = rd or 32

        def h(pc):
            ad = x[a]
            if ad & 7:
                raise Trap(6, ad)
            o = cpu.amo_offset(ad) >> 3
            old = mQ[o]
            mQ[o] = fn(old, x[b])
            x[rd] = old
            return pc + il
        return h

    def LR(rd, a, size, il):
        rd = rd or 32

        def h(pc):
            ad = x[a]
            if ad & (size - 1):
                raise Trap(4, ad)
            v = load(ad, size)
            if size == 4 and v & 0x80000000:
                v |= HI32
            cpu.reservation = ad
            x[rd] = v
            return pc + il
        return h

    def SC(rd, a, b, size, il):
        rd = rd or 32

        def h(pc):
            ad = x[a]
            if ad & (size - 1):
                raise Trap(6, ad)
            if cpu.reservation == ad:
                o = cpu.amo_offset(ad)
                if size == 4:
                    mI[o >> 2] = x[b] & M32
                else:
                    mQ[o >> 3] = x[b]
                cpu.reservation = -1
                x[rd] = 0
            else:
                cpu.reservation = -1
                x[rd] = 1
            return pc + il
        return h

    # ================================================================ system
    def ECALL(il):
        def h(pc):
            raise Trap(8 + cpu.priv, 0)
        return h

    def EBREAK(il):
        def h(pc):
            raise Trap(3, pc)
        return h

    def SYS(method, il, insn, *args):
        def h(pc):
            return method(pc, il, insn, *args)
        return h

    def CSR(rd, csr, op, src, is_imm, il, insn):
        rd = rd or 32
        do_write = op == 1 or src != 0
        csr_op = cpu.csr_op

        def h(pc):
            old, brk = csr_op(csr, op, src if is_imm else x[src], do_write, insn)
            x[rd] = old
            if brk:
                cpu.pc = pc + il
                raise Break()
            return pc + il
        return h

    # ================================================================ floating point
    def fget(F):
        if F is F32:
            return lambda r: unbox(fr[r])
        return lambda r: fr[r]

    def fput(F):
        if F is F32:
            def p(r, v):
                fr[r] = v | BOX
        else:
            def p(r, v):
                fr[r] = v
        return p

    def rounding(rm, insn):
        if rm == 7:
            r = cpu.frm
            if r > 4:
                raise Trap(2, insn)
            return r
        return rm

    def FLOAD(rd, a, imm, size, il, insn):
        def h(pc):
            if not cpu.mstatus & FS:
                raise Trap(2, insn)
            ad = (x[a] + imm) & M64
            d = rget(ad >> 12)
            if size == 8:
                if d is None or ad & 7:
                    fr[rd] = load(ad, 8)
                else:
                    fr[rd] = mQ[(ad + d) >> 3]
            else:
                if d is None or ad & 3:
                    fr[rd] = load(ad, 4) | BOX
                else:
                    fr[rd] = mI[(ad + d) >> 2] | BOX
            cpu.mstatus |= FS
            return pc + il
        return h

    def FSTORE(a, b, imm, size, il, insn):
        def h(pc):
            if not cpu.mstatus & FS:
                raise Trap(2, insn)
            ad = (x[a] + imm) & M64
            d = wget(ad >> 12)
            if size == 8:
                if d is None or ad & 7:
                    store(ad, 8, fr[b])
                else:
                    mQ[(ad + d) >> 3] = fr[b]
            else:
                if d is None or ad & 3:
                    store(ad, 4, fr[b] & M32)
                else:
                    mI[(ad + d) >> 2] = fr[b] & M32
            return pc + il
        return h

    def FOP(rd, srcs, rm, F, fn, il, insn, to_x=False, src_x=False, dst_fmt=None):
        """Generic FP op.  fn(*operand_bits, rm) -> (result, flags)."""
        get = (lambda r: x[r]) if src_x else fget(F)
        put = fput(dst_fmt or F)
        if to_x:
            rdx = rd or 32

        def h(pc):
            if not cpu.mstatus & FS:
                raise Trap(2, insn)
            r = rounding(rm, insn)
            v, fl = fn(*[get(s) for s in srcs], r)
            if to_x:
                x[rdx] = v
            else:
                put(rd, v)
            if fl:
                cpu.fflags |= fl
            cpu.mstatus |= FS
            return pc + il
        return h

    def decode_opfp(i, rd, f3, rs1, rs2, il):
        f7 = i >> 25
        fmt = f7 & 3
        f5 = f7 >> 2
        if fmt == 0:
            F = F32
        elif fmt == 1:
            F = F64
        else:
            return illegal(i)
        rm = f3
        if f5 <= 3 or f5 == 0x0B or f5 in (0x08, 0x18, 0x1A):
            if rm in (5, 6):
                return illegal(i)
        if f5 == 0x00:
            return FOP(rd, (rs1, rs2), rm, F, lambda a, b, r: fpu.add(a, b, F, r), il, i)
        if f5 == 0x01:
            return FOP(rd, (rs1, rs2), rm, F, lambda a, b, r: fpu.sub(a, b, F, r), il, i)
        if f5 == 0x02:
            return FOP(rd, (rs1, rs2), rm, F, lambda a, b, r: fpu.mul(a, b, F, r), il, i)
        if f5 == 0x03:
            return FOP(rd, (rs1, rs2), rm, F, lambda a, b, r: fpu.div(a, b, F, r), il, i)
        if f5 == 0x0B and rs2 == 0:
            return FOP(rd, (rs1,), rm, F, lambda a, r: fpu.sqrt(a, F, r), il, i)
        if f5 == 0x04 and rm <= 2:
            return FOP(rd, (rs1, rs2), 0, F, lambda a, b, r: (fpu.sgnj(a, b, F, rm), 0), il, i)
        if f5 == 0x05 and rm <= 1:
            return FOP(rd, (rs1, rs2), 0, F, lambda a, b, r: fpu.minmax(a, b, F, rm == 1), il, i)
        if f5 == 0x08:
            if fmt == 0 and rs2 == 1:       # fcvt.s.d
                return FOP(rd, (rs1,), rm, F64, lambda a, r: fpu.convert(a, F64, F32, r),
                           il, i, dst_fmt=F32)
            if fmt == 1 and rs2 == 0:       # fcvt.d.s
                return FOP(rd, (rs1,), rm, F32, lambda a, r: fpu.convert(a, F32, F64, r),
                           il, i, dst_fmt=F64)
            return illegal(i)
        if f5 == 0x14 and rm <= 2:
            return FOP(rd, (rs1, rs2), 0, F, lambda a, b, r: fpu.compare(a, b, F, rm), il, i,
                       to_x=True)
        if f5 == 0x18 and rs2 <= 3:
            width = 32 if rs2 < 2 else 64
            signed = not (rs2 & 1)

            def cvt(a, r):
                v, fl = fpu.to_int(a, F, r, width, signed)
                if width == 32:
                    v &= M32
                    if v & 0x80000000:
                        v |= HI32
                return v & M64, fl
            return FOP(rd, (rs1,), rm, F, cvt, il, i, to_x=True)
        if f5 == 0x1A and rs2 <= 3:
            if rs2 == 0:
                conv = lambda v: ((v & M32) ^ 0x80000000) - 0x80000000
            elif rs2 == 1:
                conv = lambda v: v & M32
            elif rs2 == 2:
                conv = lambda v: (v ^ S64) - S64
            else:
                conv = lambda v: v
            return FOP(rd, (rs1,), rm, F, lambda v, r: fpu.from_int(conv(v), F, r), il, i,
                       src_x=True)
        if f5 == 0x1C and rs2 == 0:
            if rm == 0:
                if F is F32:
                    fn = lambda a, r: ((((a & M32) ^ 0x80000000) - 0x80000000) & M64, 0)
                    # fmv.x.w reads the raw low 32 bits (no NaN-box check)
                    return FOP(rd, (rs1,), 0, F64, fn, il, i, to_x=True)
                return FOP(rd, (rs1,), 0, F64, lambda a, r: (a, 0), il, i, to_x=True)
            if rm == 1:
                return FOP(rd, (rs1,), 0, F, lambda a, r: (fpu.classify(a, F), 0), il, i,
                           to_x=True)
        if f5 == 0x1E and rs2 == 0 and rm == 0:
            if F is F32:
                return FOP(rd, (rs1,), 0, F32, lambda v, r: (v & M32, 0), il, i, src_x=True)
            return FOP(rd, (rs1,), 0, F64, lambda v, r: (v, 0), il, i, src_x=True)
        return illegal(i)

    def decode_fma(i, op, rd, f3, rs1, rs2, il):
        fmt = (i >> 25) & 3
        if fmt > 1 or f3 in (5, 6):
            return illegal(i)
        F = F32 if fmt == 0 else F64
        rs3 = i >> 27
        neg_prod = op in (0x4B, 0x4F)   # fnmsub, fnmadd
        neg_c = op in (0x47, 0x4F)      # fmsub, fnmadd
        return FOP(rd, (rs1, rs2, rs3), f3, F,
                   lambda a, b, c, r: fpu.fma(a, b, c, F, r, neg_prod, neg_c), il, i)

    # ================================================================ M extension helpers
    def f_mul(a, b):
        return (a * b) & M64

    def f_mulh(a, b):
        return ((((a ^ S64) - S64) * ((b ^ S64) - S64)) >> 64) & M64

    def f_mulhsu(a, b):
        return ((((a ^ S64) - S64) * b) >> 64) & M64

    def f_mulhu(a, b):
        return (a * b) >> 64

    def f_div(a, b):
        if b == 0:
            return M64
        sa, sb = (a ^ S64) - S64, (b ^ S64) - S64
        q = abs(sa) // abs(sb)
        if (sa < 0) != (sb < 0):
            q = -q
        return q & M64          # overflow case wraps to -2^63 naturally

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

    def s32(v):
        return ((v & M32) ^ 0x80000000) - 0x80000000

    def f_mulw(a, b):
        return s32(a * b) & M64

    def f_divw(a, b):
        sa, sb = s32(a), s32(b)
        if sb == 0:
            return M64
        q = abs(sa) // abs(sb)
        if (sa < 0) != (sb < 0):
            q = -q
        return s32(q) & M64

    def f_divuw(a, b):
        a, b = a & M32, b & M32
        if b == 0:
            return M64
        return s32(a // b) & M64

    def f_remw(a, b):
        sa, sb = s32(a), s32(b)
        if sb == 0:
            return sa & M64
        r = abs(sa) % abs(sb)
        return (-r if sa < 0 else r) & M64

    def f_remuw(a, b):
        a, b = a & M32, b & M32
        if b == 0:
            return s32(a) & M64
        return s32(a % b) & M64

    OP_TABLE = {
        (0x00, 0): ADD, (0x20, 0): SUB, (0x00, 1): SLL, (0x00, 2): SLT, (0x00, 3): SLTU,
        (0x00, 4): XOR, (0x00, 5): SRL, (0x20, 5): SRA, (0x00, 6): OR, (0x00, 7): AND,
    }
    M_TABLE = {0: f_mul, 1: f_mulh, 2: f_mulhsu, 3: f_mulhu,
               4: f_div, 5: f_divu, 6: f_rem, 7: f_remu}
    OP32_TABLE = {(0x00, 0): ADDW, (0x20, 0): SUBW, (0x00, 1): SLLW, (0x00, 5): SRLW,
                  (0x20, 5): SRAW}
    M32_TABLE = {0: f_mulw, 4: f_divw, 5: f_divuw, 6: f_remw, 7: f_remuw}
    LOADS = {0: LB, 1: LH, 2: LW, 3: LD, 4: LBU, 5: LHU, 6: LWU}
    STORES = {0: SB, 1: SH, 2: SW, 3: SD}
    BRANCHES = {0: BEQ, 1: BNE, 4: BLT, 5: BGE, 6: BLTU, 7: BGEU}
    IMM_OPS = {0: ADDI, 2: SLTI, 3: SLTIU, 4: XORI, 6: ORI, 7: ANDI}

    # ================================================================ 32-bit decoder
    def decode32(i):
        op = i & 0x7F
        rd = (i >> 7) & 31
        f3 = (i >> 12) & 7
        rs1 = (i >> 15) & 31
        rs2 = (i >> 20) & 31
        f7 = i >> 25

        if op == 0x13:                                      # OP-IMM
            imm = sx(i >> 20, 12)
            if f3 == 1:
                if i >> 26:
                    return illegal(i)
                return SLLI(rd, rs1, (i >> 20) & 63, 4)
            if f3 == 5:
                f6 = i >> 26
                if f6 == 0:
                    return SRLI(rd, rs1, (i >> 20) & 63, 4)
                if f6 == 0x10:
                    return SRAI(rd, rs1, (i >> 20) & 63, 4)
                return illegal(i)
            return IMM_OPS[f3](rd, rs1, imm, 4)
        if op == 0x03:                                      # LOAD
            f = LOADS.get(f3)
            return f(rd, rs1, sx(i >> 20, 12), 4) if f else illegal(i)
        if op == 0x23:                                      # STORE
            f = STORES.get(f3)
            imm = sx(((i >> 25) << 5) | rd, 12)
            return f(rs1, rs2, imm, 4) if f else illegal(i)
        if op == 0x33:                                      # OP
            if f7 == 1:
                return RR(rd, rs1, rs2, 4, M_TABLE[f3])
            f = OP_TABLE.get((f7, f3))
            return f(rd, rs1, rs2, 4) if f else illegal(i)
        if op == 0x63:                                      # BRANCH
            f = BRANCHES.get(f3)
            imm = sx(((i >> 31) << 12) | (((i >> 7) & 1) << 11) |
                     (((i >> 25) & 0x3F) << 5) | (((i >> 8) & 0xF) << 1), 13)
            return f(rs1, rs2, imm, 4) if f else illegal(i)
        if op == 0x37:
            return LUI(rd, sx(i & 0xFFFFF000, 32), 4)
        if op == 0x17:
            return AUIPC(rd, sx(i & 0xFFFFF000, 32), 4)
        if op == 0x6F:
            imm = sx(((i >> 31) << 20) | (((i >> 12) & 0xFF) << 12) |
                     (((i >> 20) & 1) << 11) | (((i >> 21) & 0x3FF) << 1), 21)
            return JAL(rd, imm, 4)
        if op == 0x67:
            if f3:
                return illegal(i)
            return JALR(rd, rs1, sx(i >> 20, 12), 4)
        if op == 0x1B:                                      # OP-IMM-32
            if f3 == 0:
                return ADDIW(rd, rs1, sx(i >> 20, 12), 4)
            if f3 == 1 and f7 == 0:
                return SLLIW(rd, rs1, rs2, 4)
            if f3 == 5 and f7 == 0:
                return SRLIW(rd, rs1, rs2, 4)
            if f3 == 5 and f7 == 0x20:
                return SRAIW(rd, rs1, rs2, 4)
            return illegal(i)
        if op == 0x3B:                                      # OP-32
            if f7 == 1:
                fn = M32_TABLE.get(f3)
                return RR(rd, rs1, rs2, 4, fn) if fn else illegal(i)
            f = OP32_TABLE.get((f7, f3))
            return f(rd, rs1, rs2, 4) if f else illegal(i)
        if op == 0x73:                                      # SYSTEM
            if f3 == 0:
                if i == 0x00000073:
                    return ECALL(4)
                if i == 0x00100073:
                    return EBREAK(4)
                if i == 0x10200073:
                    return SYS(cpu.op_sret, 4, i)
                if i == 0x30200073:
                    return SYS(cpu.op_mret, 4, i)
                if i == 0x10500073:
                    return SYS(cpu.op_wfi, 4, i)
                if f7 == 0x09 and rd == 0:
                    return SYS(cpu.op_sfence_vma, 4, i, rs1, rs2)
                return illegal(i)
            if f3 == 4:
                return illegal(i)
            return CSR(rd, i >> 20, f3 & 3, rs1, f3 >= 5, 4, i)
        if op == 0x0F:                                      # MISC-MEM
            if f3 in (0, 1):
                return NOP[4]                               # fence / fence.i
            return illegal(i)
        if op == 0x2F:                                      # AMO
            if f3 not in (2, 3):
                return illegal(i)
            size = 4 if f3 == 2 else 8
            f5 = i >> 27
            if f5 == 0x02:
                return LR(rd, rs1, size, 4) if rs2 == 0 else illegal(i)
            if f5 == 0x03:
                return SC(rd, rs1, rs2, size, 4)
            fn = _amo_fn(f5, size == 4)
            if fn is None:
                return illegal(i)
            return (AMO_W if size == 4 else AMO_D)(rd, rs1, rs2, fn, 4)
        if op == 0x07:                                      # LOAD-FP
            if f3 in (2, 3):
                return FLOAD(rd, rs1, sx(i >> 20, 12), 4 if f3 == 2 else 8, 4, i)
            return illegal(i)
        if op == 0x27:                                      # STORE-FP
            if f3 in (2, 3):
                imm = sx(((i >> 25) << 5) | rd, 12)
                return FSTORE(rs1, rs2, imm, 4 if f3 == 2 else 8, 4, i)
            return illegal(i)
        if op == 0x53:
            return decode_opfp(i, rd, f3, rs1, rs2, 4)
        if op in (0x43, 0x47, 0x4B, 0x4F):
            return decode_fma(i, op, rd, f3, rs1, rs2, 4)
        return illegal(i)

    # ================================================================ 16-bit decoder
    def decode16(i):
        op = i & 3
        f3 = i >> 13
        if op == 0:
            rdp = 8 + ((i >> 2) & 7)
            rs1p = 8 + ((i >> 7) & 7)
            imm_w = ((i >> 7) & 0x38) | ((i >> 4) & 0x4) | ((i << 1) & 0x40)
            imm_d = ((i >> 7) & 0x38) | ((i << 1) & 0xC0)
            if f3 == 0:
                imm = ((i >> 7) & 0x30) | ((i >> 1) & 0x3C0) | ((i >> 4) & 0x4) | ((i >> 2) & 0x8)
                if imm == 0:
                    return illegal(i)
                return ADDI(rdp, 2, imm, 2)
            if f3 == 1:
                return FLOAD(rdp, rs1p, imm_d, 8, 2, i)
            if f3 == 2:
                return LW(rdp, rs1p, imm_w, 2)
            if f3 == 3:
                return LD(rdp, rs1p, imm_d, 2)
            if f3 == 5:
                return FSTORE(rs1p, rdp, imm_d, 8, 2, i)
            if f3 == 6:
                return SW(rs1p, rdp, imm_w, 2)
            if f3 == 7:
                return SD(rs1p, rdp, imm_d, 2)
            return illegal(i)

        if op == 1:
            rd = (i >> 7) & 31
            imm6 = sx(((i >> 7) & 0x20) | ((i >> 2) & 0x1F), 6)
            if f3 == 0:
                return ADDI(rd, rd, imm6, 2)
            if f3 == 1:
                return ADDIW(rd, rd, imm6, 2) if rd else illegal(i)
            if f3 == 2:
                return ADDI(rd, 0, imm6, 2)
            if f3 == 3:
                if rd == 2:
                    imm = sx(((i >> 3) & 0x200) | ((i >> 2) & 0x10) | ((i << 1) & 0x40) |
                             ((i << 4) & 0x180) | ((i << 3) & 0x20), 10)
                    return ADDI(2, 2, imm, 2) if imm else illegal(i)
                imm = sx(((i << 5) & 0x20000) | ((i << 10) & 0x1F000), 18)
                return LUI(rd, imm, 2) if imm else illegal(i)
            if f3 == 4:
                rdp = 8 + ((i >> 7) & 7)
                f2 = (i >> 10) & 3
                shamt = ((i >> 7) & 0x20) | ((i >> 2) & 0x1F)
                if f2 == 0:
                    return SRLI(rdp, rdp, shamt, 2)
                if f2 == 1:
                    return SRAI(rdp, rdp, shamt, 2)
                if f2 == 2:
                    return ANDI(rdp, rdp, imm6, 2)
                rs2p = 8 + ((i >> 2) & 7)
                f = (i >> 5) & 3
                if not (i >> 12) & 1:
                    return (SUB, XOR, OR, AND)[f](rdp, rdp, rs2p, 2)
                if f == 0:
                    return SUBW(rdp, rdp, rs2p, 2)
                if f == 1:
                    return ADDW(rdp, rdp, rs2p, 2)
                return illegal(i)
            if f3 == 5:
                imm = sx(((i >> 1) & 0x800) | ((i >> 7) & 0x10) | ((i >> 1) & 0x300) |
                         ((i << 2) & 0x400) | ((i >> 1) & 0x40) | ((i << 1) & 0x80) |
                         ((i >> 2) & 0xE) | ((i << 3) & 0x20), 12)
                return JAL(0, imm, 2)
            rs1p = 8 + ((i >> 7) & 7)
            imm = sx(((i >> 4) & 0x100) | ((i >> 7) & 0x18) | ((i << 1) & 0xC0) |
                     ((i >> 2) & 0x6) | ((i << 3) & 0x20), 9)
            if f3 == 6:
                return BEQ(rs1p, 0, imm, 2)
            return BNE(rs1p, 0, imm, 2)

        if op == 2:
            rd = (i >> 7) & 31
            rs2 = (i >> 2) & 31
            imm_ldsp = ((i >> 7) & 0x20) | ((i >> 2) & 0x18) | ((i << 4) & 0x1C0)
            imm_sdsp = ((i >> 7) & 0x38) | ((i >> 1) & 0x1C0)
            if f3 == 0:
                return SLLI(rd, rd, ((i >> 7) & 0x20) | ((i >> 2) & 0x1F), 2)
            if f3 == 1:
                return FLOAD(rd, 2, imm_ldsp, 8, 2, i)
            if f3 == 2:
                imm = ((i >> 7) & 0x20) | ((i >> 2) & 0x1C) | ((i << 4) & 0xC0)
                return LW(rd, 2, imm, 2) if rd else illegal(i)
            if f3 == 3:
                return LD(rd, 2, imm_ldsp, 2) if rd else illegal(i)
            if f3 == 4:
                if not (i >> 12) & 1:
                    if rs2 == 0:
                        return JALR(0, rd, 0, 2) if rd else illegal(i)
                    return ADDI(rd, rs2, 0, 2)              # c.mv
                if rs2 == 0:
                    if rd == 0:
                        return EBREAK(2)
                    return JALR(1, rd, 0, 2)
                return ADD(rd, rd, rs2, 2)
            if f3 == 5:
                return FSTORE(2, rs2, imm_sdsp, 8, 2, i)
            if f3 == 6:
                imm = ((i >> 7) & 0x3C) | ((i >> 1) & 0xC0)
                return SW(2, rs2, imm, 2)
            return SD(2, rs2, imm_sdsp, 2)
        return illegal(i)

    cache = {}

    def decode(insn):
        h = cache.get(insn)
        if h is None:
            if (insn & 3) == 3:
                h = decode32(insn)
            else:
                h = decode16(insn)
            cache[insn] = h
        return h

    return decode
