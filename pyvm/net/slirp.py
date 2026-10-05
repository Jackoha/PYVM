"""User-mode networking: a NAT gateway for the guest, written in Python.

The guest sees an ordinary Ethernet segment with a router on it:

    10.0.2.0/24    the virtual network
    10.0.2.2       gateway; also stands for "the host" (mapped to 127.0.0.1)
    10.0.2.3       DNS forwarder -> the host's own resolvers
    10.0.2.15      the guest, handed out by the built-in DHCP server

Guest TCP connections and UDP datagrams are translated into ordinary host
sockets, so networking needs no privileges and no host configuration at all.
Connections *into* the guest are made with port forwarding (`hostfwd=`).

Threading: a background thread owns the host sockets and does the select()
loop; the emulator thread hands guest frames to `send()` and collects frames
for the guest with `recv()`.  Everything that touches the stack's state runs
under `self.lock`.
"""

import collections
import errno
import os
import random
import select
import socket
import struct
import sys
import threading
import time

from ..errors import NetError
from . import frame
from .frame import (TH_ACK, TH_FIN, TH_PSH, TH_RST, TH_SYN, IPPROTO_ICMP,
                    IPPROTO_TCP, IPPROTO_UDP)

MTU = 1500
MSS = MTU - 40
SND_BUF = 64 << 10          # host -> guest bytes buffered per connection
RCV_BUF = 64 << 10          # guest -> host bytes buffered per connection
RTO_MIN, RTO_MAX, RTX_MAX = 0.25, 8.0, 8
TIME_WAIT = 5.0
UDP_IDLE, DNS_IDLE, ICMP_IDLE = 120.0, 20.0, 30.0
FRAG_TIMEOUT = 10.0
RX_LIMIT = 512              # frames queued for the guest before we drop

CONNECTING, SYN_SENT, SYN_RCVD, ESTABLISHED, DEAD = 0, 1, 2, 3, 4

DHCP_MAGIC = b'\x63\x82\x53\x63'


def host_resolvers():
    """The host's own IPv4 nameservers, from /etc/resolv.conf."""
    out = []
    try:
        with open('/etc/resolv.conf') as f:
            for line in f:
                w = line.split()
                if len(w) >= 2 and w[0] == 'nameserver' and ':' not in w[1]:
                    out.append(w[1])
    except OSError:
        pass
    return out or ['8.8.8.8', '1.1.1.1']


