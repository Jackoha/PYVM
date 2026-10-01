"""virtio-mmio block device backed by a raw disk image (Linux `/dev/vda`)."""

import os
import struct

from .virtio import VirtioMMIO, VIRTIO_F_VERSION_1

VIRTIO_BLK_F_RO = 1 << 5
VIRTIO_BLK_F_FLUSH = 1 << 9

T_IN, T_OUT, T_FLUSH, T_GET_ID = 0, 1, 4, 8
S_OK, S_IOERR, S_UNSUPP = 0, 1, 2


class VirtioBlock(VirtioMMIO):
    device_id = 2
    num_queues = 1

    def __init__(self, machine, irq, path, readonly=False):
        super().__init__(machine, irq)
        self.readonly = readonly
        self.fd = os.open(path, os.O_RDONLY if readonly else os.O_RDWR)
        self.capacity = os.fstat(self.fd).st_size // 512
        self.features = VIRTIO_F_VERSION_1 | VIRTIO_BLK_F_FLUSH
        if readonly:
            self.features |= VIRTIO_BLK_F_RO
        # capacity (sectors), size_max, seg_max
        self.config = struct.pack('<QII', self.capacity, 0, 128)

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def handle(self, qi, descs):
        readable = b''.join(self._rd(a, n) for a, n, w in descs if not w)
        writable = [(a, n) for a, n, w in descs if w]
        if len(readable) < 16 or not writable:
            return 0
        rtype, _, sector = struct.unpack('<IIQ', readable[:16])
        status_addr = writable[-1][0] + writable[-1][1] - 1
        out_bufs = list(writable)
        out_bufs[-1] = (out_bufs[-1][0], out_bufs[-1][1] - 1)
        written = 0
        status = S_OK
        try:
            if rtype == T_IN:
                pos = sector * 512
                for a, n in out_bufs:
                    if n <= 0:
                        continue
                    data = os.pread(self.fd, n, pos)
                    data += bytes(n - len(data))
                    self.m.phys_write(a, data)
                    pos += n
                    written += n
            elif rtype == T_OUT:
                if self.readonly:
                    status = S_IOERR
                else:
                    os.pwrite(self.fd, readable[16:], sector * 512)
            elif rtype == T_FLUSH:
                if not self.readonly:
                    os.fsync(self.fd)
            elif rtype == T_GET_ID:
                ident = b'pyvm-disk'.ljust(20, b'\0')
                a, n = out_bufs[0]
                self.m.phys_write(a, ident[:n])
                written = min(n, 20)
            else:
                status = S_UNSUPP
        except OSError:
            status = S_IOERR
        self.m.phys_write(status_addr, bytes((status,)))
        return written + 1
