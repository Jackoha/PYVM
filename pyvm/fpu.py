"""IEEE-754 binary32 / binary64 arithmetic for the RISC-V F and D extensions.

Values are handled as raw bit patterns (Python ints).  Every operation is
computed *exactly* with Python integers and then rounded once, so results,
rounding modes and the accrued exception flags all follow the spec.  This is
slow compared with native floats, but floating point is rare in a kernel boot
and correctness matters more here than speed.

Every operation returns ``(bits, flags)``.
"""

from math import isqrt

# fflags bits
NX, UF, OF, DZ, NV = 1, 2, 4, 8, 16
# rounding modes
RNE, RTZ, RDN, RUP, RMM = 0, 1, 2, 3, 4

# operand kinds returned by unpack()
ZERO, FINITE, INF, QNAN, SNAN = 0, 1, 2, 3, 4


class Fmt:
    __slots__ = ('width', 'p', 'bias', 'emin', 'emax', 'expmask', 'fracmask',
                 'signbit', 'qnan', 'inf', 'maxfin', 'mask')

    def __init__(self, width, ebits):
        self.width = width
        self.p = width - ebits                  # precision incl. hidden bit
        self.bias = (1 << (ebits - 1)) - 1
        self.emax = self.bias
        self.emin = 1 - self.bias
        self.expmask = (1 << ebits) - 1
        self.fracmask = (1 << (self.p - 1)) - 1
        self.signbit = 1 << (width - 1)
        self.qnan = (self.expmask << (self.p - 1)) | (1 << (self.p - 2))
        self.inf = self.expmask << (self.p - 1)
        self.maxfin = ((self.expmask - 1) << (self.p - 1)) | self.fracmask
        self.mask = (1 << width) - 1


F32 = Fmt(32, 8)
F64 = Fmt(64, 11)

BOX = 0xFFFFFFFF00000000


def unbox(v):
    """Read a single-precision value from a 64-bit FP register (NaN-boxing)."""
    if (v >> 32) == 0xFFFFFFFF:
        return v & 0xFFFFFFFF
    return F32.qnan


def unpack(b, F):
    """-> (kind, sign, mant, exp2) with value = (-1)^sign * mant * 2^exp2."""
    p1 = F.p - 1
    sign = b >> (F.width - 1)
    e = (b >> p1) & F.expmask
    frac = b & F.fracmask
    if e == F.expmask:
        if frac == 0:
            return INF, sign, 0, 0
        return (QNAN if frac >> (p1 - 1) else SNAN), sign, 0, 0
    if e == 0:
        if frac == 0:
            return ZERO, sign, 0, 0
        return FINITE, sign, frac, F.emin - p1
    return FINITE, sign, frac | (1 << p1), e - F.bias - p1


def is_nan(b, F):
    return ((b >> (F.p - 1)) & F.expmask) == F.expmask and (b & F.fracmask) != 0


def is_snan(b, F):
    return is_nan(b, F) and not (b >> (F.p - 2)) & 1


def round_pack(sign, n, d, e2, F, rm):
    """Round the exact value (-1)^sign * n/d * 2^e2 (n >= 0, d > 0) to format F."""
    if n == 0:
        return (F.signbit if sign else 0), 0
    p = F.p
    # 2^k <= n/d < 2^(k+1)
    k = n.bit_length() - d.bit_length()
    if k >= 0:
        if n < (d << k):
            k -= 1
    elif (n << -k) < d:
        k -= 1
    E = k + e2                                   # exponent of the exact value
    q = (E if E > F.emin else F.emin) - (p - 1)  # exponent of one ulp
    s = e2 - q
    if s >= 0:
        num, den = n << s, d
    else:
        num, den = n, d << -s
    m, r = divmod(num, den)
    flags = 0
    if r:
        flags = NX
        up = _round_up(rm, sign, m, r, den)
        if E < F.emin and _tiny_after_rounding(sign, n, d, e2, E, F, rm):
            flags |= UF
        if up:
            m += 1
            if m == (1 << p):
                m >>= 1
                q += 1
    if m >= (1 << (p - 1)):
        ef = q + (p - 1) + F.bias
        if ef >= F.expmask:                      # overflow
            flags |= OF | NX
            if rm == RTZ or (rm == RDN and not sign) or (rm == RUP and sign):
                return (F.signbit if sign else 0) | F.maxfin, flags
            return (F.signbit if sign else 0) | F.inf, flags
        bits = (ef << (p - 1)) | (m - (1 << (p - 1)))
    else:
        bits = m                                  # subnormal
    if sign:
        bits |= F.signbit
    return bits, flags


