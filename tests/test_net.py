#!/usr/bin/env python3
"""Exercise pyvm's user-mode network stack without booting anything.

A small fake guest (ARP/DHCP/ICMP/UDP and a minimal TCP implementation) talks to
the backend through the same `send()` / `recv()` calls the virtio-net device
uses, so the whole NAT path is covered: address resolution, the DHCP server, the
ICMP responder, UDP flows, outbound TCP, port forwarding and bulk transfer.

    python3 tests/test_net.py [-v]

Everything runs over the host's loopback interface, so no outside network and no
privileges are needed.  Exit status 0 when every test passes.
"""

import argparse
import os
import random
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from pyvm.net import frame                                          # noqa: E402
from pyvm.net.frame import TH_ACK, TH_FIN, TH_PSH, TH_RST, TH_SYN   # noqa: E402
from pyvm.net.slirp import Slirp                                    # noqa: E402

M32 = 0xFFFFFFFF
GUEST_MAC = frame.mac2b('52:54:00:12:34:56')
VERBOSE = False


def log(*a):
    if VERBOSE:
        print('   ', *a)


class Timeout(Exception):
    pass


# ---------------------------------------------------------------- the fake guest
class GuestTcp:
    """Just enough TCP to be a believable peer: in-order data, cumulative ACKs."""

    def __init__(self, guest, lport, rip, rport, window=32768):
        self.g = guest
        self.lport = lport
        self.rip = rip
        self.rport = rport
        self.window = window
        self.iss = random.getrandbits(32)
        self.snd_nxt = self.iss
        self.rcv_nxt = 0
        self.data = bytearray()
        self.fin = False
        self.reset = False
        self.established = False

    def key(self):
        return (self.lport, self.rip, self.rport)

    def _send(self, flags, data=b'', options=b''):
        seg = frame.build_tcp(self.g.ip, self.rip, self.lport, self.rport,
                              self.snd_nxt, self.rcv_nxt, flags, self.window,
                              data, options)
        self.g.tx_ip(self.rip, frame.IPPROTO_TCP, seg)
        if flags & (TH_SYN | TH_FIN):
            self.snd_nxt = (self.snd_nxt + 1) & M32
        self.snd_nxt = (self.snd_nxt + len(data)) & M32

    def connect(self):
        self._send(TH_SYN, options=frame.mss_option(1460))
        self.g.wait(lambda: self.established or self.reset, 'SYN-ACK for :%d' % self.rport)
        if self.reset:
            raise AssertionError('connection to %s:%d was refused'
                                 % (frame.b2ip(self.rip), self.rport))

    def accept(self):
        """We are the server: the stack opened this connection towards us."""
        self._send(TH_SYN | TH_ACK, options=frame.mss_option(1460))
        self.g.wait(lambda: self.established, 'ACK completing the handshake')

    def write(self, data):
        for i in range(0, len(data), 1460):
            self._send(TH_ACK | TH_PSH, data[i:i + 1460])

    def close(self):
        self._send(TH_ACK | TH_FIN)

    def on_segment(self, seq, ack, flags, win, data, options):
        if flags & TH_RST:
            self.reset = True
            return
        if flags & TH_SYN and flags & TH_ACK:           # SYN-ACK: finish our open
            self.rcv_nxt = (seq + 1) & M32
            self.established = True
            self._send(TH_ACK)
            return
        if flags & TH_ACK and not self.established:     # ACK of our SYN-ACK
            self.established = True
        if data:
            if seq == self.rcv_nxt:
                self.data += data
                self.rcv_nxt = (seq + len(data)) & M32
            self._send(TH_ACK)
        if flags & TH_FIN:
            if (seq + len(data)) & M32 == self.rcv_nxt:
                self.rcv_nxt = (self.rcv_nxt + 1) & M32
                self.fin = True
            self._send(TH_ACK)


