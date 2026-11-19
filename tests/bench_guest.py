#!/usr/bin/env python3
"""Time real work inside the guest: boot, then a few CPU-heavy shell commands.

    python tests/bench_guest.py --kernel Image --initrd rootfs.cpio
    python tests/bench_guest.py --kernel Image --initrd rootfs.cpio --no-jit

Each command's wall time is measured from typing it to its output appearing,
the way a person at the console would see it.  --no-jit runs the interpreter
alone, for comparison; --no-jit-cache starts without compiled code from earlier
runs (the first run after installing or changing pyvm looks like that).
"""

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from pyvm import jit                            # noqa: E402
from pyvm.console import BufferConsole          # noqa: E402
from pyvm.machine import Machine                # noqa: E402

COMMANDS = [
    ("seq 1 20000 | awk '{s+=$1} END {print s}'", '200010000'),
    ('dd if=/dev/zero bs=64k count=32 2>/dev/null | md5sum',
     'b2d1236c286a3c0704224fe4105eca49'),
    ('dd if=/dev/zero bs=64k count=16 2>/dev/null | gzip -c | wc -c', ''),
    ('dd if=/dev/zero bs=64k count=16 2>/dev/null | sha256sum',
     '30e14955ebf1352266dc2ff8067e68104607e750abb9d3b36582b8af909fcb58'),
    ('i=0; while [ $i -lt 3000 ]; do i=$((i+1)); done; echo $i', '3000'),
    ('ls -lR / >/dev/null 2>&1; echo walked', 'walked'),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--kernel', required=True)
    ap.add_argument('--initrd', required=True)
    ap.add_argument('--mem', type=int, default=128)
    ap.add_argument('--no-jit', action='store_true')
    ap.add_argument('--no-jit-cache', action='store_true')
    a = ap.parse_args()
    if a.no_jit:
        jit.ENABLED = False
    if a.no_jit_cache:
        jit.CACHE = False
    con = BufferConsole()
    m = Machine(con, mem_mb=a.mem, realtime=True)
    with open(a.kernel, 'rb') as f:
        kernel = f.read()
    with open(a.initrd, 'rb') as f:
        initrd = f.read()
    m.boot_linux(kernel, initrd, 'console=ttyS0 earlycon=sbi')
    results = []

    def wait(token, start, timeout=900):
        t0 = time.time()
        while time.time() - t0 < timeout:
            i = con.output.find(token, start)
            if i >= 0:
                return i + len(token)
            time.sleep(0.01)
        return -1

    def drive():
        t0 = time.time()
        pos = wait(b'# ', 0)
        results.append(('boot to shell prompt', time.time() - t0, pos >= 0))
        for n, (cmd, expect) in enumerate(COMMANDS):
            if pos < 0:
                break
            marker = '@@%d@@' % (1000 + n)
            t0 = time.time()
            con.feed(('%s; echo "@@$((1000+%d))@@"\n' % (cmd, n)).encode())
            end = wait(marker.encode(), pos)
            out = con.output[pos:end].decode(errors='replace') if end > 0 else ''
            results.append((cmd, time.time() - t0, end > 0 and expect in out.split('\n', 1)[-1]))
            pos = end
        con.feed(b'poweroff -f\n')

    th = threading.Thread(target=drive, daemon=True)
    th.start()
    try:
        m.run()
    finally:
        m.close()
    th.join(5)
    total = 0.0
    ok = True
    print('==== guest benchmark (%s)' % ('interpreter only' if a.no_jit else 'jit'))
    for cmd, dt, good in results:
        total += dt
        ok = ok and good
        print('  %7.2fs  %s%s' % (dt, cmd, '' if good else '   <-- WRONG OUTPUT'))
    print('  %7.2fs  total' % total)
    return 0 if ok and len(results) == len(COMMANDS) + 1 else 1


if __name__ == '__main__':
    sys.exit(main())
