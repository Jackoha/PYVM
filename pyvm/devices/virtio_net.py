"""virtio-mmio network device (Linux `eth0`).

Two virtqueues, as virtio requires: queue 0 carries buffers the driver posts for
packets coming in, queue 1 the packets it wants sent.  Each one is prefixed with
a `virtio_net_hdr` that stays all-zero here, because no checksum or segmentation
offload is advertised -- the frames on the wire are plain Ethernet.

Where those frames come from and go to is the backend's business (`pyvm/net/`).
"""

import collections
import struct

from .virtio import VirtioMMIO, VIRTIO_F_VERSION_1, S_DRIVER_OK

VIRTIO_NET_F_MTU = 1 << 3
VIRTIO_NET_F_MAC = 1 << 5
VIRTIO_NET_F_MRG_RXBUF = 1 << 15
VIRTIO_NET_F_STATUS = 1 << 16

VIRTIO_NET_S_LINK_UP = 1

RX, TX = 0, 1
RX_LIMIT = 512                  # frames held for the guest before we drop


class VirtioNet(VirtioMMIO):
    device_id = 1
    num_queues = 2

    def __init__(self, machine, irq, backend, dump=None):
        super().__init__(machine, irq)
        self.backend = backend
        self.dump = dump
        self.pending = collections.deque()
        self.rx_packets = self.tx_packets = 0
        self.rx_dropped = self.tx_dropped = 0
        self.mtu = getattr(backend, 'mtu', 1500)
        self.features = (VIRTIO_F_VERSION_1 | VIRTIO_NET_F_MAC
                         | VIRTIO_NET_F_STATUS | VIRTIO_NET_F_MTU)
        self.config = (backend.mac + struct.pack('<HHH', VIRTIO_NET_S_LINK_UP, 1, self.mtu))

    def reset(self):
        super().reset()
        pending = getattr(self, 'pending', None)   # not yet built during __init__
        if pending is not None:
            pending.clear()

    @property
    def hdr(self):
        """The virtio_net_hdr we prepend to incoming frames (all offloads off).

        Modern (VERSION_1) drivers always use the 12-byte form, which ends in
        num_buffers; a legacy driver without MRG_RXBUF uses the 10-byte one."""
        if self.drv_features & (VIRTIO_F_VERSION_1 | VIRTIO_NET_F_MRG_RXBUF):
            return bytes(10) + struct.pack('<H', 1)
        return bytes(10)

    def close(self):
        if self.backend is not None:
            self.backend.close()
            self.backend = None
        if self.dump is not None:
            self.dump.close()
            self.dump = None

    # ------------------------------------------------------------------ guest -> host
    def handle(self, qi, descs):
        if qi != TX:
            return 0
        data = b''.join(bytes(self._rd(a, n)) for a, n, w in descs if not w)
        hdr = len(self.hdr)
        if len(data) > hdr:
            f = data[hdr:]
            self.tx_packets += 1
            if self.dump is not None:
                self.dump.write(f)
            self.backend.send(f)
        else:
            self.tx_dropped += 1
        return 0

    # ------------------------------------------------------------------ host -> guest
    def kick(self, qi):
        if qi == TX:
            self.process(TX)
        else:
            self.flush_rx()                 # the driver posted receive buffers

    def driver_ok(self):
        self.flush_rx()

    def poll(self):
        """Collect frames from the backend and hand them to the driver."""
        backend = self.backend
        if backend is None:
            return 0
        while True:
            f = backend.recv()
            if f is None:
                break
            if self.dump is not None:
                self.dump.write(f)
            if len(self.pending) >= RX_LIMIT:
                self.rx_dropped += 1
                continue
            self.pending.append(f)
        return self.flush_rx()

    def flush_rx(self):
        if not self.pending:
            return 0
        q = self.queues[RX]
        if not q.ready or not self.status & S_DRIVER_OK:
            return 0
        hdr = self.hdr
        delivered, used = 0, False
        while self.pending:
            head = self.next_head(q)
            if head is None:
                break
            data = hdr + self.pending.popleft()
            n = 0
            for addr, length, writable in self.chain(q, head):
                if not writable or n >= len(data):
                    continue
                chunk = data[n:n + length]
                self.m.phys_write(addr, chunk)
                n += len(chunk)
            if n < len(data):
                self.rx_dropped += 1        # buffer too small: the driver discards it
                n = 0
            else:
                self.rx_packets += 1
                delivered += 1
            self.push_used(q, head, n)
            used = True
        if used:
            self.notify(q)
        return delivered

    def stats(self):
        return ('net: %d packets in (%d dropped), %d out (%d dropped)'
                % (self.rx_packets, self.rx_dropped, self.tx_packets, self.tx_dropped))
