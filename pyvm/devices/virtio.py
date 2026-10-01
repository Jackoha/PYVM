"""The virtio-mmio (version 2) transport shared by the block and network devices.

Register layout and virtqueue handling follow the virtio 1.1 specification;
subclasses only add their device id, feature bits, config space and the
per-queue buffer handling in `handle()`.
"""

import struct

VIRTIO_F_VERSION_1 = 1 << 32

DESC_NEXT, DESC_WRITE, DESC_INDIRECT = 1, 2, 4
AVAIL_F_NO_INTERRUPT = 1
INT_VRING, INT_CONFIG = 1, 2

MAGIC = 0x74726976          # "virt"
VENDOR = 0x554D4551         # "QEMU" -- what Linux expects from a virtio-mmio board

S_ACK, S_DRIVER, S_DRIVER_OK, S_FEATURES_OK = 1, 2, 4, 8


class Virtqueue:
    __slots__ = ('qmax', 'num', 'ready', 'desc', 'avail', 'used', 'last_avail')

    def __init__(self, qmax):
        self.qmax = qmax
        self.reset()

    def reset(self):
        self.num = self.qmax
        self.ready = 0
        self.desc = self.avail = self.used = 0
        self.last_avail = 0


class VirtioMMIO:
    size = 0x1000
    device_id = 0
    queue_max = 256
    num_queues = 1

    def __init__(self, machine, irq):
        self.m = machine
        self.irq = irq
        self.features = VIRTIO_F_VERSION_1
        self.config = b''
        self.config_gen = 0
        self.queues = [Virtqueue(self.queue_max) for _ in range(self.num_queues)]
        self.reset()

    def reset(self):
        self.status = 0
        self.dev_feat_sel = 0
        self.drv_feat_sel = 0
        self.drv_features = 0
        self.queue_sel = 0
        self.int_status = 0
        for q in self.queues:
            q.reset()
        plic = getattr(self.m, 'plic', None)
        if plic is not None:
            plic.set_irq(self.irq, False)

    def close(self):
        pass

    # ------------------------------------------------------------------ registers
    def read(self, off, size):
        if off >= 0x100:
            o = off - 0x100
            return int.from_bytes((self.config[o:o + size] + bytes(size))[:size], 'little')
        if off == 0x00:
            return MAGIC
        if off == 0x04:
            return 2                              # virtio-mmio version 2 (modern)
        if off == 0x08:
            return self.device_id
        if off == 0x0C:
            return VENDOR
        if off == 0x10:
            if self.dev_feat_sel < 2:
                return (self.features >> (32 * self.dev_feat_sel)) & 0xFFFFFFFF
            return 0
        if off == 0x34:
            return self.queue_max if self.queue_sel < self.num_queues else 0
        if off == 0x44:
            return self.queues[self.queue_sel].ready if self.queue_sel < self.num_queues else 0
        if off == 0x60:
            return self.int_status
        if off == 0x70:
            return self.status
        if off == 0xFC:
            return self.config_gen
        return 0

    def write(self, off, size, v):
        v &= 0xFFFFFFFF
        q = self.queues[self.queue_sel] if self.queue_sel < self.num_queues else None
        if off == 0x14:
            self.dev_feat_sel = v
        elif off == 0x20:
            if self.drv_feat_sel < 2:
                sh = 32 * self.drv_feat_sel
                self.drv_features = (self.drv_features & ~(0xFFFFFFFF << sh)) | (v << sh)
        elif off == 0x24:
            self.drv_feat_sel = v
        elif off == 0x30:
            self.queue_sel = v
        elif q is None and 0x38 <= off <= 0xA4:
            pass                                  # queue the driver must not be using
        elif off == 0x38:
            q.num = min(v, q.qmax) or q.qmax
        elif off == 0x44:
            q.ready = v & 1
        elif off == 0x50:
            if v < self.num_queues and self.queues[v].ready:
                self.kick(v)
        elif off == 0x64:
            self.int_status &= ~v
            self.m.plic.set_irq(self.irq, bool(self.int_status))
        elif off == 0x70:
            if v == 0:
                self.reset()
            else:
                self.status = v
                if v & S_DRIVER_OK:
                    self.driver_ok()
        elif off == 0x80:
            q.desc = (q.desc & ~0xFFFFFFFF) | v
        elif off == 0x84:
            q.desc = (q.desc & 0xFFFFFFFF) | (v << 32)
        elif off == 0x90:
            q.avail = (q.avail & ~0xFFFFFFFF) | v
        elif off == 0x94:
            q.avail = (q.avail & 0xFFFFFFFF) | (v << 32)
        elif off == 0xA0:
            q.used = (q.used & ~0xFFFFFFFF) | v
        elif off == 0xA4:
            q.used = (q.used & 0xFFFFFFFF) | (v << 32)

    # ------------------------------------------------------------------ virtqueues
    def _rd(self, pa, n):
        data = self.m.phys_read(pa, n)
        if data is None:
            raise IOError('virtio: bad guest address %#x' % pa)
        return data

    def next_head(self, q):
        """Take the next descriptor chain the driver made available, or None."""
        if not q.ready:
            return None
        avail_idx = struct.unpack('<H', self._rd(q.avail + 2, 2))[0]
        if q.last_avail == avail_idx:
            return None
        slot = q.last_avail % q.num
        head = struct.unpack('<H', self._rd(q.avail + 4 + 2 * slot, 2))[0]
        q.last_avail = (q.last_avail + 1) & 0xFFFF
        return head

    def chain(self, q, head):
        """Walk a descriptor chain -> [(addr, len, device_writable), ...]."""
        descs = []
        table, idx, limit = q.desc, head, q.num
        while True:
            addr, length, flags, nxt = struct.unpack('<QIHH', self._rd(table + 16 * idx, 16))
            if flags & DESC_INDIRECT:
                table, idx, limit = addr, 0, length // 16
                continue
            descs.append((addr, length, flags & DESC_WRITE))
            if not flags & DESC_NEXT or len(descs) > limit + 1:
                return descs
            idx = nxt

    def push_used(self, q, head, written):
        used_idx = struct.unpack('<H', self._rd(q.used + 2, 2))[0]
        elem = q.used + 4 + 8 * (used_idx % q.num)
        self.m.phys_write(elem, struct.pack('<II', head, written))
        self.m.phys_write(q.used + 2, struct.pack('<H', (used_idx + 1) & 0xFFFF))

    def notify(self, q):
        """Raise the used-buffer interrupt unless the driver asked us not to."""
        if struct.unpack('<H', self._rd(q.avail, 2))[0] & AVAIL_F_NO_INTERRUPT:
            return
        self.int_status |= INT_VRING
        self.m.plic.set_irq(self.irq, True)

    def config_changed(self):
        self.config_gen = (self.config_gen + 1) & 0xFFFFFFFF
        self.int_status |= INT_CONFIG
        self.m.plic.set_irq(self.irq, True)

    # ------------------------------------------------------------------ device side
    def driver_ok(self):
        """The driver finished initialisation (status DRIVER_OK)."""

    def kick(self, qi):
        """The driver notified queue `qi`."""
        self.process(qi)

    def process(self, qi=0):
        """Handle every chain currently available on queue `qi`."""
        q = self.queues[qi]
        used = False
        while True:
            head = self.next_head(q)
            if head is None:
                break
            written = self.handle(qi, self.chain(q, head))
            self.push_used(q, head, written)
            used = True
        if used:
            self.notify(q)

    def handle(self, qi, descs):
        """Process one descriptor chain; return the number of bytes written."""
        raise NotImplementedError
