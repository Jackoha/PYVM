/*
 * pyvm native core: the RV64 hart's hot path in C.
 *
 * pyvm is a Python program; this file is optional.  pyvm/native/__init__.py
 * compiles it with the host's C compiler the first time it is needed and
 * loads it with ctypes.  Without a compiler pyvm runs as before.
 *
 * The core executes RV64IMAC (+ FP loads, stores and moves) on guest RAM,
 * with its own Sv39 page walker and TLB.  Anything else -- CSRs, ecall,
 * xRET, WFI, fences of the TLB, LR/SC, FP arithmetic, MMIO, misaligned
 * accesses that cross a page -- is handed back: run() stops *before* that
 * instruction with status EXIT_PYTHON and the Python implementation runs it.
 * So the core only ever implements the simple, common instructions, and the
 * rest keeps its one, already tested implementation.
 *
 * Registers live in arrays shared with Python (ctypes), so nothing is copied
 * on the way in or out.  pc, privilege, mstatus and satp are passed in the
 * Hart struct; the core never changes the last three.
 */
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <fenv.h>

typedef uint64_t u64;
typedef int64_t i64;
typedef uint32_t u32;
typedef int32_t i32;
typedef unsigned __int128 u128;
typedef __int128 i128;

#define RAM_BASE 0x80000000ULL
#define TLB_SIZE 4096

enum { EXIT_BUDGET = 0, EXIT_TRAP = 1, EXIT_PYTHON = 2, EXIT_STOP = 3 };

/* TLB entry permission bits (from the leaf PTE) */
#define P_R 0x02
#define P_W 0x04
#define P_X 0x08
#define P_U 0x10
#define P_D 0x80

typedef struct {
    u64 pc;
    u64 mstatus;
    u64 satp;
    u64 priv;
    u64 status;          /* out: EXIT_* */
    u64 cause, tval;     /* out: for EXIT_TRAP */
    u64 count;           /* out: instructions retired */
    uint8_t *ram;
    u64 ram_size;
    u64 *x;              /* 33 entries; x[0] reads 0 */
    u64 *f;              /* 32 entries */
    /* in: what the CSRs handled here need */
    u64 mtime, mcounteren, scounteren, mip, mie, mideleg;
    u64 reservation;     /* LR/SC; ~0 = none (Python clears it on traps) */
    u64 instret;
    /* in/out: supervisor trap state, so syscalls and page faults the kernel
       handles never leave C */
    u64 medeleg, stvec, sepc, scause, stval, sscratch;
    u64 fflags, frm;     /* in/out */
    struct {
        u64 tag;             /* vpn + 1, 0 = empty */
        u64 off;             /* RAM offset of the page */
        u64 perm;            /* leaf PTE's R/W/X/U/D bits */
    } tlb[TLB_SIZE];
} Hart;

#define MSTATUS_MPRV (1ULL << 17)
#define MSTATUS_SUM (1ULL << 18)
#define MSTATUS_MXR (1ULL << 19)
#define MSTATUS_FS (3ULL << 13)
#define MSTATUS_SIE (1ULL << 1)
#define MSTATUS_MIE (1ULL << 3)
#define SSTATUS_WMASK ((1ULL << 1) | (1ULL << 5) | (1ULL << 8) | MSTATUS_FS | MSTATUS_SUM | MSTATUS_MXR)
#define SSTATUS_RMASK (SSTATUS_WMASK | (3ULL << 32))

#define MSTATUS_SPIE (1ULL << 5)
#define MSTATUS_SPP (1ULL << 8)
#define MSTATUS_TSR (1ULL << 22)

/* Take an exception into S-mode if it is delegated there (CPU.trap). */
static int s_trap(Hart *h, u64 cause, u64 tval, u64 pc) {
    u64 priv = h->priv;
    if (priv > 1 || !((h->medeleg >> cause) & 1)) return 0;
    h->scause = cause;
    h->sepc = pc;
    h->stval = tval;
    u64 st = h->mstatus;
    h->mstatus = (st & ~(MSTATUS_SPIE | MSTATUS_SPP | MSTATUS_SIE)) |
                 ((st & MSTATUS_SIE) << 4) | (priv << 8);
    h->priv = 1;
    h->reservation = ~0ULL;
    return 1;
}

/* CPU.check_irq(take=False) in cpu.py */
static int irq_ready(Hart *h, u64 mstatus) {
    u64 p = h->mip & h->mie;
    if (!p) return 0;
    u64 pend = p & ~h->mideleg;
    if (pend && (h->priv < 3 || (mstatus & MSTATUS_MIE))) return 1;
    pend = p & h->mideleg;
    return pend && (h->priv == 0 || (h->priv == 1 && (mstatus & MSTATUS_SIE)));
}

void flush(Hart *h) { memset(h->tlb, 0, sizeof h->tlb); }

/* translation result codes */
#define T_OK 0
#define T_FAULT 1        /* page fault: cause depends on access */
#define T_ACCESS 2       /* access fault (PTE outside RAM) */
#define T_IO 3           /* translates fine, but not to RAM: Python does it */

static inline int perm_ok(u64 perm, int acc, int priv, u64 mstatus) {
    if (acc == 0) {
        if (!(perm & P_X)) return 0;
    } else if (acc == 1) {
        if (!((perm & P_R) || ((mstatus & MSTATUS_MXR) && (perm & P_X)))) return 0;
    } else {
        if (!(perm & P_W) || !(perm & P_D)) return 0;
    }
    if (perm & P_U) {
        if (priv == 1 && (acc == 0 || !(mstatus & MSTATUS_SUM))) return 0;
    } else if (priv == 0) {
        return 0;
    }
    return 1;
}

/* Sv39 walk, same rules (and A/D updates) as CPU.translate in cpu.py. */
static int walk(Hart *h, u64 va, int acc, int priv, u64 *off_out) {
    u64 top = va >> 38;
    if (top && top != 0x3FFFFFF) return T_FAULT;
    u64 pt = (h->satp & ((1ULL << 44) - 1)) << 12;
    int level = 2;
    u64 pte, pte_addr;
    for (;;) {
        pte_addr = pt + (((va >> (12 + 9 * level)) & 511) << 3) - RAM_BASE;
        if (pte_addr >= h->ram_size) return T_ACCESS;
        memcpy(&pte, h->ram + pte_addr, 8);
        if (!(pte & 1) || (pte & 6) == 4) return T_FAULT;
        if (pte & 0xA) break;
        if (--level < 0) return T_FAULT;
        pt = ((pte >> 10) & ((1ULL << 44) - 1)) << 12;
    }
    /* permission check on the leaf (D is handled by the update below) */
    u64 perm = pte & (P_R | P_W | P_X | P_U);
    if (!perm_ok(perm | P_D, acc, priv, h->mstatus)) return T_FAULT;
    u64 ppn = (pte >> 10) & ((1ULL << 44) - 1);
    if (level && (ppn & ((1ULL << (9 * level)) - 1))) return T_FAULT;
    if (!(pte & 0x40) || (acc == 2 && !(pte & 0x80))) {
        pte |= 0x40 | (acc == 2 ? 0x80 : 0);
        memcpy(h->ram + pte_addr, &pte, 8);
    }
    u64 pa = (ppn << 12) | (va & ((1ULL << (12 + 9 * level)) - 1));
    u64 off = (pa & ~4095ULL) - RAM_BASE;
    if (pa < RAM_BASE || off >= h->ram_size) return T_IO;
    u64 vpn = va >> 12;
    int i = vpn & (TLB_SIZE - 1);
    h->tlb[i].tag = vpn + 1;
    h->tlb[i].off = off;
    h->tlb[i].perm = perm | (pte & P_D);
    *off_out = off + (va & 4095);
    return T_OK;
}

/* What translation looks like in the current mode, recomputed whenever the
   privilege or mstatus changes: per access type, whether it is untranslated,
   the privilege it checks against, and which TLB permission bytes allow it. */
typedef struct {
    int bare[3];
    int priv[3];
    const uint8_t *ok[3];
} Mode;