# ====================================================================== TCP
class _Tcp:
    """One guest TCP connection, proxied to a host socket.

    Sequence numbers are kept as byte offsets from the initial sequence number
    (offset 0 is the SYN, data starts at 1), so no wrap-around arithmetic is
    needed anywhere below."""

    def __init__(self, s, key, sock, state):
        self.s = s
        self.key = key                  # (guest_ip, guest_port, remote_ip, remote_port)
        self.sock = sock
        self.state = state
        self.iss = random.getrandbits(32)
        self.irs = 0
        self.snd_una = 0                # oldest unacknowledged offset
        self.snd_nxt = 0                # next offset to send
        self.snd_wnd = 0                # what the guest will accept
        self.snd_buf = bytearray()      # host data from offset max(snd_una, 1) on
        self.acked_data = 0             # data bytes the guest has acknowledged
        self.rcv_nxt = 0                # next offset expected from the guest
        self.to_host = bytearray()      # guest data not yet written to the socket
        self.mss = MSS
        self.host_eof = False
        self.fin_sent = False
        self.fin_off = None
        self.guest_fin = False
        self.shut_done = False
        self.rto = RTO_MIN
        self.rtx_at = None
        self.tries = 0
        self.expire = None
        self.last_wnd = 0

    # ---------------------------------------------------------- helpers
    @property
    def sent_data(self):
        return max(0, self.snd_nxt - 1)

    def window(self):
        return max(0, min(0xFFFF, RCV_BUF - len(self.to_host)))

    def emit(self, flags, data=b'', seq=None, options=b''):
        gip, gport, rip, rport = self.key
        off = self.snd_nxt if seq is None else seq
        ack = (self.irs + self.rcv_nxt) & frame.M32 if flags & TH_ACK else 0
        self.last_wnd = self.window()
        seg = frame.build_tcp(rip, gip, rport, gport, self.iss + off, ack,
                             flags, self.last_wnd, data, options)
        self.s.tx_ip(rip, gip, IPPROTO_TCP, seg)

    def arm(self):
        if self.rtx_at is None:
            self.rtx_at = time.monotonic() + self.rto

    def ack(self):
        self.emit(TH_ACK)

    # ---------------------------------------------------------- sending
    def push(self):
        if self.state != ESTABLISHED:
            return
        idx = self.sent_data - self.acked_data
        while idx < len(self.snd_buf):
            room = self.snd_wnd - (self.snd_nxt - self.snd_una)
            if room <= 0:
                break
            n = min(len(self.snd_buf) - idx, self.mss, room)
            self.emit(TH_ACK | TH_PSH, bytes(self.snd_buf[idx:idx + n]))
            self.snd_nxt += n
            idx += n
            self.arm()
        if self.host_eof and not self.fin_sent and idx >= len(self.snd_buf):
            self.fin_off = self.snd_nxt
            self.emit(TH_ACK | TH_FIN)
            self.snd_nxt += 1
            self.fin_sent = True
            self.arm()

    def flush_to_host(self):
        if self.sock is None:
            return
        while self.to_host:
            try:
                n = self.sock.send(self.to_host)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                self.fail()
                return
            if n <= 0:
                break
            del self.to_host[:n]
        if self.guest_fin and not self.to_host and not self.shut_done:
            self.shut_done = True
            try:
                self.sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    # ---------------------------------------------------------- guest -> us
    def on_segment(self, seq, ack, flags, win, data, options):
        if flags & TH_RST:
            self.s.forget_tcp(self)
            return
        if self.state == SYN_SENT:                      # we opened towards the guest
            if flags & (TH_SYN | TH_ACK) != (TH_SYN | TH_ACK):
                return
            if frame.rel(self.iss, ack, 0) != 1:
                return
            self.irs = seq
            self.rcv_nxt = 1
            self.snd_una = self.snd_nxt = 1
            self.snd_wnd = win
            self.mss = min(self.mss, frame.option_mss(options) or self.mss)
            self.state = ESTABLISHED
            self.rtx_at, self.tries, self.rto = None, 0, RTO_MIN
            self.ack()
            return
        if self.state in (CONNECTING, DEAD):
            return

        if flags & TH_ACK:
            aoff = frame.rel(self.iss, ack, self.snd_una)
            if self.snd_una < aoff <= self.snd_nxt:
                self.snd_una = aoff
                want = min(max(0, aoff - 1), self.acked_data + len(self.snd_buf))
                if want > self.acked_data:
                    del self.snd_buf[:want - self.acked_data]
                    self.acked_data = want
                self.tries, self.rto = 0, RTO_MIN
                self.rtx_at = time.monotonic() + self.rto \
                    if self.snd_una < self.snd_nxt else None
            self.snd_wnd = win
            if self.state == SYN_RCVD and self.snd_una >= 1:
                self.state = ESTABLISHED

        soff = frame.rel(self.irs, seq, self.rcv_nxt)
        if data:
            if soff <= self.rcv_nxt < soff + len(data):
                chunk = data[self.rcv_nxt - soff:]
                self.to_host += chunk
                self.rcv_nxt += len(chunk)
                self.flush_to_host()
            self.ack()                                  # also re-ACKs out-of-order data
        if flags & TH_FIN:
            if soff + len(data) == self.rcv_nxt and not self.guest_fin:
                self.guest_fin = True
                self.rcv_nxt += 1
                self.flush_to_host()
            self.ack()
        self.push()
        self.check_done()

    # ---------------------------------------------------------- host -> us
    def selectors(self):
        """(want_read, want_write) for the host socket."""
        if self.state == CONNECTING:
            return False, True
        if self.state in (SYN_SENT, DEAD):
            return False, False
        return (not self.host_eof and len(self.snd_buf) < SND_BUF), bool(self.to_host)

    def on_readable(self):
        try:
            data = self.sock.recv(min(1 << 16, SND_BUF - len(self.snd_buf)))
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.fail()
            return
        if data:
            self.snd_buf += data
        else:
            self.host_eof = True
        self.push()
        self.check_done()

    def on_writable(self):
        if self.state == CONNECTING:
            err = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err:
                self.reject()
                return
            try:
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            self.state = SYN_RCVD
            self.emit(TH_SYN | TH_ACK, seq=0, options=frame.mss_option(MSS))
            self.snd_nxt = 1
            self.arm()
            return
        before = len(self.to_host)
        self.flush_to_host()
        if len(self.to_host) < before and self.window() - self.last_wnd >= self.mss:
            self.ack()                                  # window update
        self.check_done()

    # ---------------------------------------------------------- timers / teardown
    def deadline(self):
        d = [t for t in (self.rtx_at, self.expire) if t is not None]
        return min(d) if d else None

    def tick(self, now):
        if self.expire is not None and now >= self.expire:
            self.s.forget_tcp(self)
            return
        if self.rtx_at is None or now < self.rtx_at:
            return
        # A shut receive window is probed for as long as it stays shut, the way
        # TCP's persist timer does; everything else gives up eventually.
        probing = self.state == ESTABLISHED and self.snd_wnd == 0
        if self.tries >= RTX_MAX and not probing:
            self.fail()
            return
        self.tries += 1
        self.rto = min(self.rto * 2, RTO_MAX)
        self.rtx_at = now + self.rto
        if self.state == SYN_SENT:
            self.emit(TH_SYN, seq=0, options=frame.mss_option(MSS))
        elif self.state == SYN_RCVD:
            self.emit(TH_SYN | TH_ACK, seq=0, options=frame.mss_option(MSS))
        else:
            self.snd_nxt = self.snd_una                 # go back and resend
            self.fin_sent = False
            self.push()
            if self.snd_nxt == self.snd_una:
                self.ack()                              # zero window: probe

    def check_done(self):
        """Both directions closed and our FIN acknowledged: linger, then drop."""
        if self.expire is None and self.guest_fin and self.fin_sent \
                and self.snd_una > self.fin_off:
            self.expire = time.monotonic() + TIME_WAIT
            self.rtx_at = None
            self.s.close_sock(self)

    def reject(self):
        """The host refused the connection: tell the guest with a RST."""
        self.emit(TH_RST | TH_ACK, seq=0)
        self.s.forget_tcp(self)

    def fail(self):
        if self.state in (ESTABLISHED, SYN_RCVD):
            self.emit(TH_RST | TH_ACK)
        self.s.forget_tcp(self)


