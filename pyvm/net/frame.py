"""Packing, parsing and checksums for the headers the network backends need:
Ethernet, ARP, IPv4, ICMP, UDP and TCP.

Addresses are handled as raw bytes (4 bytes for IPv4, 6 for a MAC) because that
is the form they take on the wire; `ip2b`/`b2ip` convert to and from text.
"""

import struct

ETH_HDR = 14
ETH_P_IP = 0x0800
ETH_P_ARP = 0x0806
ETH_P_IPV6 = 0x86DD

IPPROTO_ICMP, IPPROTO_TCP, IPPROTO_UDP = 1, 6, 17

ICMP_ECHOREPLY, ICMP_UNREACH, ICMP_ECHO, ICMP_TIMXCEED = 0, 3, 8, 11

TH_FIN, TH_SYN, TH_RST, TH_PSH, TH_ACK, TH_URG = 1, 2, 4, 8, 16, 32

BCAST_MAC = b'\xff' * 6
BCAST_IP = b'\xff' * 4
ANY_IP = b'\0' * 4

M32 = 0xFFFFFFFF


def ip2b(s):
    parts = s.split('.')
    if len(parts) != 4:
        raise ValueError('not an IPv4 address: %r' % s)
    return bytes(int(p) for p in parts)


def b2ip(b):
    return '%d.%d.%d.%d' % tuple(b)


def mac2b(s):
    parts = s.replace('-', ':').split(':')
    if len(parts) != 6:
        raise ValueError('not a MAC address: %r' % s)
    return bytes(int(p, 16) for p in parts)


def b2mac(b):
    return ':'.join('%02x' % c for c in b)


def parse_cidr(s):
    """'10.0.2.0/24' -> (network, netmask) as 4-byte values."""
    addr, _, bits = s.partition('/')
    n = int(bits) if bits else 24
    if not 0 < n <= 32:
        raise ValueError('bad prefix length in %r' % s)
    mask = (M32 << (32 - n)) & M32
    net = int.from_bytes(ip2b(addr), 'big') & mask
    return net.to_bytes(4, 'big'), mask.to_bytes(4, 'big')


def same_net(a, b, mask):
    return bytes(x & m for x, m in zip(a, mask)) == bytes(x & m for x, m in zip(b, mask))


def broadcast_of(net, mask):
    return bytes((n & m) | (~m & 0xFF) for n, m in zip(net, mask))


# ---------------------------------------------------------------- checksums
def ones_sum(data, init=0):
    """RFC 1071 one's-complement sum of 16-bit words."""
    if len(data) & 1:
        data = bytes(data) + b'\0'
    s = init + sum(struct.unpack('!%dH' % (len(data) >> 1), data))
    while s > 0xFFFF:
        s = (s & 0xFFFF) + (s >> 16)
    return s


def checksum(data, init=0):
    return ~ones_sum(data, init) & 0xFFFF


def l4_checksum(src, dst, proto, seg):
    """TCP/UDP checksum over the IPv4 pseudo header plus the segment."""
    ph = src + dst + struct.pack('!BBH', 0, proto, len(seg))
    return checksum(seg, ones_sum(ph))


# ---------------------------------------------------------------- Ethernet
def eth(dst, src, etype, payload):
    return dst + src + struct.pack('!H', etype) + bytes(payload)


def parse_eth(f):
    """-> (dst_mac, src_mac, ethertype, payload) or None."""
    if len(f) < ETH_HDR:
        return None
    return bytes(f[0:6]), bytes(f[6:12]), f[12] << 8 | f[13], f[ETH_HDR:]


# ---------------------------------------------------------------- ARP
def arp(op, sha, spa, tha, tpa):
    return struct.pack('!HHBBH', 1, ETH_P_IP, 6, 4, op) + sha + spa + tha + tpa


def parse_arp(p):
    """-> (op, sender_mac, sender_ip, target_ip) or None."""
    if len(p) < 28:
        return None
    htype, ptype, hlen, plen, op = struct.unpack_from('!HHBBH', p, 0)
    if htype != 1 or ptype != ETH_P_IP or hlen != 6 or plen != 4:
        return None
    return op, bytes(p[8:14]), bytes(p[14:18]), bytes(p[24:28])