def _round_up(rm, sign, m, r, den):
    if rm == RNE:
        r2 = r << 1
        return r2 > den or (r2 == den and (m & 1))
    if rm == RTZ:
        return False
    if rm == RDN:
        return bool(sign)
    if rm == RUP:
        return not sign
    return (r << 1) >= den                       # RMM


def _tiny_after_rounding(sign, n, d, e2, E, F, rm):
    # RISC-V detects tininess after rounding: the value is tiny unless rounding
    # it with an unbounded exponent range reaches 2^emin.
    if E != F.emin - 1:
        return True
    p = F.p
    q = E - (p - 1)
    s = e2 - q
    if s >= 0:
        num, den = n << s, d
    else:
        num, den = n, d << -s
    m, r = divmod(num, den)
    if r and _round_up(rm, sign, m, r, den):
        m += 1
    return m < (1 << p)


def _nan_result(F, *ops):
    flags = 0
    for b in ops:
        if is_snan(b, F):
            flags = NV
    return F.qnan, flags


# ---------------------------------------------------------------- arithmetic

def add(a, b, F, rm, negate_b=False):
    ka, sa, ma, ea = unpack(a, F)
    kb, sb, mb, eb = unpack(b, F)
    if negate_b:
        sb ^= 1
    if ka >= QNAN or kb >= QNAN:
        return _nan_result(F, a, b)
    if ka == INF or kb == INF:
        if ka == INF and kb == INF and sa != sb:
            return F.qnan, NV
        s = sa if ka == INF else sb
        return (F.signbit if s else 0) | F.inf, 0
    return _add_exact(sa, ma, ea, sb, mb, eb, ka == ZERO and kb == ZERO, F, rm)


def _add_exact(sa, ma, ea, sb, mb, eb, both_zero, F, rm):
    if both_zero or (ma == 0 and mb == 0):
        if sa == sb:
            return (F.signbit if sa else 0), 0
        return (F.signbit if rm == RDN else 0), 0
    e = ea if ea < eb else eb
    va = ma << (ea - e)
    vb = mb << (eb - e)
    total = (-va if sa else va) + (-vb if sb else vb)
    if total == 0:
        return (F.signbit if rm == RDN else 0), 0
    if total < 0:
        return round_pack(1, -total, 1, e, F, rm)
    return round_pack(0, total, 1, e, F, rm)


def sub(a, b, F, rm):
    return add(a, b, F, rm, True)


def mul(a, b, F, rm):
    ka, sa, ma, ea = unpack(a, F)
    kb, sb, mb, eb = unpack(b, F)
    if ka >= QNAN or kb >= QNAN:
        return _nan_result(F, a, b)
    s = sa ^ sb
    if ka == INF or kb == INF:
        if ka == ZERO or kb == ZERO:
            return F.qnan, NV
        return (F.signbit if s else 0) | F.inf, 0
    if ka == ZERO or kb == ZERO:
        return (F.signbit if s else 0), 0
    return round_pack(s, ma * mb, 1, ea + eb, F, rm)


def div(a, b, F, rm):
    ka, sa, ma, ea = unpack(a, F)
    kb, sb, mb, eb = unpack(b, F)
    if ka >= QNAN or kb >= QNAN:
        return _nan_result(F, a, b)
    s = sa ^ sb
    sbit = F.signbit if s else 0
    if ka == INF:
        if kb == INF:
            return F.qnan, NV
        return sbit | F.inf, 0
    if kb == INF:
        return sbit, 0
    if kb == ZERO:
        if ka == ZERO:
            return F.qnan, NV
        return sbit | F.inf, DZ
    if ka == ZERO:
        return sbit, 0
    return round_pack(s, ma, mb, ea - eb, F, rm)


def sqrt(a, F, rm):
    ka, sa, ma, ea = unpack(a, F)
    if ka >= QNAN:
        return _nan_result(F, a)
    if ka == ZERO:
        return a, 0
    if sa:
        return F.qnan, NV
    if ka == INF:
        return a, 0
    if ea & 1:
        ma <<= 1
        ea -= 1
    k = F.p + 4
    M = ma << (2 * k)
    r = isqrt(M)
    if r * r == M:
        return round_pack(0, r, 1, (ea >> 1) - k, F, rm)
    # inexact: r < sqrt < r+1 and r has >= p+2 bits, so r+1/2 rounds identically
    return round_pack(0, 2 * r + 1, 2, (ea >> 1) - k, F, rm)