/* ok_tab[acc][priv][SUM][MXR][perm]: computed once */
static uint8_t ok_tab[3][4][2][2][256];
static int ok_ready;

static void ok_init(void) {
    for (int acc = 0; acc < 3; acc++)
        for (int priv = 0; priv < 4; priv++)
            for (int sum = 0; sum < 2; sum++)
                for (int mxr = 0; mxr < 2; mxr++) {
                    u64 st = (sum ? MSTATUS_SUM : 0) | (mxr ? MSTATUS_MXR : 0);
                    for (int p = 0; p < 256; p++)
                        ok_tab[acc][priv][sum][mxr][p] = (uint8_t)perm_ok((u64)p, acc, priv, st);
                }
    ok_ready = 1;
}

static void mode_calc(Hart *h, Mode *m) {
    int sum = (h->mstatus & MSTATUS_SUM) != 0, mxr = (h->mstatus & MSTATUS_MXR) != 0;
    for (int acc = 0; acc < 3; acc++) {
        int priv = (int)h->priv;
        if (acc && priv == 3 && (h->mstatus & MSTATUS_MPRV))
            priv = (int)((h->mstatus >> 11) & 3);
        m->priv[acc] = priv;
        m->bare[acc] = priv == 3 || !(h->satp >> 60);
        m->ok[acc] = ok_tab[acc][priv][sum][mxr];
    }
}

/* virtual address -> RAM offset.  acc: 0 fetch, 1 load, 2 store */
static inline int xl(Hart *h, const Mode *md, u64 va, int acc, u64 *off) {
    if (md->bare[acc]) {
        u64 o = va - RAM_BASE;
        if (va < RAM_BASE || o >= h->ram_size) return T_IO;
        *off = o;
        return T_OK;
    }
    u64 vpn = va >> 12;
    int i = vpn & (TLB_SIZE - 1);
    if (h->tlb[i].tag == vpn + 1 && md->ok[acc][h->tlb[i].perm]) {
        *off = h->tlb[i].off + (va & 4095);
        return T_OK;
    }
    return walk(h, va, acc, md->priv[acc], off);
}

static inline i64 sx(u64 v, int bits) {
    return (i64)(v << (64 - bits)) >> (64 - bits);
}

/* Expand a compressed instruction to its 32-bit equivalent (0 = not handled). */
static u32 expand(u32 i) {
    u32 op = i & 3, f3 = i >> 13;
    u32 rdp = 8 + ((i >> 2) & 7), rs1p = 8 + ((i >> 7) & 7);
    u32 rd = (i >> 7) & 31, rs2 = (i >> 2) & 31;
#define ITYPE(imm, rs1, f3_, rd_, opc) ((((u32)(imm) & 0xFFF) << 20) | ((rs1) << 15) | ((f3_) << 12) | ((rd_) << 7) | (opc))
#define STYPE(imm, rs2_, rs1, f3_, opc) (((((u32)(imm) >> 5) & 0x7F) << 25) | ((rs2_) << 20) | ((rs1) << 15) | ((f3_) << 12) | (((u32)(imm) & 31) << 7) | (opc))
#define RTYPE(f7, rs2_, rs1, f3_, rd_, opc) (((f7) << 25) | ((rs2_) << 20) | ((rs1) << 15) | ((f3_) << 12) | ((rd_) << 7) | (opc))
    if (op == 0) {
        u32 imm_w = ((i >> 7) & 0x38) | ((i >> 4) & 0x4) | ((i << 1) & 0x40);
        u32 imm_d = ((i >> 7) & 0x38) | ((i << 1) & 0xC0);
        switch (f3) {
        case 0: {
            u32 imm = ((i >> 7) & 0x30) | ((i >> 1) & 0x3C0) | ((i >> 4) & 0x4) | ((i >> 2) & 0x8);
            return imm ? ITYPE(imm, 2, 0, rdp, 0x13) : 0;
        }
        case 1: return ITYPE(imm_d, rs1p, 3, rdp, 0x07);          /* c.fld */
        case 2: return ITYPE(imm_w, rs1p, 2, rdp, 0x03);          /* c.lw */
        case 3: return ITYPE(imm_d, rs1p, 3, rdp, 0x03);          /* c.ld */
        case 5: return STYPE(imm_d, rdp, rs1p, 3, 0x27);          /* c.fsd */
        case 6: return STYPE(imm_w, rdp, rs1p, 2, 0x23);          /* c.sw */
        case 7: return STYPE(imm_d, rdp, rs1p, 3, 0x23);          /* c.sd */
        }
        return 0;
    }
    if (op == 1) {
        i32 imm6 = (i32)sx(((i >> 7) & 0x20) | ((i >> 2) & 0x1F), 6);
        switch (f3) {
        case 0: return ITYPE(imm6, rd, 0, rd, 0x13);                       /* c.addi */
        case 1: return rd ? ITYPE(imm6, rd, 0, rd, 0x1B) : 0;              /* c.addiw */
        case 2: return ITYPE(imm6, 0, 0, rd, 0x13);                        /* c.li */
        case 3:
            if (rd == 2) {
                i32 imm = (i32)sx(((i >> 3) & 0x200) | ((i >> 2) & 0x10) | ((i << 1) & 0x40) |
                                  ((i << 4) & 0x180) | ((i << 3) & 0x20), 10);
                return imm ? ITYPE(imm, 2, 0, 2, 0x13) : 0;
            } else {
                i32 imm = (i32)sx(((i << 5) & 0x20000) | ((i << 10) & 0x1F000), 18);
                return imm ? (((u32)imm & 0xFFFFF000) | (rd << 7) | 0x37) : 0;
            }
        case 4: {
            u32 f2 = (i >> 10) & 3;
            u32 sh = ((i >> 7) & 0x20) | ((i >> 2) & 0x1F);
            if (f2 == 0) return ITYPE(sh, rs1p, 5, rs1p, 0x13);
            if (f2 == 1) return ITYPE(sh | 0x400, rs1p, 5, rs1p, 0x13);
            if (f2 == 2) return ITYPE(imm6, rs1p, 7, rs1p, 0x13);
            u32 r2 = 8 + ((i >> 2) & 7), f = (i >> 5) & 3;
            if (!((i >> 12) & 1)) {
                static const u32 f3s[4] = {0, 4, 6, 7};
                return RTYPE(f == 0 ? 0x20 : 0, r2, rs1p, f3s[f], rs1p, 0x33);
            }
            if (f == 0) return RTYPE(0x20, r2, rs1p, 0, rs1p, 0x3B);
            if (f == 1) return RTYPE(0, r2, rs1p, 0, rs1p, 0x3B);
            return 0;
        }
        case 5: {
            i32 imm = (i32)sx(((i >> 1) & 0x800) | ((i >> 7) & 0x10) | ((i >> 1) & 0x300) |
                              ((i << 2) & 0x400) | ((i >> 1) & 0x40) | ((i << 1) & 0x80) |
                              ((i >> 2) & 0xE) | ((i << 3) & 0x20), 12);
            u32 u = (u32)imm;
            return (((u >> 20) & 1) << 31) | (((u >> 1) & 0x3FF) << 21) | (((u >> 11) & 1) << 20) |
                   (((u >> 12) & 0xFF) << 12) | 0x6F;                      /* c.j */
        }
        default: {
            i32 imm = (i32)sx(((i >> 4) & 0x100) | ((i >> 7) & 0x18) | ((i << 1) & 0xC0) |
                              ((i >> 2) & 0x6) | ((i << 3) & 0x20), 9);
            u32 u = (u32)imm;
            return (((u >> 12) & 1) << 31) | (((u >> 5) & 0x3F) << 25) | (rs1p << 15) |
                   ((f3 == 6 ? 0 : 1) << 12) | (((u >> 1) & 0xF) << 8) | (((u >> 11) & 1) << 7) | 0x63;
        }
        }
    }
    if (op == 2) {
        switch (f3) {
        case 0: return ITYPE(((i >> 7) & 0x20) | ((i >> 2) & 0x1F), rd, 1, rd, 0x13);   /* c.slli */
        case 1: return ITYPE(((i >> 7) & 0x20) | ((i >> 2) & 0x18) | ((i << 4) & 0x1C0), 2, 3, rd, 0x07);
        case 2: return rd ? ITYPE(((i >> 7) & 0x20) | ((i >> 2) & 0x1C) | ((i << 4) & 0xC0), 2, 2, rd, 0x03) : 0;
        case 3: return rd ? ITYPE(((i >> 7) & 0x20) | ((i >> 2) & 0x18) | ((i << 4) & 0x1C0), 2, 3, rd, 0x03) : 0;
        case 4:
            if (!((i >> 12) & 1)) {
                if (rs2 == 0) return rd ? ITYPE(0, rd, 0, 0, 0x67) : 0;      /* c.jr */
                return RTYPE(0, rs2, 0, 0, rd, 0x33);                         /* c.mv */
            }
            if (rs2 == 0) return rd ? ITYPE(0, rd, 0, 1, 0x67) : 0;          /* c.jalr; ebreak -> 0 */
            return RTYPE(0, rs2, rd, 0, rd, 0x33);                            /* c.add */
        case 5: return STYPE(((i >> 7) & 0x38) | ((i >> 1) & 0x1C0), rs2, 2, 3, 0x27);
        case 6: return STYPE(((i >> 7) & 0x3C) | ((i >> 1) & 0xC0), rs2, 2, 2, 0x23);
        case 7: return STYPE(((i >> 7) & 0x38) | ((i >> 1) & 0x1C0), rs2, 2, 3, 0x23);
        }
    }
    return 0;
}

