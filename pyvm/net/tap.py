"""TAP backend: the guest NIC is bridged to a real host network interface.

This needs a TAP device the user may open, which normally means running as root
or being given one:

    sudo ip tuntap add dev pyvm0 mode tap user $USER
    sudo ip addr add 10.0.3.1/24 dev pyvm0
    sudo ip link set pyvm0 up
    python3 -m pyvm -k Image -i rootfs.cpio --net tap,ifname=pyvm0

Unlike user networking the guest is then a real host on that link, so it can be
reached directly and can speak any protocol; routing and NAT on the host are up
to you (`iptables -t nat -A POSTROUTING -s 10.0.3.0/24 -j MASQUERADE`).
"""

import collections
import fcntl
import os
import select
import struct
import threading

from ..errors import NetError
from . import frame

TUNSETIFF = 0x400454CA
IFF_TAP, IFF_NO_PI, IFF_VNET_HDR = 0x0002, 0x1000, 0x4000
RX_LIMIT = 512


class Tap:
    """Raw Ethernet frames to and from a host TAP device."""

    kind = 'tap'

    def __init__(self, ifname='pyvm0', fd=None, mac='52:54:00:12:34:56', mtu=1500,
                 debug=False):
        self.mac = frame.mac2b(mac)
        self.mtu = int(mtu)
        self.debug = bool(debug)
        self.ifname = ifname
        self.rx = collections.deque()
        self.dropped = 0
        self.errors = 0
        self.wake = None
        self.stop = False
        self.thread = None
        self._wake_r = self._wake_w = -1
        if fd is not None:
            self.fd = int(fd)
            os.set_blocking(self.fd, False)
        else:
            self.fd = self._open(ifname)

    @staticmethod
    def _open(ifname):
        try:
            fd = os.open('/dev/net/tun', os.O_RDWR)
        except OSError as e:
            raise NetError('cannot open /dev/net/tun (%s); user networking '
                           '(--net user) needs no privileges' % e.strerror)
        try:
            req = struct.pack('16sH22s', ifname.encode(), IFF_TAP | IFF_NO_PI, b'')
            fcntl.ioctl(fd, TUNSETIFF, req)
        except OSError as e:
            os.close(fd)
            raise NetError('cannot attach to TAP device %r (%s); create it with '
                           '"ip tuntap add dev %s mode tap user $USER"'
                           % (ifname, e.strerror, ifname))
        os.set_blocking(fd, False)
        return fd

    # ---------------------------------------------------------- lifecycle
    def start(self, wake=None):
        self.wake = wake
        self._wake_r, self._wake_w = os.pipe()
        self.thread = threading.Thread(target=self._io_loop, name='pyvm-net', daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop = True
        if self._wake_w >= 0:
            try:
                os.write(self._wake_w, b'\0')
            except OSError:
                pass
        if self.thread is not None:
            self.thread.join(timeout=1.0)
            self.thread = None
        for fd in (self.fd, self._wake_r, self._wake_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.fd = self._wake_r = self._wake_w = -1

    # ---------------------------------------------------------- device interface
    def send(self, f):
        try:
            os.write(self.fd, f)
        except (BlockingIOError, InterruptedError):
            self.dropped += 1
        except OSError:
            self.errors += 1

    def recv(self):
        try:
            return self.rx.popleft()
        except IndexError:
            return None

    def _io_loop(self):
        while not self.stop:
            try:
                r, _, _ = select.select([self.fd, self._wake_r], [], [], 1.0)
            except (OSError, ValueError):
                return
            if self.stop:
                return
            if self._wake_r in r:
                try:
                    os.read(self._wake_r, 4096)
                except OSError:
                    pass
            if self.fd not in r:
                continue
            got = False
            for _ in range(64):
                try:
                    f = os.read(self.fd, 65536)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    return
                if not f:
                    break
                if len(self.rx) >= RX_LIMIT:
                    self.dropped += 1
                    continue
                self.rx.append(f)
                got = True
            if got and self.wake:
                self.wake()

    def describe(self):
        return 'TAP networking on %s (the guest is a real host on that link)' % self.ifname