# ---------------------------------------------------------------- IPv4
def build_ip4(src, dst, proto, payload, ident=0, ttl=64, mtu=0):
    """IPv4 packet(s) carrying `payload`, fragmented to fit `mtu` if needed."""
    limit = max((mtu or 0xFFFF) - 20, 8)
    if len(payload) <= limit:
        pieces = [(0, payload, 0)]
    else:
        room = limit & ~7
        pieces = []
        off = 0
        while off < len(payload):
            piece = payload[off:off + room]
            off += len(piece)
            pieces.append((off - len(piece), piece, 0x2000 if off < len(payload) else 0))
    out = []
    for off, piece, mf in pieces:
        hdr = struct.pack('!BBHHHBBH', 0x45, 0, 20 + len(piece), ident & 0xFFFF,
                          mf | (off >> 3), ttl, proto, 0) + src + dst
        out.append(hdr[:10] + struct.pack('!H', checksum(hdr)) + hdr[12:] + bytes(piece))
    return out


def parse_ip4(p):
    """-> dict(src, dst, proto, payload, ident, mf, off) or None."""
    if len(p) < 20 or (p[0] >> 4) != 4:
        return None
    ihl = (p[0] & 15) * 4
    total = struct.unpack_from('!H', p, 2)[0]
    if ihl < 20 or total < ihl or len(p) < total:
        return None
    ident, frag = struct.unpack_from('!HH', p, 4)
    return {'src': bytes(p[12:16]), 'dst': bytes(p[16:20]), 'proto': p[9],
            'ident': ident, 'mf': bool(frag & 0x2000), 'off': (frag & 0x1FFF) * 8,
            'payload': p[ihl:total]}


# ---------------------------------------------------------------- UDP
def build_udp(src, dst, sport, dport, data):
    seg = struct.pack('!HHHH', sport, dport, 8 + len(data), 0) + bytes(data)
    ck = l4_checksum(src, dst, IPPROTO_UDP, seg) or 0xFFFF
    return seg[:6] + struct.pack('!H', ck) + seg[8:]


def parse_udp(seg):
    """-> (sport, dport, data) or None."""
    if len(seg) < 8:
        return None
    sport, dport, ln, _ = struct.unpack_from('!HHHH', seg, 0)
    end = len(seg) if ln < 8 else min(ln, len(seg))
    return sport, dport, seg[8:end]


# ---------------------------------------------------------------- TCP
def build_tcp(src, dst, sport, dport, seq, ack, flags, window, data=b'', options=b''):
    options = bytes(options)
    options += b'\0' * (-len(options) % 4)
    off = (20 + len(options)) >> 2
    seg = struct.pack('!HHIIBBHHH', sport, dport, seq & M32, ack & M32,
                      off << 4, flags, window, 0, 0) + options + bytes(data)
    ck = l4_checksum(src, dst, IPPROTO_TCP, seg)
    return seg[:16] + struct.pack('!H', ck) + seg[18:]


def parse_tcp(seg):
    """-> (sport, dport, seq, ack, flags, window, data, options) or None."""
    if len(seg) < 20:
        return None
    sport, dport, seq, ack, offb, flags, win = struct.unpack_from('!HHIIBBH', seg, 0)
    off = (offb >> 4) * 4
    if off < 20 or len(seg) < off:
        return None
    return sport, dport, seq, ack, flags, win, seg[off:], seg[20:off]


def mss_option(mss):
    return struct.pack('!BBH', 2, 4, mss)


def option_mss(options):
    """The peer's advertised MSS, or None."""
    i, n = 0, len(options)
    while i < n:
        kind = options[i]
        if kind == 0:
            break
        if kind == 1:
            i += 1
            continue
        if i + 1 >= n:
            break
        ln = options[i + 1]
        if ln < 2 or i + ln > n:
            break
        if kind == 2 and ln == 4:
            return struct.unpack_from('!H', options, i + 2)[0]
        i += ln
    return None


def rel(base, wire, ref):
    """Map a 32-bit sequence number to the absolute byte offset nearest `ref`.

    Sequence numbers wrap; offsets counted from the initial sequence number do
    not, which keeps the connection bookkeeping free of wrap-around cases."""
    d = ((wire - base) - ref) & M32
    if d >= 0x80000000:
        d -= 0x100000000
    return ref + d