#define SEXT32(v) ((u64)(i64)(i32)(u32)(v))

/* every compressed instruction, expanded once */
static u32 exp_tab[65536];
static int exp_ready;

static void exp_init(void) {
    for (u32 i = 0; i < 65536; i++)
        exp_tab[i] = (i & 3) == 3 ? 0 : expand(i);
    exp_ready = 1;
}

/* ------------------------------------------------------------------ F and D
 * Arithmetic runs on the host FPU with the guest's rounding mode, and the
 * host's exception flags become fflags; the RISC-V rules the host does not
 * follow (canonical NaNs, saturating conversions, min/max, NaN-boxing, the
 * flags of compares) are done by hand.  fpu.py is the reference: tests check
 * this against it.  RMM has no host equivalent, so it goes to Python.
 */
#define NX 1
#define UF 2
#define OF 4
#define DZ 8
#define NV 16
#define QNAN64 0x7FF8000000000000ULL
#define QNAN32 0x7FC00000U
#define BOX 0xFFFFFFFF00000000ULL

static const int host_rm[4] = {FE_TONEAREST, FE_TOWARDZERO, FE_DOWNWARD, FE_UPWARD};

static inline double d_of(u64 v) { double d; memcpy(&d, &v, 8); return d; }
static inline u64 bits_d(double d) { u64 v; memcpy(&v, &d, 8); return v; }
static inline u32 unbox(u64 v) { return (v >> 32) == 0xFFFFFFFF ? (u32)v : QNAN32; }
static inline float f_of(u32 w) { float f; memcpy(&f, &w, 4); return f; }
static inline u32 bits_f(float f) { u32 w; memcpy(&w, &f, 4); return w; }
static inline int nan64(u64 b) { return ((b >> 52) & 0x7FF) == 0x7FF && (b & ((1ULL << 52) - 1)); }
static inline int snan64(u64 b) { return nan64(b) && !((b >> 51) & 1); }
static inline int nan32(u32 b) { return ((b >> 23) & 0xFF) == 0xFF && (b & 0x7FFFFF); }
static inline int snan32(u32 b) { return nan32(b) && !((b >> 22) & 1); }

static u32 host_flags(void) {
    int e = fetestexcept(FE_ALL_EXCEPT);
    return (e & FE_INEXACT ? NX : 0) | (e & FE_UNDERFLOW ? UF : 0) | (e & FE_OVERFLOW ? OF : 0) |
           (e & FE_DIVBYZERO ? DZ : 0) | (e & FE_INVALID ? NV : 0);
}

/* sortable key: -0 below +0 unless zero_equal (fpu._key) */
static inline i64 key64(u64 b, int zero_equal) {
    if (zero_equal && !(b << 1)) return 0;
    return (b >> 63) ? -(i64)(b & ~(1ULL << 63)) - 1 + (zero_equal ? 1 : 0) : (i64)b;
}
static inline i64 key32(u32 b, int zero_equal) {
    if (zero_equal && !(b << 1)) return 0;
    return (b >> 31) ? -(i64)(b & 0x7FFFFFFF) - 1 + (zero_equal ? 1 : 0) : (i64)b;
}

static u64 classify(u64 b, int dbl) {
    int s, expmax, expz, fracz, quiet;
    if (dbl) {
        s = b >> 63; u64 e = (b >> 52) & 0x7FF; u64 fr = b & ((1ULL << 52) - 1);
        expmax = e == 0x7FF; expz = e == 0; fracz = fr == 0; quiet = (b >> 51) & 1;
    } else {
        u32 w = (u32)b;
        s = w >> 31; u32 e = (w >> 23) & 0xFF; u32 fr = w & 0x7FFFFF;
        expmax = e == 0xFF; expz = e == 0; fracz = fr == 0; quiet = (w >> 22) & 1;
    }
    if (expmax) {
        if (fracz) return s ? 1 : 1 << 7;
        return quiet ? 1 << 9 : 1 << 8;
    }
    if (expz) {
        if (fracz) return s ? 1 << 3 : 1 << 4;
        return s ? 1 << 2 : 1 << 5;
    }
    return s ? 1 << 1 : 1 << 6;
}

/* float -> integer, saturating (fpu.to_int); v is exact in double */
static u64 to_int(double v, int isnan_, int width, int sign, int rm, u32 *fl) {
    double lo = sign ? -ldexp(1, width - 1) : 0;
    double hi_lim = sign ? ldexp(1, width - 1) : ldexp(1, width);     /* exclusive */
    u64 hi = sign ? ((1ULL << (width - 1)) - 1) : (width == 64 ? ~0ULL : ((1ULL << width) - 1));
    u64 lov = sign ? (u64)(-(i64)1 << (width - 1)) : 0;
    if (isnan_) { *fl |= NV; return hi; }
    if (isinf(v)) { *fl |= NV; return v < 0 ? lov : hi; }
    fesetround(host_rm[rm]);
    volatile double vv = v;
    double r = nearbyint(vv);
    fesetround(FE_TONEAREST);
    if (r < lo) { *fl |= NV; return lov; }
    if (r >= hi_lim) { *fl |= NV; return hi; }
    if (r != v) *fl |= NX;
    if (sign) return (u64)(i64)r;
    return (u64)r;
}