# ====================================================================== UDP
class _Udp:
    """A guest UDP flow bound to a host socket."""

    def __init__(self, s, key, sock, target, idle=UDP_IDLE):
        self.s = s
        self.key = key
        self.sock = sock
        self.target = target
        self.idle = idle
        self.expire = time.monotonic() + idle

    def selectors(self):
        return True, False

    def send(self, data):
        self.expire = time.monotonic() + self.idle
        try:
            self.sock.send(data)
        except OSError:
            pass

    def on_readable(self):
        gip, gport, rip, rport = self.key
        for _ in range(64):
            try:
                data = self.sock.recv(1 << 16)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            self.expire = time.monotonic() + self.idle
            self.s.tx_ip(rip, gip, IPPROTO_UDP,
                         frame.build_udp(rip, gip, rport, gport, data))

    def deadline(self):
        return self.expire

    def tick(self, now):
        if now >= self.expire:
            self.s.forget_udp(self)

    def on_writable(self):
        pass


class _UdpReverse:
    """A forwarded flow that came *from* the host: replies go back to its peer."""

    def __init__(self, s, key, sock, peer, idle=UDP_IDLE):
        self.s = s
        self.key = key
        self.sock = sock                # the listening socket, shared
        self.peer = peer
        self.idle = idle
        self.expire = time.monotonic() + idle

    def selectors(self):
        return False, False             # the listener does the reading

    def send(self, data):
        self.expire = time.monotonic() + self.idle
        try:
            self.sock.sendto(data, self.peer)
        except OSError:
            pass

    def deadline(self):
        return self.expire

    def tick(self, now):
        if now >= self.expire:
            self.s.forget_udp(self)

    def on_readable(self):
        pass

    def on_writable(self):
        pass