class FakeGuest:
    def __init__(self, backend, ip='10.0.2.15'):
        self.b = backend
        self.mac = GUEST_MAC
        self.ip = frame.ip2b(ip)
        self.conns = {}
        self.others = []            # frames that are not TCP: ARP, DHCP, ICMP, UDP
        self.listen = {}            # port -> GuestTcp waiting for an inbound SYN

    # ------------------------------------------------------------ sending
    def tx(self, etype, payload, dst=None):
        self.b.send(frame.eth(dst or self.b.mac_for(self.b.host_ip), self.mac,
                              etype, payload))

    def tx_ip(self, dst, proto, payload, src=None):
        for pkt in frame.build_ip4(src or self.ip, dst, proto, payload, ident=1, mtu=1500):
            self.tx(frame.ETH_P_IP, pkt)

    def tx_udp(self, dst, sport, dport, data, src=None):
        src = src or self.ip
        self.tx_ip(dst, frame.IPPROTO_UDP,
                   frame.build_udp(src, dst, sport, dport, data), src=src)

    # ------------------------------------------------------------ receiving
    def pump(self):
        got = False
        while True:
            f = self.b.recv()
            if f is None:
                return got
            got = True
            self._dispatch(f)

    def _dispatch(self, f):
        p = frame.parse_eth(f)
        if p is None:
            return
        _, _, etype, payload = p
        if etype != frame.ETH_P_IP:
            self.others.append((etype, payload))
            return
        ip = frame.parse_ip4(payload)
        if ip is None or ip['proto'] != frame.IPPROTO_TCP:
            self.others.append((etype, payload))
            return
        t = frame.parse_tcp(ip['payload'])
        if t is None:
            return
        sport, dport, seq, ack, flags, win, data, options = t
        key = (dport, ip['src'], sport)
        c = self.conns.get(key)
        if c is None:
            if flags & TH_SYN and not flags & TH_ACK and dport in self.listen:
                c = self.listen.pop(dport)
                c.rip, c.rport, c.lport = ip['src'], sport, dport
                c.rcv_nxt = (seq + 1) & M32
                self.conns[c.key()] = c
                c.accept()
                return
            log('unmatched TCP segment for port %d' % dport)
            return
        c.on_segment(seq, ack, flags, win, data, options)

    def wait(self, cond, what, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump()
            if cond():
                return
            time.sleep(0.002)
        raise Timeout('timed out waiting for %s' % what)

    def wait_frame(self, match, what, timeout=5.0):
        """Wait for a non-TCP frame matching `match(etype, payload)`."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump()
            for i, (etype, payload) in enumerate(self.others):
                if match(etype, payload):
                    return self.others.pop(i)
            time.sleep(0.002)
        raise Timeout('timed out waiting for %s' % what)

    # ------------------------------------------------------------ TCP helpers
    def connect(self, rip, rport, lport=None, window=32768):
        lport = lport or random.randint(40000, 60000)
        c = GuestTcp(self, lport, rip, rport, window)
        self.conns[c.key()] = c
        c.connect()
        return c

    def server(self, port, window=32768):
        c = GuestTcp(self, port, self.ip, 0, window)
        self.listen[port] = c
        return c


# ---------------------------------------------------------------- host helpers
class EchoServer(threading.Thread):
    """A host TCP server that echoes back whatever it is sent."""

    def __init__(self, transform=None):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.transform = transform
        self.received = bytearray()
        self.start()

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn:
            if self.transform is not None:
                self.transform(conn)
                return
            while True:
                data = conn.recv(65536)
                if not data:
                    conn.shutdown(socket.SHUT_WR)
                    return
                self.received += data
                conn.sendall(data)


class UdpEcho(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self.start()

    def run(self):
        while True:
            try:
                data, peer = self.sock.recvfrom(65536)
            except OSError:
                return
            self.sock.sendto(b'echo:' + data, peer)


# ---------------------------------------------------------------- the tests
TESTS = []


def test(name):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


@test('ARP resolves the gateway')
def t_arp(s, g):
    g.tx(frame.ETH_P_ARP,
         frame.arp(1, g.mac, g.ip, bytes(6), s.host_ip),
         dst=frame.BCAST_MAC)
    etype, payload = g.wait_frame(lambda e, p: e == frame.ETH_P_ARP, 'an ARP reply')
    op, sha, spa, tpa = frame.parse_arp(payload)
    assert op == 2, 'op %d' % op
    assert spa == s.host_ip, frame.b2ip(spa)
    assert sha == s.mac_for(s.host_ip), frame.b2mac(sha)
    log('gateway %s is at %s' % (frame.b2ip(spa), frame.b2mac(sha)))


def dhcp_request(g, mtype, xid=0x1234abcd):
    pkt = struct.pack('!BBBBIHH', 1, 1, 6, 0, xid, 0, 0)
    pkt += frame.ANY_IP * 4
    pkt += g.mac + bytes(10) + bytes(64) + bytes(128)
    pkt += b'\x63\x82\x53\x63' + bytes([53, 1, mtype, 55, 3, 1, 3, 6]) + b'\xff'
    pkt += bytes(max(0, 300 - len(pkt)))
    g.tx_udp(frame.BCAST_IP, 68, 67, pkt, src=frame.ANY_IP)
    for _ in range(2):
        etype, payload = g.wait_frame(
            lambda e, p: e == frame.ETH_P_IP and frame.parse_ip4(p)
            and frame.parse_ip4(p)['proto'] == frame.IPPROTO_UDP, 'a DHCP reply')
        ip = frame.parse_ip4(payload)
        sport, dport, data = frame.parse_udp(ip['payload'])
        if sport == 67 and dport == 68 and struct.unpack_from('!I', data, 4)[0] == xid:
            opts, i = {}, 240
            while i < len(data) and data[i] != 255:
                if data[i] == 0:
                    i += 1
                    continue
                ln = data[i + 1]
                opts[data[i]] = bytes(data[i + 2:i + 2 + ln])
                i += 2 + ln
            return bytes(data[16:20]), opts
    raise AssertionError('no DHCP reply')


@test('DHCP hands out the guest address')
def t_dhcp(s, g):
    yiaddr, opts = dhcp_request(g, 1)                    # DISCOVER
    assert opts[53] == b'\x02', 'expected an OFFER, got type %r' % opts[53]
    assert yiaddr == s.guest_ip, frame.b2ip(yiaddr)
    assert opts[1] == s.mask, frame.b2ip(opts[1])
    assert opts[3] == s.host_ip, 'router %s' % frame.b2ip(opts[3])
    assert opts[6] == s.dns_ip, 'dns %s' % frame.b2ip(opts[6])
    log('offer: ip %s mask %s router %s dns %s'
        % (frame.b2ip(yiaddr), frame.b2ip(opts[1]), frame.b2ip(opts[3]), frame.b2ip(opts[6])))
    yiaddr, opts = dhcp_request(g, 3)                    # REQUEST
    assert opts[53] == b'\x05', 'expected an ACK, got type %r' % opts[53]
    assert yiaddr == s.guest_ip


@test('ICMP echo to the gateway is answered')
def t_icmp(s, g):
    msg = bytearray(struct.pack('!BBHHH', 8, 0, 0, 0x4242, 1) + b'ping-payload')
    struct.pack_into('!H', msg, 2, frame.checksum(msg))
    g.tx_ip(s.host_ip, frame.IPPROTO_ICMP, bytes(msg))

    def is_reply(e, p):
        if e != frame.ETH_P_IP:
            return False
        ip = frame.parse_ip4(p)
        return ip and ip['proto'] == frame.IPPROTO_ICMP and ip['payload'][0] == 0

    etype, payload = g.wait_frame(is_reply, 'an ICMP echo reply')
    ip = frame.parse_ip4(payload)
    reply = ip['payload']
    assert frame.checksum(reply) == 0, 'bad ICMP checksum'
    assert struct.unpack_from('!HH', reply, 4) == (0x4242, 1), 'id/seq mismatch'
    assert reply[8:] == b'ping-payload'
    assert ip['src'] == s.host_ip
    log('echo reply from %s, payload intact' % frame.b2ip(ip['src']))


@test('TCP out: the gateway address reaches the host')
def t_tcp_out(s, g):
    srv = EchoServer()
    c = g.connect(s.host_ip, srv.port)
    c.write(b'hello from the guest')
    g.wait(lambda: c.data == b'hello from the guest', 'the echo')
    c.close()
    g.wait(lambda: c.fin, 'the host side to close')
    log('echoed %d bytes, both sides closed' % len(c.data))


@test('TCP out: a refused connection becomes a RST')
def t_tcp_refused(s, g):
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()                                       # nothing listens there now
    c = GuestTcp(g, 45001, s.host_ip, port)
    g.conns[c.key()] = c
    c._send(TH_SYN, options=frame.mss_option(1460))
    g.wait(lambda: c.reset or c.established, 'a RST')
    assert c.reset and not c.established, 'expected a reset, not a connection'
    log('port %d refused, guest got a RST' % port)


@test('TCP out: 256 KiB transfer respects the window')
def t_tcp_bulk(s, g):
    blob = bytes(random.getrandbits(8) for _ in range(256 << 10))

    def serve(conn):
        conn.sendall(blob)
        conn.shutdown(socket.SHUT_WR)
        while conn.recv(65536):
            pass

    srv = EchoServer(transform=serve)
    c = g.connect(s.host_ip, srv.port, window=16384)     # small window on purpose
    g.wait(lambda: len(c.data) >= len(blob), 'the bulk transfer', timeout=60)
    assert bytes(c.data) == blob, 'payload corrupted (%d bytes)' % len(c.data)
    g.wait(lambda: c.fin, 'the closing FIN')
    log('%d KiB arrived intact through a 16 KiB window' % (len(c.data) >> 10))


@test('TCP out: the guest can push 256 KiB to the host')
def t_tcp_upload(s, g):
    blob = bytes(random.getrandbits(8) for _ in range(256 << 10))
    srv = EchoServer()
    c = g.connect(s.host_ip, srv.port)
    c.write(blob)
    g.wait(lambda: len(c.data) >= len(blob), 'the echo of the upload', timeout=60)
    assert bytes(c.data) == blob, 'payload corrupted'
    log('%d KiB uploaded and echoed back intact' % (len(blob) >> 10))


@test('UDP out reaches a host service and the reply comes back')
def t_udp(s, g):
    srv = UdpEcho()
    g.tx_udp(s.host_ip, 51000, srv.port, b'datagram')

    def is_reply(e, p):
        if e != frame.ETH_P_IP:
            return False
        ip = frame.parse_ip4(p)
        if not ip or ip['proto'] != frame.IPPROTO_UDP:
            return False
        u = frame.parse_udp(ip['payload'])
        return u and u[1] == 51000 and u[2].startswith(b'echo:')

    etype, payload = g.wait_frame(is_reply, 'the UDP reply')
    ip = frame.parse_ip4(payload)
    sport, dport, data = frame.parse_udp(ip['payload'])
    assert data == b'echo:datagram', data
    assert frame.l4_checksum(ip['src'], ip['dst'], frame.IPPROTO_UDP, ip['payload']) == 0
    assert sport == srv.port and ip['src'] == s.host_ip
    log('reply %r from %s:%d, checksum valid' % (data, frame.b2ip(ip['src']), sport))


@test('hostfwd: the host connects into the guest')
def t_hostfwd(s, g):
    srv = g.server(2222)                                # the guest "listens" on 2222
    result = {}

    def client():
        try:
            c = socket.create_connection(('127.0.0.1', s.fwd_port), timeout=10)
            with c:
                c.sendall(b'knock knock')
                c.shutdown(socket.SHUT_WR)
                data = b''
                while True:
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                result['data'] = data
        except OSError as e:
            result['error'] = e

    th = threading.Thread(target=client, daemon=True)
    th.start()
    g.wait(lambda: srv.established, 'the forwarded connection to reach the guest')
    g.wait(lambda: srv.data == b'knock knock', 'the data the host sent')
    srv.write(b'who is there')
    srv.close()
    g.wait(lambda: result or srv.fin, 'the host client to finish')
    th.join(timeout=5)
    assert 'error' not in result, result['error']
    assert result.get('data') == b'who is there', result
    log('host -> guest on forwarded port %d, reply %r' % (s.fwd_port, result['data']))


@test('DNS over TCP is forwarded to the real resolver')
def t_tcp_dns(s, g):
    # Resolvers fall back to TCP when a reply will not fit in a datagram.  Point
    # the stack at a local stand-in resolver and check a TCP connection to the
    # virtual DNS address reaches it, rather than the literal 10.0.2.3.
    srv = EchoServer()
    r = Slirp(dnsserver='127.0.0.1')
    r.resolvers = ['127.0.0.1']
    r.dns_ip = frame.ip2b('10.0.2.3')
    r.start()
    rg = FakeGuest(r)
    try:
        # the stand-in listens on an ephemeral port, so aim port 53 at it
        real = r._l4_target
        r._l4_target = lambda dst, dport: (('127.0.0.1', srv.port)
                                           if dst == r.dns_ip and dport == 53
                                           else real(dst, dport))
        c = GuestTcp(rg, 45003, r.dns_ip, 53)
        rg.conns[c.key()] = c
        c.connect()
        c.write(b'\x00\x1dquery-over-tcp')
        rg.wait(lambda: c.data.endswith(b'query-over-tcp'), 'the forwarded TCP query')
        log('TCP :53 to %s reached the resolver and echoed back'
            % frame.b2ip(r.dns_ip))
    finally:
        r.close()


@test('restrict=on blocks the outside but not DHCP')
def t_restrict(s, g):
    r = Slirp(restrict=True)
    r.start()
    rg = FakeGuest(r)
    try:
        srv = EchoServer()
        c = GuestTcp(rg, 45002, r.host_ip, srv.port)
        rg.conns[c.key()] = c
        c._send(TH_SYN, options=frame.mss_option(1460))
        rg.wait(lambda: c.reset or c.established, 'a response to the blocked SYN')
        assert c.reset and not c.established, 'restrict=on let a connection through'
        yiaddr, opts = dhcp_request(rg, 1)
        assert yiaddr == r.guest_ip, 'DHCP should still work under restrict=on'
        log('outbound TCP reset, DHCP still served')
    finally:
        r.close()


def main():
    global VERBOSE
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('-v', '--verbose', action='store_true')
    a = p.parse_args()
    VERBOSE = a.verbose

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    fwd_port = sock.getsockname()[1]
    sock.close()

    s = Slirp(hostfwd=['tcp:127.0.0.1:%d-:2222' % fwd_port])
    s.fwd_port = fwd_port
    s.start()
    print('pyvm user-mode network stack: %s' % s.describe())
    print('  forwarding host 127.0.0.1:%d -> guest :2222\n' % fwd_port)

    failed = 0
    try:
        for name, fn in TESTS:
            g = FakeGuest(s)
            t0 = time.monotonic()
            try:
                fn(s, g)
                print('  [PASS] %-52s %5.2fs' % (name, time.monotonic() - t0))
            except Exception as e:
                failed += 1
                print('  [FAIL] %-52s %5.2fs' % (name, time.monotonic() - t0))
                print('         %s: %s' % (type(e).__name__, e))
                if VERBOSE:
                    import traceback
                    traceback.print_exc()
    finally:
        s.close()
    print('\n  %d/%d passed, %d stack errors, %d frames dropped'
          % (len(TESTS) - failed, len(TESTS), s.errors, s.dropped))
    print('  RESULT: %s' % ('PASS' if not failed and not s.errors else 'FAIL'))
    return 1 if failed or s.errors else 0


if __name__ == '__main__':
    sys.exit(main())