/* Execute an F/D instruction.  Returns 0 if Python has to do it. */
static int fp_exec(Hart *h, u32 insn, u64 *x, u64 *f) {
    u32 opc = insn & 0x7F, rd = (insn >> 7) & 31, rm = (insn >> 12) & 7;
    u32 rs1 = (insn >> 15) & 31, rs2 = (insn >> 20) & 31;
    if (!(h->mstatus & MSTATUS_FS)) return 0;             /* illegal: Python traps */
    int dbl = (insn >> 25) & 3;
    if (dbl > 1) return 0;
    u32 f5 = insn >> 27;
    u32 fl = 0;
    int rounds = opc != 0x53 || f5 <= 3 || f5 == 0x0B || f5 == 0x08 || f5 == 0x18 || f5 == 0x1A;
    int r = rm;
    if (rounds) {
        if (rm == 7) r = (int)h->frm;
        if (r > 3) return 0;                               /* RMM, or illegal */
        if (rm == 5 || rm == 6) return 0;
    }
    u64 a = f[rs1], b = f[rs2], res;
    int to_x = 0;

    if (opc != 0x53) {                                     /* fused multiply-add */
        u64 c = f[insn >> 27];
        int neg_prod = opc == 0x4B || opc == 0x4F, neg_c = opc == 0x47 || opc == 0x4F;
        feclearexcept(FE_ALL_EXCEPT);
        fesetround(host_rm[r]);
        if (dbl) {
            volatile double va = d_of(a), vb = d_of(b), vc = d_of(c);
            double pa = neg_prod ? -va : va, pc = neg_c ? -vc : vc;
            volatile double vr = fma(pa, vb, pc);
            res = bits_d(vr);
            if (nan64(res)) res = QNAN64;
        } else {
            volatile float va = f_of(unbox(a)), vb = f_of(unbox(b)), vc = f_of(unbox(c));
            float pa = neg_prod ? -va : va, pc = neg_c ? -vc : vc;
            volatile float vr = fmaf(pa, vb, pc);
            u32 w = bits_f(vr);
            res = BOX | (nan32(w) ? QNAN32 : w);
        }
        fl = host_flags();
        fesetround(FE_TONEAREST);
        goto done;
    }

    switch (f5) {
    case 0x00: case 0x01: case 0x02: case 0x03: case 0x0B:
        if (f5 == 0x0B && rs2) return 0;
        feclearexcept(FE_ALL_EXCEPT);
        fesetround(host_rm[r]);
        if (dbl) {
            volatile double va = d_of(a), vb = d_of(b), vr;
            switch (f5) {
            case 0: vr = va + vb; break;
            case 1: vr = va - vb; break;
            case 2: vr = va * vb; break;
            case 3: vr = va / vb; break;
            default: vr = sqrt(va); break;
            }
            res = bits_d(vr);
            if (nan64(res)) res = QNAN64;
        } else {
            volatile float va = f_of(unbox(a)), vb = f_of(unbox(b)), vr;
            switch (f5) {
            case 0: vr = va + vb; break;
            case 1: vr = va - vb; break;
            case 2: vr = va * vb; break;
            case 3: vr = va / vb; break;
            default: vr = sqrtf(va); break;
            }
            u32 w = bits_f(vr);
            res = BOX | (nan32(w) ? QNAN32 : w);
        }
        fl = host_flags();
        fesetround(FE_TONEAREST);
        break;
    case 0x04:                                             /* fsgnj, fsgnjn, fsgnjx */
        if (rm > 2) return 0;
        if (dbl) {
            u64 sb = 1ULL << 63;
            u64 sg = rm == 0 ? (b & sb) : rm == 1 ? (~b & sb) : ((a ^ b) & sb);
            res = (a & ~sb) | sg;
        } else {
            u32 ua = unbox(a), ub = unbox(b), sb = 1U << 31;
            u32 sg = rm == 0 ? (ub & sb) : rm == 1 ? (~ub & sb) : ((ua ^ ub) & sb);
            res = BOX | ((ua & ~sb) | sg);
        }
        break;
    case 0x05: {                                           /* fmin, fmax */
        if (rm > 1) return 0;
        int an, bn;
        i64 ka, kb;
        u64 qa = a, qb = b, qn;
        if (dbl) {
            an = nan64(a); bn = nan64(b);
            if (snan64(a) || snan64(b)) fl |= NV;
            ka = key64(a, 0); kb = key64(b, 0); qn = QNAN64;
        } else {
            u32 ua = unbox(a), ub = unbox(b);
            an = nan32(ua); bn = nan32(ub);
            if (snan32(ua) || snan32(ub)) fl |= NV;
            ka = key32(ua, 0); kb = key32(ub, 0); qn = BOX | QNAN32;
            qa = BOX | ua; qb = BOX | ub;
        }
        if (an && bn) res = qn;
        else if (an) res = qb;
        else if (bn) res = qa;
        else if (rm == 1) res = ka >= kb ? qa : qb;
        else res = ka <= kb ? qa : qb;
        break;
    }
    case 0x08:                                             /* fcvt.s.d / fcvt.d.s */
        if (dbl == 0 && rs2 == 1) {
            if (nan64(a)) { if (snan64(a)) fl |= NV; res = BOX | QNAN32; break; }
            feclearexcept(FE_ALL_EXCEPT);
            fesetround(host_rm[r]);
            volatile double va = d_of(a);
            volatile float vr = (float)va;
            fl = host_flags();
            fesetround(FE_TONEAREST);
            res = BOX | bits_f(vr);
        } else if (dbl == 1 && rs2 == 0) {
            u32 ua = unbox(a);
            if (nan32(ua)) { if (snan32(ua)) fl |= NV; res = QNAN64; break; }
            res = bits_d((double)f_of(ua));
        } else {
            return 0;
        }
        break;
    case 0x14: {                                           /* fle, flt, feq */
        if (rm > 2) return 0;
        int an, bn, sn;
        i64 ka, kb;
        if (dbl) {
            an = nan64(a); bn = nan64(b); sn = snan64(a) || snan64(b);
            ka = key64(a, 1); kb = key64(b, 1);
        } else {
            u32 ua = unbox(a), ub = unbox(b);
            an = nan32(ua); bn = nan32(ub); sn = snan32(ua) || snan32(ub);
            ka = key32(ua, 1); kb = key32(ub, 1);
        }
        if (an || bn) {
            res = 0;
            if (rm != 2 || sn) fl |= NV;
        } else {
            res = rm == 0 ? ka <= kb : rm == 1 ? ka < kb : ka == kb;
        }
        to_x = 1;
        break;
    }
    case 0x18: {                                           /* fcvt.{w,wu,l,lu}.{s,d} */
        if (rs2 > 3) return 0;
        int width = rs2 < 2 ? 32 : 64, sign = !(rs2 & 1);
        double v;
        int isn;
        if (dbl) { isn = nan64(a); v = isn ? 0 : d_of(a); }
        else { u32 ua = unbox(a); isn = nan32(ua); v = isn ? 0 : (double)f_of(ua); }
        res = to_int(v, isn, width, sign, r, &fl);
        if (width == 32) res = SEXT32(res);
        to_x = 1;
        break;
    }
    case 0x1A: {                                           /* fcvt.{s,d}.{w,wu,l,lu} */
        if (rs2 > 3) return 0;
        u64 xv = rs1 ? x[rs1] : 0;
        feclearexcept(FE_ALL_EXCEPT);
        fesetround(host_rm[r]);
        if (dbl) {
            volatile double vr;
            switch (rs2) {
            case 0: vr = (double)(i32)xv; break;
            case 1: vr = (double)(u32)xv; break;
            case 2: { volatile i64 t = (i64)xv; vr = (double)t; break; }
            default: { volatile u64 t = xv; vr = (double)t; break; }
            }
            res = bits_d(vr);
        } else {
            volatile float vr;
            switch (rs2) {
            case 0: { volatile i32 t = (i32)xv; vr = (float)t; break; }
            case 1: { volatile u32 t = (u32)xv; vr = (float)t; break; }
            case 2: { volatile i64 t = (i64)xv; vr = (float)t; break; }
            default: { volatile u64 t = xv; vr = (float)t; break; }
            }
            res = BOX | bits_f(vr);
        }
        fl = host_flags();
        fesetround(FE_TONEAREST);
        break;
    }
    case 0x1C:
        if (rs2 || rm > 1) return 0;
        if (rm == 0) res = dbl ? a : SEXT32(a);            /* fmv.x.{w,d}: raw bits */
        else res = classify(dbl ? a : unbox(a), dbl);
        to_x = 1;
        break;
    case 0x1E:                                             /* fmv.{w,d}.x */
        if (rs2 || rm) return 0;
        {
            u64 xv = rs1 ? x[rs1] : 0;
            res = dbl ? xv : (BOX | (u32)xv);
        }
        break;
    default:
        return 0;
    }
done:
    if (to_x) {
        if (rd) x[rd] = res;
    } else {
        f[rd] = res;
    }
    h->fflags |= fl;
    h->mstatus |= MSTATUS_FS;
    return 1;
}