# ====================================================================== ICMP
class _Icmp:
    """Echo requests for the outside world, through a Linux ping socket."""

    def __init__(self, s, key, sock):
        self.s = s
        self.key = key                  # (guest_ip, ident, remote_ip)
        self.sock = sock
        self.expire = time.monotonic() + ICMP_IDLE

    def selectors(self):
        return True, False

    def send(self, msg):
        self.expire = time.monotonic() + ICMP_IDLE
        try:
            self.sock.send(msg)
        except OSError:
            pass

    def on_readable(self):
        gip, ident, rip = self.key
        for _ in range(16):
            try:
                data = self.sock.recv(1 << 16)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if len(data) < 8:
                continue
            # the kernel replaced the echo id with the socket's port: put ours back
            msg = bytearray(data)
            struct.pack_into('!HH', msg, 2, 0, ident)
            struct.pack_into('!H', msg, 2, frame.checksum(msg))
            self.s.tx_ip(rip, gip, IPPROTO_ICMP, bytes(msg))

    def deadline(self):
        return self.expire

    def tick(self, now):
        if now >= self.expire:
            self.s.forget_icmp(self)

    def on_writable(self):
        pass


# ====================================================================== forwarding
class _TcpListener:
    def __init__(self, s, sock, gport):
        self.s = s
        self.sock = sock
        self.gport = gport

    def selectors(self):
        return True, False

    def on_readable(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        self.s.open_to_guest(conn, self.gport)

    def on_writable(self):
        pass

    def deadline(self):
        return None

    def tick(self, now):
        pass


class _UdpListener:
    def __init__(self, s, sock, gport):
        self.s = s
        self.sock = sock
        self.gport = gport

    def selectors(self):
        return True, False

    def on_readable(self):
        for _ in range(64):
            try:
                data, peer = self.sock.recvfrom(1 << 16)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            self.s.udp_to_guest(self.sock, peer, self.gport, data)

    def on_writable(self):
        pass

    def deadline(self):
        return None

    def tick(self, now):
        pass


# ====================================================================== the stack
class Slirp:
    """User-mode network backend.  `mac` is the address the guest NIC gets."""

    kind = 'user'

    def __init__(self, network='10.0.2.0/24', host=None, dns=None, guest=None,
                 mac='52:54:00:12:34:56', mtu=MTU, hostfwd=(), dnsserver=None,
                 restrict=False, debug=False):
        self.net, self.mask = frame.parse_cidr(network)
        base = int.from_bytes(self.net, 'big')
        self.host_ip = frame.ip2b(host) if host else (base + 2).to_bytes(4, 'big')
        self.dns_ip = frame.ip2b(dns) if dns else (base + 3).to_bytes(4, 'big')
        self.guest_ip = frame.ip2b(guest) if guest else (base + 15).to_bytes(4, 'big')
        self.bcast_ip = frame.broadcast_of(self.net, self.mask)
        self.mac = frame.mac2b(mac)
        self.guest_mac = self.mac
        self.mtu = int(mtu)
        self.restrict = bool(restrict)
        self.debug = bool(debug)
        self.resolvers = [dnsserver] if dnsserver else host_resolvers()

        self.rx = collections.deque()
        self.lock = threading.RLock()
        self.tcp = {}
        self.udp = {}
        self.icmp = {}
        self.listeners = []
        self.frags = {}
        self.dead = []
        self.errors = 0
        self.dropped = 0
        self.ident = 0
        self._dns_rr = 0
        self._no_ping_until = 0.0
        self._ping_noted = False
        self.wake = None
        self.stop = False
        self.thread = None
        self._wake_r = self._wake_w = -1
        self._fwd = list(hostfwd)

    # ---------------------------------------------------------- lifecycle
    def start(self, wake=None):
        self.wake = wake
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        for spec in self._fwd:
            self._listen(spec)
        self.thread = threading.Thread(target=self._io_loop, name='pyvm-net', daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop = True
        self._nudge()
        if self.thread is not None:
            self.thread.join(timeout=1.0)
            self.thread = None
        with self.lock:
            for table in (self.tcp, self.udp, self.icmp):
                for c in list(table.values()):
                    self.close_sock(c)
                table.clear()
            for l in self.listeners:
                self.close_sock(l)
            self.listeners = []
            self._reap()
        for fd in (self._wake_r, self._wake_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._wake_r = self._wake_w = -1

    def _nudge(self):
        if self._wake_w >= 0:
            try:
                os.write(self._wake_w, b'\0')
            except (OSError, ValueError):
                pass

    def note(self, msg):
        sys.stderr.write('pyvm: net: %s\r\n' % msg)

    # ---------------------------------------------------------- device interface
    def send(self, f):
        """A frame the guest transmitted (called on the emulator thread)."""
        with self.lock:
            try:
                self._from_guest(f)
            except Exception as e:                      # a bad guest packet must not kill the VM
                self.errors += 1
                if self.debug:
                    import traceback
                    traceback.print_exc()
                    self.note('dropped a guest frame: %s' % e)
        self._nudge()

    def recv(self):
        """A frame for the guest, or None (called on the emulator thread)."""
        try:
            return self.rx.popleft()
        except IndexError:
            return None

    # ---------------------------------------------------------- towards the guest
    def tx_eth(self, src_mac, dst_mac, etype, payload):
        if len(self.rx) >= RX_LIMIT:
            self.dropped += 1
            return
        self.rx.append(frame.eth(dst_mac, src_mac, etype, payload))

    def tx_ip(self, src, dst, proto, payload, dst_mac=None):
        self.ident = (self.ident + 1) & 0xFFFF
        for pkt in frame.build_ip4(src, dst, proto, payload, ident=self.ident, mtu=self.mtu):
            self.tx_eth(self.mac_for(src), dst_mac or self.guest_mac, frame.ETH_P_IP, pkt)

    def mac_for(self, ip):
        """A stable made-up MAC for one of our virtual addresses."""
        return b'\x52\x55' + ip

    # ---------------------------------------------------------- from the guest
    def _from_guest(self, f):
        p = frame.parse_eth(f)
        if p is None:
            return
        dst_mac, src_mac, etype, payload = p
        if not src_mac[0] & 1:
            self.guest_mac = src_mac
        if etype == frame.ETH_P_ARP:
            self._arp(payload)
        elif etype == frame.ETH_P_IP:
            self._ip4(payload)

    def _arp(self, p):
        a = frame.parse_arp(p)
        if a is None:
            return
        op, sha, spa, tpa = a
        if op != 1 or not self._is_ours(tpa):
            return
        tha = self.mac_for(tpa)
        self.tx_eth(tha, sha, frame.ETH_P_ARP, frame.arp(2, tha, tpa, sha, spa))

    def _is_ours(self, ip):
        """Do we answer ARP for this address?"""
        if ip in (self.guest_ip, frame.ANY_IP, self.bcast_ip):
            return False
        return ip in (self.host_ip, self.dns_ip) or frame.same_net(ip, self.net, self.mask)

    def _ip4(self, p):
        ip = frame.parse_ip4(p)
        if ip is None:
            return
        if ip['mf'] or ip['off']:
            ip = self._reassemble(ip)
            if ip is None:
                return
        src, dst, proto, seg = ip['src'], ip['dst'], ip['proto'], ip['payload']
        if proto == IPPROTO_UDP:
            self._udp(src, dst, seg)
        elif proto == IPPROTO_TCP:
            self._tcp(src, dst, seg)
        elif proto == IPPROTO_ICMP:
            self._icmp(src, dst, seg)

    def _reassemble(self, ip):
        key = (ip['src'], ip['dst'], ip['ident'], ip['proto'])
        now = time.monotonic()
        for k, v in list(self.frags.items()):
            if now - v[0] > FRAG_TIMEOUT:
                del self.frags[k]
        slot = self.frags.setdefault(key, [now, {}, None])
        slot[1][ip['off']] = bytes(ip['payload'])
        if not ip['mf']:
            slot[2] = ip['off'] + len(ip['payload'])
        if slot[2] is None:
            return None
        out = bytearray()
        for off in sorted(slot[1]):
            if off != len(out):
                return None                             # still a hole
            out += slot[1][off]
        if len(out) != slot[2]:
            return None
        del self.frags[key]
        ip['payload'] = bytes(out)
        return ip

    # ---------------------------------------------------------- host targets
    def _target(self, ip):
        """Where a guest packet for `ip` really goes on the host."""
        if ip == self.host_ip:
            return '127.0.0.1'                          # 10.0.2.2 means "the host"
        return frame.b2ip(ip)

    def _l4_target(self, dst, dport):
        """Where a guest TCP/UDP flow to dst:dport really goes, or None to refuse.

        The virtual DNS address is a stand-in for the host's own resolvers, and
        that has to hold for TCP as well as UDP: resolvers fall back to TCP when
        a reply does not fit in a datagram, and connecting to the literal
        10.0.2.3 would just hang."""
        if dst == self.dns_ip:
            if dport != 53:
                return None
            self._dns_rr += 1
            return (self.resolvers[self._dns_rr % len(self.resolvers)], 53)
        if not self._reachable(dst):
            return None
        return (self._target(dst), dport)

    def _reachable(self, ip):
        if ip in (self.bcast_ip, frame.BCAST_IP, frame.ANY_IP) or ip[0] >= 224:
            return False
        return not self.restrict

    # ---------------------------------------------------------- UDP
    def _udp(self, src, dst, seg):
        u = frame.parse_udp(seg)
        if u is None:
            return
        sport, dport, data = u
        if dport == 67 and sport == 68:
            return self._dhcp(data)
        key = (src, sport, dst, dport)
        c = self.udp.get(key)
        if c is not None:
            return c.send(data)
        target = self._l4_target(dst, dport)
        if target is None:
            return
        idle = DNS_IDLE if dst == self.dns_ip else UDP_IDLE
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            sock.connect(target)
        except OSError:
            return
        self.udp[key] = c = _Udp(self, key, sock, target, idle)
        c.send(data)

    def udp_to_guest(self, listener, peer, gport, data):
        """A datagram arrived on a forwarded host port: hand it to the guest."""
        key = None
        for k, c in self.udp.items():
            if isinstance(c, _UdpReverse) and c.peer == peer and k[1] == gport:
                key = k
                break
        if key is None:
            port = self._free_port(gport, self.udp)
            if port is None:
                return
            key = (self.guest_ip, gport, self.host_ip, port)
            self.udp[key] = _UdpReverse(self, key, listener, peer)
        c = self.udp[key]
        c.expire = time.monotonic() + c.idle
        gip, gp, rip, rport = key
        self.tx_ip(rip, gip, IPPROTO_UDP, frame.build_udp(rip, gip, rport, gp, data))

    # ---------------------------------------------------------- ICMP
    def _icmp(self, src, dst, msg):
        if len(msg) < 8:
            return
        if msg[0] != frame.ICMP_ECHO:
            return
        if dst in (self.host_ip, self.dns_ip) or dst == self.bcast_ip:
            reply = b'\0' + msg[1:2] + b'\0\0' + msg[4:]
            reply = reply[:2] + struct.pack('!H', frame.checksum(reply)) + reply[4:]
            src_ip = self.host_ip if dst == self.bcast_ip else dst
            return self.tx_ip(src_ip, src, IPPROTO_ICMP, reply)
        if not self._reachable(dst) or time.monotonic() < self._no_ping_until:
            return
        ident = struct.unpack_from('!H', msg, 4)[0]
        key = (src, ident, dst)
        c = self.icmp.get(key)
        if c is None:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, IPPROTO_ICMP)
                sock.setblocking(False)
                sock.connect((self._target(dst), 0))
            except OSError as e:
                if e.errno in (errno.EACCES, errno.EPERM, errno.EPROTONOSUPPORT):
                    # Retry later rather than giving up for good, so raising the
                    # host's limit takes effect without restarting the VM.
                    self._no_ping_until = time.monotonic() + 10.0
                    if not self._ping_noted:
                        self._ping_noted = True
                        self.note('ping to the outside needs an ICMP socket, which '
                                  'this user may not open (%s).' % e.strerror)
                        self.note('on the host, once:  sudo sysctl -w '
                                  'net.ipv4.ping_group_range="0 %d"' % os.getgid())
                        self.note('TCP, UDP and DNS are unaffected; pings to %s '
                                  'are answered here.' % frame.b2ip(self.host_ip))
                return
            self.icmp[key] = c = _Icmp(self, key, sock)
        c.send(bytes(msg))

    # ---------------------------------------------------------- TCP
    def _tcp(self, src, dst, seg):
        t = frame.parse_tcp(seg)
        if t is None:
            return
        sport, dport, seq, ack, flags, win, data, options = t
        key = (src, sport, dst, dport)
        c = self.tcp.get(key)
        if c is not None:
            return c.on_segment(seq, ack, flags, win, data, options)
        if flags & TH_RST:
            return
        if (flags & (TH_SYN | TH_ACK)) != TH_SYN:
            return self._rst(key, seq, ack, flags, len(data))
        target = self._l4_target(dst, dport)
        if target is None:
            return self._rst(key, seq, ack, flags, len(data))
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)
            err = sock.connect_ex(target)
        except OSError:
            return self._rst(key, seq, ack, flags, len(data))
        if err not in (0, errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK):
            sock.close()
            return self._rst(key, seq, ack, flags, len(data))
        c = _Tcp(self, key, sock, CONNECTING)
        c.irs = seq
        c.rcv_nxt = 1
        c.snd_wnd = win
        c.mss = min(MSS, frame.option_mss(options) or MSS)
        self.tcp[key] = c
        if err == 0:
            c.on_writable()

    def _rst(self, key, seq, ack, flags, dlen):
        """Reset a segment that belongs to no connection (RFC 793 p.65)."""
        gip, gport, rip, rport = key
        if flags & TH_ACK:
            seg = frame.build_tcp(rip, gip, rport, gport, ack, 0, TH_RST, 0)
        else:
            nxt = (seq + dlen + (1 if flags & (TH_SYN | TH_FIN) else 0)) & frame.M32
            seg = frame.build_tcp(rip, gip, rport, gport, 0, nxt, TH_RST | TH_ACK, 0)
        self.tx_ip(rip, gip, IPPROTO_TCP, seg)

    def open_to_guest(self, sock, gport):
        """A host connection arrived on a forwarded port: open one to the guest."""
        port = self._free_port(gport, self.tcp)
        if port is None:
            sock.close()
            return
        try:
            sock.setblocking(False)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            sock.close()
            return
        key = (self.guest_ip, gport, self.host_ip, port)
        self.tcp[key] = c = _Tcp(self, key, sock, SYN_SENT)
        c.emit(TH_SYN, seq=0, options=frame.mss_option(MSS))
        c.snd_nxt = 1
        c.arm()

    def _free_port(self, gport, table):
        for _ in range(2000):
            p = random.randint(32768, 60999)
            if (self.guest_ip, gport, self.host_ip, p) not in table:
                return p
        return None

    # ---------------------------------------------------------- DHCP
    def _dhcp(self, data):
        if len(data) < 240 or data[236:240] != DHCP_MAGIC or data[0] != 1:
            return
        xid, _, flags = struct.unpack_from('!IHH', data, 4)
        chaddr = bytes(data[28:34])
        opts, i = {}, 240
        while i < len(data):
            t = data[i]
            if t == 255:
                break
            if t == 0:
                i += 1
                continue
            if i + 1 >= len(data):
                break
            ln = data[i + 1]
            opts[t] = bytes(data[i + 2:i + 2 + ln])
            i += 2 + ln
        mtype = opts.get(53, b'\0')[0]
        if mtype == 1:
            reply = 2                                   # DISCOVER -> OFFER
        elif mtype in (3, 8):
            reply = 5                                   # REQUEST / INFORM -> ACK
        else:
            return
        o = bytes([53, 1, reply, 54, 4]) + self.host_ip
        o += bytes([51, 4]) + struct.pack('!I', 86400)
        o += bytes([1, 4]) + self.mask
        o += bytes([3, 4]) + self.host_ip
        o += bytes([6, 4]) + self.dns_ip
        o += bytes([28, 4]) + self.bcast_ip
        o += bytes([26, 2]) + struct.pack('!H', self.mtu)
        o += b'\xff'
        pkt = struct.pack('!BBBBIHH', 2, 1, 6, 0, xid, 0, flags)
        pkt += frame.ANY_IP + self.guest_ip + self.host_ip + frame.ANY_IP
        pkt += chaddr + bytes(10) + bytes(64) + bytes(128) + DHCP_MAGIC + o
        pkt += bytes(max(0, 300 - len(pkt)))
        dst_mac = frame.BCAST_MAC if flags & 0x8000 else chaddr
        self.tx_ip(self.host_ip, frame.BCAST_IP, IPPROTO_UDP,
                   frame.build_udp(self.host_ip, frame.BCAST_IP, 67, 68, pkt),
                   dst_mac=dst_mac)

    # ---------------------------------------------------------- port forwarding
    def _listen(self, spec):
        """hostfwd spec: tcp|udp:[hostaddr]:hostport-[guestaddr]:guestport"""
        proto, _, rest = spec.partition(':')
        proto = proto.lower() or 'tcp'
        if proto not in ('tcp', 'udp') or '-' not in rest:
            raise NetError('bad hostfwd %r (want tcp::2222-:22)' % spec)
        hostpart, _, guestpart = rest.rpartition('-')
        haddr, _, hport = hostpart.rpartition(':')
        gaddr, _, gport = guestpart.rpartition(':')
        if not hport.isdigit() or not gport.isdigit():
            raise NetError('bad hostfwd %r (want tcp::2222-:22)' % spec)
        if gaddr and frame.ip2b(gaddr) != self.guest_ip:
            raise NetError('hostfwd %r: the guest is %s' % (spec, frame.b2ip(self.guest_ip)))
        kind = socket.SOCK_STREAM if proto == 'tcp' else socket.SOCK_DGRAM
        s = socket.socket(socket.AF_INET, kind)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setblocking(False)
        try:
            s.bind((haddr or '127.0.0.1', int(hport)))
            if proto == 'tcp':
                s.listen(8)
        except OSError as e:
            s.close()
            raise NetError('hostfwd %s: cannot listen on %s:%s (%s)'
                           % (spec, haddr or '127.0.0.1', hport, e.strerror))
        cls = _TcpListener if proto == 'tcp' else _UdpListener
        self.listeners.append(cls(self, s, int(gport)))

    # ---------------------------------------------------------- bookkeeping
    def close_sock(self, c):
        if c.sock is not None:
            self.dead.append(c.sock)
            c.sock = None

    def forget_tcp(self, c):
        c.state = DEAD
        self.tcp.pop(c.key, None)
        self.close_sock(c)

    def forget_udp(self, c):
        self.udp.pop(c.key, None)
        if not isinstance(c, _UdpReverse):
            self.close_sock(c)

    def forget_icmp(self, c):
        self.icmp.pop(c.key, None)
        self.close_sock(c)

    def _reap(self):
        for sock in self.dead:
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass
        self.dead = []

    def _conns(self):
        return list(self.tcp.values()) + list(self.udp.values()) + \
            list(self.icmp.values()) + list(self.listeners)

    # ---------------------------------------------------------- host I/O thread
    def _io_loop(self):
        while not self.stop:
            with self.lock:
                self._reap()
                rd, wr = [], []
                owner = {}
                now = time.monotonic()
                deadline = now + 30.0
                for c in self._conns():
                    if c.sock is None:
                        continue
                    want_r, want_w = c.selectors()
                    if want_r:
                        rd.append(c.sock)
                    if want_w:
                        wr.append(c.sock)
                    if want_r or want_w:
                        # a forwarded UDP flow shares the listener's socket and
                        # does its own reading, so it must not claim it here
                        owner.setdefault(c.sock, c)
                    d = c.deadline()
                    if d is not None and d < deadline:
                        deadline = d
                timeout = max(0.0, min(deadline - now, 30.0))
            try:
                r, w, _ = select.select(rd + [self._wake_r], wr, [], timeout)
            except (OSError, ValueError):
                with self.lock:
                    self._reap()
                continue
            if self._wake_r in r:
                r.remove(self._wake_r)
                try:
                    os.read(self._wake_r, 4096)
                except OSError:
                    pass
            if self.stop:
                break
            with self.lock:
                self._service(r, w, owner)
            if self.rx and self.wake:
                self.wake()

    def _service(self, r, w, owner):
        for sock in w:
            c = owner.get(sock)
            if c is not None and c.sock is not None:
                self._guard(c.on_writable)
        for sock in r:
            c = owner.get(sock)
            if c is not None and c.sock is not None:
                self._guard(c.on_readable)
        now = time.monotonic()
        for c in self._conns():
            if c.sock is not None or c.deadline() is not None:
                self._guard(c.tick, now)

    def _guard(self, fn, *args):
        try:
            fn(*args)
        except Exception:
            self.errors += 1
            if self.debug:
                import traceback
                traceback.print_exc()

    # ---------------------------------------------------------- introspection
    def describe(self):
        return ('user networking: guest %s/%d, gateway %s, DNS %s -> %s'
                % (frame.b2ip(self.guest_ip),
                   bin(int.from_bytes(self.mask, 'big')).count('1'),
                   frame.b2ip(self.host_ip), frame.b2ip(self.dns_ip),
                   ', '.join(self.resolvers)))