def fma(a, b, c, F, rm, neg_prod=False, neg_c=False):
    """(+/-)(a*b) (+/-) c with a single rounding."""
    ka, sa, ma, ea = unpack(a, F)
    kb, sb, mb, eb = unpack(b, F)
    kc, sc, mc, ec = unpack(c, F)
    ps = sa ^ sb ^ (1 if neg_prod else 0)
    if neg_c:
        sc ^= 1
    inf_times_zero = (ka == INF and kb == ZERO) or (ka == ZERO and kb == INF)
    if ka >= QNAN or kb >= QNAN or kc >= QNAN:
        bits, flags = _nan_result(F, a, b, c)
        if inf_times_zero:
            flags |= NV
        return bits, flags
    if inf_times_zero:
        return F.qnan, NV
    if ka == INF or kb == INF:
        if kc == INF and sc != ps:
            return F.qnan, NV
        return (F.signbit if ps else 0) | F.inf, 0
    if kc == INF:
        return (F.signbit if sc else 0) | F.inf, 0
    pz = ka == ZERO or kb == ZERO
    return _add_exact(ps, 0 if pz else ma * mb, ea + eb, sc, mc, ec,
                      pz and kc == ZERO, F, rm)


# ---------------------------------------------------------------- compare

def _key(b, F, zero_equal):
    mag = b & ~F.signbit & F.mask
    if b & F.signbit:
        return -mag if zero_equal else -mag - 1
    return mag


def minmax(a, b, F, is_max):
    an, bn = is_nan(a, F), is_nan(b, F)
    flags = NV if (is_snan(a, F) or is_snan(b, F)) else 0
    if an and bn:
        return F.qnan, flags
    if an:
        return b, flags
    if bn:
        return a, flags
    ka, kb = _key(a, F, False), _key(b, F, False)
    if is_max:
        return (a if ka >= kb else b), flags
    return (a if ka <= kb else b), flags


def compare(a, b, F, op):
    """op: 0 = le, 1 = lt, 2 = eq.  -> (0|1, flags)"""
    if is_nan(a, F) or is_nan(b, F):
        if op == 2:
            return 0, (NV if (is_snan(a, F) or is_snan(b, F)) else 0)
        return 0, NV
    ka, kb = _key(a, F, True), _key(b, F, True)
    if op == 0:
        return int(ka <= kb), 0
    if op == 1:
        return int(ka < kb), 0
    return int(ka == kb), 0


def classify(a, F):
    k, s, m, e = unpack(a, F)
    if k == INF:
        return 1 << 0 if s else 1 << 7
    if k == ZERO:
        return 1 << 3 if s else 1 << 4
    if k == FINITE:
        sub_ = ((a >> (F.p - 1)) & F.expmask) == 0
        if s:
            return 1 << 2 if sub_ else 1 << 1
        return 1 << 5 if sub_ else 1 << 6
    return 1 << 8 if k == SNAN else 1 << 9


def sgnj(a, b, F, mode):
    """mode 0: fsgnj, 1: fsgnjn, 2: fsgnjx"""
    sb = F.signbit
    if mode == 0:
        s = b & sb
    elif mode == 1:
        s = (b & sb) ^ sb
    else:
        s = (a ^ b) & sb
    return (a & ~sb & F.mask) | s


# ---------------------------------------------------------------- conversion

def to_int(a, F, rm, width, signed):
    """Float -> integer.  Returns (python int in range, flags)."""
    if signed:
        lo, hi = -(1 << (width - 1)), (1 << (width - 1)) - 1
    else:
        lo, hi = 0, (1 << width) - 1
    k, s, m, e = unpack(a, F)
    if k >= QNAN:
        return hi, NV
    if k == INF:
        return (lo if s else hi), NV
    if k == ZERO:
        return 0, 0
    if e >= 0:
        v, inexact = m << e, False
    else:
        den = 1 << -e
        v, r = divmod(m, den)
        inexact = r != 0
        if inexact and _round_up(rm, s, v, r, den):
            v += 1
    if s:
        v = -v
    if v < lo or v > hi:
        return (lo if v < lo else hi), NV
    return v, (NX if inexact else 0)


def from_int(v, F, rm):
    if v < 0:
        return round_pack(1, -v, 1, 0, F, rm)
    return round_pack(0, v, 1, 0, F, rm)


def convert(a, Fsrc, Fdst, rm):
    k, s, m, e = unpack(a, Fsrc)
    if k >= QNAN:
        return Fdst.qnan, (NV if k == SNAN else 0)
    sbit = Fdst.signbit if s else 0
    if k == INF:
        return sbit | Fdst.inf, 0
    if k == ZERO:
        return sbit, 0
    return round_pack(s, m, 1, e, Fdst, rm)
