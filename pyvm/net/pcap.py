"""Write captured frames to a pcap file, for `tcpdump -r` / Wireshark."""

import struct
import time


class PcapWriter:
    def __init__(self, path, snaplen=65535):
        self.snaplen = snaplen
        self.f = open(path, 'wb')
        # classic pcap header, microsecond resolution, LINKTYPE_ETHERNET
        self.f.write(struct.pack('<IHHiIII', 0xA1B2C3D4, 2, 4, 0, 0, snaplen, 1))
        self.f.flush()

    def write(self, f):
        if self.f is None:
            return
        t = time.time()
        data = bytes(f[:self.snaplen])
        self.f.write(struct.pack('<IIII', int(t), int(t % 1 * 1e6), len(data), len(f)))
        self.f.write(data)
        self.f.flush()

    def close(self):
        if self.f is not None:
            self.f.close()
            self.f = None