/* ------------------------------------------------------------------ decoded-instruction cache
 * The common instructions are decoded once into a DI: an operation number,
 * register numbers (a write to x0 goes to the junk slot x[32]) and the
 * immediate ready to use.  Entries are found by RAM offset and are only used
 * while the bytes there are still the ones decoded, so code that changes is
 * simply decoded again.  Anything not listed runs through the full decoder
 * in run() (op O_SLOW).
 */
enum {
    O_SLOW, O_ADDI, O_SLTI, O_SLTIU, O_XORI, O_ORI, O_ANDI, O_SLLI, O_SRLI, O_SRAI,
    O_ADDIW, O_SLLIW, O_SRLIW, O_SRAIW,
    O_ADD, O_SUB, O_SLL, O_SLT, O_SLTU, O_XOR, O_SRL, O_SRA, O_OR, O_AND,
    O_MUL, O_MULH, O_MULHSU, O_MULHU, O_DIV, O_DIVU, O_REM, O_REMU,
    O_ADDW, O_SUBW, O_SLLW, O_SRLW, O_SRAW, O_MULW, O_DIVW, O_DIVUW, O_REMW, O_REMUW,
    O_LUI, O_AUIPC, O_JAL, O_JALR, O_BEQ, O_BNE, O_BLT, O_BGE, O_BLTU, O_BGEU,
    O_LB, O_LH, O_LW, O_LD, O_LBU, O_LHU, O_LWU, O_SB, O_SH, O_SW, O_SD, O_NOP,
};

typedef struct {
    u64 key;             /* RAM offset + 1 */
    u32 raw;             /* the bytes decoded (16 or 32 bits) */
    u32 insn;            /* as a 32-bit instruction (compressed ones expanded) */
    uint8_t op, rd, rs1, rs2, il;
    i64 imm;
} DI;

#define DIC_SIZE (1 << 16)
static DI dic[DIC_SIZE];

static void decode(DI *d, u64 off, u32 raw, u32 insn, int il) {
    d->key = off + 1;
    d->raw = raw;
    d->insn = insn;
    d->il = (uint8_t)il;
    d->op = O_SLOW;
    if (!insn) return;
    u32 opc = insn & 0x7F, rd = (insn >> 7) & 31, f3 = (insn >> 12) & 7;
    u32 rs1 = (insn >> 15) & 31, rs2 = (insn >> 20) & 31, f7 = insn >> 25;
    d->rd = rd ? (uint8_t)rd : 32;
    d->rs1 = (uint8_t)rs1;
    d->rs2 = (uint8_t)rs2;
    i64 immi = (i64)(i32)insn >> 20;
    switch (opc) {
    case 0x13:
        d->imm = immi;
        switch (f3) {
        case 0: d->op = O_ADDI; break;
        case 2: d->op = O_SLTI; break;
        case 3: d->op = O_SLTIU; break;
        case 4: d->op = O_XORI; break;
        case 6: d->op = O_ORI; break;
        case 7: d->op = O_ANDI; break;
        case 1: if (!(insn >> 26)) { d->op = O_SLLI; d->imm = (insn >> 20) & 63; } break;
        case 5:
            if ((insn >> 26) == 0) { d->op = O_SRLI; d->imm = (insn >> 20) & 63; }
            else if ((insn >> 26) == 0x10) { d->op = O_SRAI; d->imm = (insn >> 20) & 63; }
            break;
        }
        if (!rd && d->op != O_SLOW) d->op = O_NOP;
        break;
    case 0x1B:
        d->imm = immi;
        if (f3 == 0) d->op = O_ADDIW;
        else if (f3 == 1 && f7 == 0) { d->op = O_SLLIW; d->imm = rs2; }
        else if (f3 == 5 && f7 == 0) { d->op = O_SRLIW; d->imm = rs2; }
        else if (f3 == 5 && f7 == 0x20) { d->op = O_SRAIW; d->imm = rs2; }
        break;
    case 0x33:
        if (f7 == 0) {
            static const uint8_t t[8] = {O_ADD, O_SLL, O_SLT, O_SLTU, O_XOR, O_SRL, O_OR, O_AND};
            d->op = t[f3];
        } else if (f7 == 1) {
            static const uint8_t t[8] = {O_MUL, O_MULH, O_MULHSU, O_MULHU, O_DIV, O_DIVU, O_REM, O_REMU};
            d->op = t[f3];
        } else if (f7 == 0x20 && f3 == 0) d->op = O_SUB;
        else if (f7 == 0x20 && f3 == 5) d->op = O_SRA;
        break;
    case 0x3B:
        if (f7 == 0 && f3 == 0) d->op = O_ADDW;
        else if (f7 == 0x20 && f3 == 0) d->op = O_SUBW;
        else if (f7 == 0 && f3 == 1) d->op = O_SLLW;
        else if (f7 == 0 && f3 == 5) d->op = O_SRLW;
        else if (f7 == 0x20 && f3 == 5) d->op = O_SRAW;
        else if (f7 == 1) {
            static const uint8_t t[8] = {O_MULW, O_SLOW, O_SLOW, O_SLOW, O_DIVW, O_DIVUW, O_REMW, O_REMUW};
            d->op = t[f3];
        }
        break;
    case 0x37: d->op = O_LUI; d->imm = (i64)(i32)(insn & 0xFFFFF000); break;
    case 0x17: d->op = O_AUIPC; d->imm = (i64)(i32)(insn & 0xFFFFF000); break;
    case 0x6F:
        d->op = O_JAL;
        d->imm = sx((((insn >> 31) & 1) << 20) | (((insn >> 12) & 0xFF) << 12) |
                    (((insn >> 20) & 1) << 11) | (((insn >> 21) & 0x3FF) << 1), 21);
        break;
    case 0x67: if (!f3) { d->op = O_JALR; d->imm = immi; } break;
    case 0x63: {
        static const uint8_t t[8] = {O_BEQ, O_BNE, O_SLOW, O_SLOW, O_BLT, O_BGE, O_BLTU, O_BGEU};
        d->op = t[f3];
        d->imm = sx((((insn >> 31) & 1) << 12) | (((insn >> 7) & 1) << 11) |
                    (((insn >> 25) & 0x3F) << 5) | (((insn >> 8) & 0xF) << 1), 13);
        break;
    }
    case 0x03: {
        static const uint8_t t[8] = {O_LB, O_LH, O_LW, O_LD, O_LBU, O_LHU, O_LWU, O_SLOW};
        d->op = t[f3];
        d->imm = immi;
        break;
    }
    case 0x23:
        if (f3 <= 3) {
            d->op = O_SB + f3;
            d->imm = sx(((insn >> 25) << 5) | rd, 12);
        }
        break;
    case 0x0F: if (f3 <= 1) d->op = O_NOP; break;
    }
}

/* Run up to n instructions.  Sets h->count, h->status (and cause/tval). */
void run(Hart *h, u64 n) {
    u64 pc = h->pc;
    u64 *x = h->x;
    u64 *f = h->f;
    uint8_t *ram = h->ram;
    u64 cnt = 0;
    int status = EXIT_BUDGET;
    u64 cause = 0, tval = 0;
    /* fetch cache: the page pc is in */
    u64 fvpn = ~0ULL, fbase = 0;
    Mode md;
    if (!exp_ready) exp_init();
    if (!ok_ready) ok_init();
    mode_calc(h, &md);
#define MODE_CHANGED() do { mode_calc(h, &md); fvpn = ~0ULL; } while (0)

/* raise an exception: S-mode ones are taken right here, the rest in Python */
#define TRAP(c, t) do { \
        if (s_trap(h, (c), (t), pc)) { \
            pc = h->stvec & ~3ULL; \
            MODE_CHANGED(); \
            if (irq_ready(h, h->mstatus)) { status = EXIT_STOP; goto out; } \
            goto next; /* (not `continue`: that would only leave this macro) */ \
        } \
        cause = (c); tval = (t); status = EXIT_TRAP; goto out; \
    } while (0)
#define PYTHON() do { status = EXIT_PYTHON; goto out; } while (0)
/* memory access: on anything but a plain RAM hit, leave it to Python */
#define ADDR(va, acc, size, off) do { \
        int r_ = xl(h, &md, (va), (acc), &(off)); \
        if (r_ == T_FAULT) TRAP((acc) == 1 ? 13 : 15, (va)); \
        if (r_ != T_OK || (((va) & 4095) + (size) > 4096)) PYTHON(); \
    } while (0)

    while (cnt < n) {
        u64 off;
        if ((pc >> 12) != fvpn) {
            int r = xl(h, &md, pc, 0, &off);
            if (r == T_FAULT) TRAP(12, pc);
            if (r == T_ACCESS) TRAP(1, pc);
            if (r == T_IO) PYTHON();
            fvpn = pc >> 12;
            fbase = off - (pc & 4095);
        }
        off = fbase + (pc & 4095);
        u32 insn;
        u32 lo = ram[off] | ((u32)ram[off + 1] << 8);
        int il;
        if ((pc & 4095) != 4094 || (lo & 3) != 3) {
            u32 raw = lo;
            if ((lo & 3) == 3) memcpy(&raw, ram + off, 4);
            DI *d = &dic[(off >> 1) & (DIC_SIZE - 1)];
            if (d->key != off + 1 || d->raw != raw)
                decode(d, off, raw, (lo & 3) == 3 ? raw : exp_tab[lo], (lo & 3) == 3 ? 4 : 2);
            u64 *X = x;
            u64 A = X[d->rs1], B = X[d->rs2], o;
            i64 I = d->imm;
            switch (d->op) {
#define W(val) do { X[d->rd] = (val); pc += d->il; cnt++; goto next; } while (0)
            case O_ADDI: W(A + I);
            case O_SLTI: W((i64)A < I);
            case O_SLTIU: W(A < (u64)I);
            case O_XORI: W(A ^ I);
            case O_ORI: W(A | I);
            case O_ANDI: W(A & I);
            case O_SLLI: W(A << I);
            case O_SRLI: W(A >> I);
            case O_SRAI: W((u64)((i64)A >> I));
            case O_ADDIW: W(SEXT32(A + I));
            case O_SLLIW: W(SEXT32((u32)A << I));
            case O_SRLIW: W(SEXT32((u32)A >> I));
            case O_SRAIW: W(SEXT32((i32)A >> I));
            case O_ADD: W(A + B);
            case O_SUB: W(A - B);
            case O_SLL: W(A << (B & 63));
            case O_SLT: W((i64)A < (i64)B);
            case O_SLTU: W(A < B);
            case O_XOR: W(A ^ B);
            case O_SRL: W(A >> (B & 63));
            case O_SRA: W((u64)((i64)A >> (B & 63)));
            case O_OR: W(A | B);
            case O_AND: W(A & B);
            case O_MUL: W(A * B);
            case O_MULH: W((u64)(((i128)(i64)A * (i128)(i64)B) >> 64));
            case O_MULHSU: W((u64)(((i128)(i64)A * (i128)(u128)B) >> 64));
            case O_MULHU: W((u64)(((u128)A * (u128)B) >> 64));
            case O_DIV:
                W(B == 0 ? ~0ULL : ((i64)A == INT64_MIN && (i64)B == -1) ? A : (u64)((i64)A / (i64)B));
            case O_DIVU: W(B ? A / B : ~0ULL);
            case O_REM:
                W(B == 0 ? A : ((i64)A == INT64_MIN && (i64)B == -1) ? 0 : (u64)((i64)A % (i64)B));
            case O_REMU: W(B ? A % B : A);
            case O_ADDW: W(SEXT32(A + B));
            case O_SUBW: W(SEXT32(A - B));
            case O_SLLW: W(SEXT32((u32)A << (B & 31)));
            case O_SRLW: W(SEXT32((u32)A >> (B & 31)));
            case O_SRAW: W(SEXT32((i32)A >> (B & 31)));
            case O_MULW: W(SEXT32((u32)A * (u32)B));
            case O_DIVW: {
                i32 sa = (i32)A, sb = (i32)B;
                W(sb == 0 ? ~0ULL : (sa == INT32_MIN && sb == -1) ? SEXT32(sa) : SEXT32(sa / sb));
            }
            case O_DIVUW: W((u32)B ? SEXT32((u32)A / (u32)B) : ~0ULL);
            case O_REMW: {
                i32 sa = (i32)A, sb = (i32)B;
                W(sb == 0 ? SEXT32(sa) : (sa == INT32_MIN && sb == -1) ? 0 : SEXT32(sa % sb));
            }
            case O_REMUW: W((u32)B ? SEXT32((u32)A % (u32)B) : SEXT32((u32)A));
            case O_LUI: W((u64)I);
            case O_AUIPC: W(pc + I);
            case O_NOP: pc += d->il; cnt++; goto next;
            case O_JAL: X[d->rd] = pc + d->il; pc += I; cnt++; goto next;
            case O_JALR: { u64 t = (A + I) & ~1ULL; X[d->rd] = pc + d->il; pc = t; cnt++; goto next; }
#define BR(c) do { pc = (c) ? pc + I : pc + d->il; cnt++; goto next; } while (0)
            case O_BEQ: BR(A == B);
            case O_BNE: BR(A != B);
            case O_BLT: BR((i64)A < (i64)B);
            case O_BGE: BR((i64)A >= (i64)B);
            case O_BLTU: BR(A < B);
            case O_BGEU: BR(A >= B);
            case O_LB: ADDR(A + I, 1, 1, o); W((u64)(i64)(int8_t)ram[o]);
            case O_LBU: ADDR(A + I, 1, 1, o); W(ram[o]);
            case O_LH: { u64 va = A + I; int16_t t; ADDR(va, 1, 2, o); memcpy(&t, ram + o, 2); W((u64)(i64)t); }
            case O_LHU: { u64 va = A + I; uint16_t t; ADDR(va, 1, 2, o); memcpy(&t, ram + o, 2); W(t); }
            case O_LW: { u64 va = A + I; i32 t; ADDR(va, 1, 4, o); memcpy(&t, ram + o, 4); W((u64)(i64)t); }
            case O_LWU: { u64 va = A + I; u32 t; ADDR(va, 1, 4, o); memcpy(&t, ram + o, 4); W(t); }
            case O_LD: { u64 va = A + I, t; ADDR(va, 1, 8, o); memcpy(&t, ram + o, 8); W(t); }
#define ST(size, T) do { u64 va = A + I; ADDR(va, 2, size, o); T t = (T)B; memcpy(ram + o, &t, size); \
                         pc += d->il; cnt++; goto next; } while (0)
            case O_SB: ST(1, uint8_t);
            case O_SH: ST(2, uint16_t);
            case O_SW: ST(4, u32);
            case O_SD: ST(8, u64);
#undef W
#undef BR
#undef ST
            default:
                insn = d->insn;
                il = d->il;
                if (!insn) PYTHON();
                goto decoded;
            }
        }
        if ((lo & 3) == 3) {
            if ((pc & 4095) == 4094) {                  /* straddles a page */
                u64 o2;
                int r = xl(h, &md, pc + 2, 0, &o2);
                if (r == T_FAULT) TRAP(12, pc + 2);
                if (r == T_ACCESS) TRAP(1, pc + 2);
                if (r == T_IO) PYTHON();
                fvpn = ~0ULL;                           /* the TLB slot may have moved */
                insn = lo | ((u32)(ram[o2] | ((u32)ram[o2 + 1] << 8)) << 16);
            } else {
                insn = lo | ((u32)(ram[off + 2] | ((u32)ram[off + 3] << 8)) << 16);
            }
            il = 4;
        } else {
            insn = exp_tab[lo];
            if (!insn) PYTHON();
            il = 2;
        }
    decoded:;
        u32 opc = insn & 0x7F, rd = (insn >> 7) & 31, f3 = (insn >> 12) & 7;
        u32 rs1 = (insn >> 15) & 31, rs2 = (insn >> 20) & 31, f7 = insn >> 25;
        u64 a = x[rs1], b = x[rs2];
        if (!rs1) a = 0;
        if (!rs2) b = 0;
        i64 immi = (i64)(i32)insn >> 20;
        u64 npc = pc + il;
        u64 v;

        switch (opc) {
        case 0x13: {                                    /* OP-IMM */
            u32 sh = (insn >> 20) & 63;
            switch (f3) {
            case 0: v = a + immi; break;
            case 1: if (insn >> 26) PYTHON(); v = a << sh; break;
            case 2: v = (i64)a < immi; break;
            case 3: v = a < (u64)immi; break;
            case 4: v = a ^ immi; break;
            case 5:
                if ((insn >> 26) == 0) v = a >> sh;
                else if ((insn >> 26) == 0x10) v = (u64)((i64)a >> sh);
                else PYTHON();
                break;
            case 6: v = a | immi; break;
            default: v = a & immi; break;
            }
            if (rd) x[rd] = v;
            break;
        }
        case 0x1B: {                                    /* OP-IMM-32 */
            u32 sh = rs2;
            if (f3 == 0) v = SEXT32(a + immi);
            else if (f3 == 1 && f7 == 0) v = SEXT32((u32)a << sh);
            else if (f3 == 5 && f7 == 0) v = SEXT32((u32)a >> sh);
            else if (f3 == 5 && f7 == 0x20) v = SEXT32((i32)a >> sh);
            else PYTHON();
            if (rd) x[rd] = v;
            break;
        }
        case 0x33:                                      /* OP */
            if (f7 == 1) {
                switch (f3) {
                case 0: v = a * b; break;
                case 1: v = (u64)(((i128)(i64)a * (i128)(i64)b) >> 64); break;
                case 2: v = (u64)(((i128)(i64)a * (i128)(u128)b) >> 64); break;
                case 3: v = (u64)(((u128)a * (u128)b) >> 64); break;
                case 4:
                    if (b == 0) v = ~0ULL;
                    else if ((i64)a == INT64_MIN && (i64)b == -1) v = a;
                    else v = (u64)((i64)a / (i64)b);
                    break;
                case 5: v = b ? a / b : ~0ULL; break;
                case 6:
                    if (b == 0) v = a;
                    else if ((i64)a == INT64_MIN && (i64)b == -1) v = 0;
                    else v = (u64)((i64)a % (i64)b);
                    break;
                default: v = b ? a % b : a; break;
                }
            } else if (f7 == 0) {
                switch (f3) {
                case 0: v = a + b; break;
                case 1: v = a << (b & 63); break;
                case 2: v = (i64)a < (i64)b; break;
                case 3: v = a < b; break;
                case 4: v = a ^ b; break;
                case 5: v = a >> (b & 63); break;
                case 6: v = a | b; break;
                default: v = a & b; break;
                }
            } else if (f7 == 0x20 && f3 == 0) v = a - b;
            else if (f7 == 0x20 && f3 == 5) v = (u64)((i64)a >> (b & 63));
            else PYTHON();
            if (rd) x[rd] = v;
            break;
        case 0x3B:                                      /* OP-32 */
            if (f7 == 1) {
                i32 sa = (i32)a, sb = (i32)b;
                u32 ua = (u32)a, ub = (u32)b;
                switch (f3) {
                case 0: v = SEXT32(ua * ub); break;
                case 4:
                    if (sb == 0) v = ~0ULL;
                    else if (sa == INT32_MIN && sb == -1) v = SEXT32(sa);
                    else v = SEXT32(sa / sb);
                    break;
                case 5: v = ub ? SEXT32(ua / ub) : ~0ULL; break;
                case 6:
                    if (sb == 0) v = SEXT32(sa);
                    else if (sa == INT32_MIN && sb == -1) v = 0;
                    else v = SEXT32(sa % sb);
                    break;
                case 7: v = ub ? SEXT32(ua % ub) : SEXT32(ua); break;
                default: PYTHON();
                }
            } else if (f7 == 0 && f3 == 0) v = SEXT32(a + b);
            else if (f7 == 0x20 && f3 == 0) v = SEXT32(a - b);
            else if (f7 == 0 && f3 == 1) v = SEXT32((u32)a << (b & 31));
            else if (f7 == 0 && f3 == 5) v = SEXT32((u32)a >> (b & 31));
            else if (f7 == 0x20 && f3 == 5) v = SEXT32((i32)a >> (b & 31));
            else PYTHON();
            if (rd) x[rd] = v;
            break;
        case 0x37:                                      /* LUI */
            if (rd) x[rd] = (u64)(i64)(i32)(insn & 0xFFFFF000);
            break;
        case 0x17:                                      /* AUIPC */
            if (rd) x[rd] = pc + (u64)(i64)(i32)(insn & 0xFFFFF000);
            break;
        case 0x6F: {                                    /* JAL */
            i64 imm = sx((((insn >> 31) & 1) << 20) | (((insn >> 12) & 0xFF) << 12) |
                         (((insn >> 20) & 1) << 11) | (((insn >> 21) & 0x3FF) << 1), 21);
            if (rd) x[rd] = npc;
            npc = pc + imm;
            break;
        }
        case 0x67:                                      /* JALR */
            if (f3) PYTHON();
            npc = (a + immi) & ~1ULL;
            if (rd) x[rd] = pc + il;
            break;
        case 0x63: {                                    /* BRANCH */
            int t;
            switch (f3) {
            case 0: t = a == b; break;
            case 1: t = a != b; break;
            case 4: t = (i64)a < (i64)b; break;
            case 5: t = (i64)a >= (i64)b; break;
            case 6: t = a < b; break;
            case 7: t = a >= b; break;
            default: PYTHON();
            }
            if (t) {
                i64 imm = sx((((insn >> 31) & 1) << 12) | (((insn >> 7) & 1) << 11) |
                             (((insn >> 25) & 0x3F) << 5) | (((insn >> 8) & 0xF) << 1), 13);
                npc = pc + imm;
            }
            break;
        }
        case 0x03: {                                    /* LOAD */
            if (f3 == 7) PYTHON();
            u64 va = a + immi, o;
            int size = 1 << (f3 & 3);
            ADDR(va, 1, size, o);
            switch (f3) {
            case 0: v = (u64)(i64)(int8_t)ram[o]; break;
            case 1: { int16_t t; memcpy(&t, ram + o, 2); v = (u64)(i64)t; break; }
            case 2: { i32 t; memcpy(&t, ram + o, 4); v = (u64)(i64)t; break; }
            case 3: memcpy(&v, ram + o, 8); break;
            case 4: v = ram[o]; break;
            case 5: { uint16_t t; memcpy(&t, ram + o, 2); v = t; break; }
            default: { u32 t; memcpy(&t, ram + o, 4); v = t; break; }
            }
            if (rd) x[rd] = v;
            break;
        }
        case 0x23: {                                    /* STORE */
            if (f3 > 3) PYTHON();
            u64 va = a + (u64)sx(((insn >> 25) << 5) | rd, 12), o;
            int size = 1 << f3;
            ADDR(va, 2, size, o);
            switch (f3) {
            case 0: ram[o] = (uint8_t)b; break;
            case 1: { uint16_t t = (uint16_t)b; memcpy(ram + o, &t, 2); break; }
            case 2: { u32 t = (u32)b; memcpy(ram + o, &t, 4); break; }
            default: memcpy(ram + o, &b, 8); break;
            }
            break;
        }
        case 0x07: {                                    /* FLW / FLD */
            if (f3 != 2 && f3 != 3) PYTHON();
            if ((h->mstatus & MSTATUS_FS) != MSTATUS_FS) PYTHON();   /* Python marks it dirty */
            u64 va = a + immi, o;
            int size = f3 == 2 ? 4 : 8;
            ADDR(va, 1, size, o);
            if (size == 4) { u32 t; memcpy(&t, ram + o, 4); f[rd] = 0xFFFFFFFF00000000ULL | t; }
            else memcpy(&f[rd], ram + o, 8);
            break;
        }
        case 0x27: {                                    /* FSW / FSD */
            if (f3 != 2 && f3 != 3) PYTHON();
            if (!(h->mstatus & MSTATUS_FS)) PYTHON();
            u64 va = a + (u64)sx(((insn >> 25) << 5) | rd, 12), o;
            int size = f3 == 2 ? 4 : 8;
            ADDR(va, 2, size, o);
            if (size == 4) memcpy(ram + o, &f[rs2], 4);
            else memcpy(ram + o, &f[rs2], 8);
            break;
        }
        case 0x2F: {                                    /* AMO, LR, SC */
            u32 f5 = insn >> 27;
            if (f3 != 2 && f3 != 3) PYTHON();
            u64 va = a, o;
            int size = f3 == 2 ? 4 : 8;
            if (va & (size - 1)) PYTHON();
            if (f5 == 2) {                              /* LR */
                if (rs2) PYTHON();
                ADDR(va, 1, size, o);
                if (size == 4) { i32 t; memcpy(&t, ram + o, 4); v = (u64)(i64)t; }
                else memcpy(&v, ram + o, 8);
                h->reservation = va;
                if (rd) x[rd] = v;
                break;
            }
            if (f5 == 3) {                              /* SC */
                if (h->reservation == va) {
                    ADDR(va, 2, size, o);
                    if (size == 4) memcpy(ram + o, &b, 4);
                    else memcpy(ram + o, &b, 8);
                    v = 0;
                } else {
                    v = 1;
                }
                h->reservation = ~0ULL;
                if (rd) x[rd] = v;
                break;
            }
            ADDR(va, 2, size, o);
            if (size == 4) {
                i32 old; memcpy(&old, ram + o, 4);
                i32 bv = (i32)b, nv;
                switch (f5) {
                case 0x01: nv = bv; break;
                case 0x00: nv = (i32)((u32)old + (u32)bv); break;
                case 0x04: nv = old ^ bv; break;
                case 0x0C: nv = old & bv; break;
                case 0x08: nv = old | bv; break;
                case 0x10: nv = old < bv ? old : bv; break;
                case 0x14: nv = old > bv ? old : bv; break;
                case 0x18: nv = (u32)old < (u32)bv ? old : bv; break;
                case 0x1C: nv = (u32)old > (u32)bv ? old : bv; break;
                default: PYTHON();
                }
                memcpy(ram + o, &nv, 4);
                if (rd) x[rd] = (u64)(i64)old;
            } else {
                u64 old; memcpy(&old, ram + o, 8);
                u64 nv;
                switch (f5) {
                case 0x01: nv = b; break;
                case 0x00: nv = old + b; break;
                case 0x04: nv = old ^ b; break;
                case 0x0C: nv = old & b; break;
                case 0x08: nv = old | b; break;
                case 0x10: nv = (i64)old < (i64)b ? old : b; break;
                case 0x14: nv = (i64)old > (i64)b ? old : b; break;
                case 0x18: nv = old < b ? old : b; break;
                case 0x1C: nv = old > b ? old : b; break;
                default: PYTHON();
                }
                memcpy(ram + o, &nv, 8);
                if (rd) x[rd] = old;
            }
            break;
        }
        case 0x73: {                                    /* SYSTEM: ecall, sret, a few CSRs */
            if (insn == 0x00000073) TRAP(8 + h->priv, 0);              /* ecall */
            if (insn == 0x10200073) {                                  /* sret */
                u64 st = h->mstatus;
                if (h->priv == 0 || (h->priv == 1 && (st & MSTATUS_TSR))) PYTHON();
                u64 spp = (st >> 8) & 1;
                st = (st & ~(MSTATUS_SIE | MSTATUS_SPP | MSTATUS_MPRV)) |
                     ((st >> 4) & MSTATUS_SIE) | MSTATUS_SPIE;
                h->mstatus = st;
                h->priv = spp;
                pc = h->sepc;
                MODE_CHANGED();
                cnt++;
                if (irq_ready(h, st)) { status = EXIT_STOP; goto out; }
                goto next;
            }
            if (!f3 || f3 == 4) PYTHON();
            u32 csr = insn >> 20;
            {
                u64 *p = 0, mask = ~0ULL;
                switch (csr) {
                case 0x105: p = &h->stvec; mask = ~2ULL; break;
                case 0x140: p = &h->sscratch; break;
                case 0x141: p = &h->sepc; mask = ~1ULL; break;
                case 0x142: p = &h->scause; break;
                case 0x143: p = &h->stval; break;
                }
                if (p) {
                    if (h->priv == 0) PYTHON();
                    u64 old = *p;
                    u64 src = f3 >= 5 ? rs1 : a;
                    if ((f3 & 3) == 1 || rs1 != 0) {
                        u64 nv = (f3 & 3) == 1 ? src : (f3 & 3) == 2 ? (old | src) : (old & ~src);
                        *p = nv & mask;
                    }
                    if (rd) x[rd] = old;
                    break;
                }
            }
            u64 src = f3 >= 5 ? rs1 : a;
            int write = (f3 & 3) == 1 || rs1 != 0;
            if (csr == 0x100) {                         /* sstatus */
                if (h->priv == 0) PYTHON();
                u64 st = h->mstatus;
                u64 old = st & SSTATUS_RMASK;
                if ((st & MSTATUS_FS) == MSTATUS_FS) old |= 1ULL << 63;
                if (write) {
                    u64 nv = (f3 & 3) == 1 ? src : (f3 & 3) == 2 ? (old | src) : (old & ~src);
                    u64 ns = (st & ~SSTATUS_WMASK) | (nv & SSTATUS_WMASK);
                    /* (a SUM/MXR change needs nothing here: every access checks
                       the current mstatus; Python reselects its TLBs afterwards) */
                    h->mstatus = ns;
                    if ((st ^ ns) & (MSTATUS_SUM | MSTATUS_MXR)) MODE_CHANGED();
                    if (rd) x[rd] = old;
                    if (irq_ready(h, ns)) {             /* take it now: end the batch */
                        pc = npc;
                        cnt++;
                        status = EXIT_STOP;
                        goto out;
                    }
                } else if (rd) {
                    x[rd] = old;
                }
                break;
            }
            if (csr >= 1 && csr <= 3) {                 /* fflags, frm, fcsr */
                if (!(h->mstatus & MSTATUS_FS)) PYTHON();
                u64 old = csr == 1 ? h->fflags : csr == 2 ? h->frm : (h->frm << 5) | h->fflags;
                if (write) {
                    u64 nv = (f3 & 3) == 1 ? src : (f3 & 3) == 2 ? (old | src) : (old & ~src);
                    if (csr == 1) h->fflags = nv & 0x1F;
                    else if (csr == 2) h->frm = nv & 7;
                    else { h->frm = (nv >> 5) & 7; h->fflags = nv & 0x1F; }
                    h->mstatus |= MSTATUS_FS;
                }
                if (rd) x[rd] = old;
                break;
            }
            if ((csr == 0xC01 || csr == 0xC00 || csr == 0xC02) && !write) {  /* time, cycle, instret */
                u32 idx = csr & 3;
                if (h->priv < 3 && !((h->mcounteren >> idx) & 1)) PYTHON();
                if (h->priv == 0 && !((h->scounteren >> idx) & 1)) PYTHON();
                if (rd) x[rd] = idx == 1 ? h->mtime : h->instret;
                break;
            }
            PYTHON();
        }
        case 0x0F:                                      /* FENCE, FENCE.I */
            if (f3 > 1) PYTHON();
            break;
        case 0x53: case 0x43: case 0x47: case 0x4B: case 0x4F:   /* F, D */
            if (!fp_exec(h, insn, x, f)) PYTHON();
            break;
        default:
            PYTHON();
        }
        pc = npc;
        cnt++;
    next:;
    }
out:
    h->pc = pc;
    h->count = cnt;
    h->status = status;
    h->cause = cause;
    h->tval = tval;
}
