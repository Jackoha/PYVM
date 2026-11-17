#!/usr/bin/env python3
"""Measure where an emulated boot spends its instructions.

    python tests/boot_phases.py --kernel Image --initrd rootfs.cpio
    python tests/boot_phases.py --kernel Image --disk disk.img --repeat 3

Boots headlessly with realtime off (so wall time is pure emulation) and
reports the instruction count and elapsed time as the guest passes a few
landmarks in its own console output, ending at the shell prompt.  Landmarks
that do not apply (a disk root unpacks no initrd) are simply left out.

Reading the numbers: the *cumulative* instruction count at each landmark is
repeatable to about 0.1%, so differences between two images are meaningful.
The per-landmark deltas are softer -- console output arrives in bursts, so a
landmark can be noticed slightly late -- and wall time on a loaded host is
softer still.  Compare instructions, not seconds.
"""

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from pyvm.console import BufferConsole          # noqa: E402
from pyvm.machine import Machine                # noqa: E402

LANDMARKS = [
    ('kernel entry',    b'Linux version'),
    ('initrd unpacked', b'Freeing initrd memory'),
    ('init started',    b'as init process'),      # /init, or /sbin/init on a disk
    ('init banner',     b'Welcome to Linux on pyvm'),
    ('shell prompt',    b'root@pyvm:~#'),
]


def boot_once(a):
    con = BufferConsole()
    m = Machine(con, mem_mb=a.mem, disk=a.disk, realtime=False, net=None)
    with open(a.kernel, 'rb') as f:
        kernel = f.read()
    initrd = None
    if a.initrd:
        with open(a.initrd, 'rb') as f:
            initrd = f.read()
    bootargs = 'console=ttyS0 earlycon=sbi'
    if a.disk and not a.initrd:
        bootargs += ' root=/dev/vda rw'
    m.boot_linux(kernel, initrd, bootargs)

    seen = {}

    def watch():
        # Watch for every landmark at once rather than one after another: a
        # landmark that never appears must not hold up the ones behind it.
        t0 = time.time()
        last = LANDMARKS[-1][0]
        while time.time() - t0 < a.timeout and last not in seen:
            out = con.output
            for name, token in LANDMARKS:
                if name not in seen and out.find(token) >= 0:
                    seen[name] = (m.cpu.instret, time.time() - t0)
            time.sleep(0.005)
        if last in seen:
            time.sleep(0.2)
            con.feed(b'poweroff -f\n')
            time.sleep(1.0)
        con.quit_requested = True            # no prompt means no poweroff

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    m.run(None)
    t.join(5)
    m.close()
    for name, _ in LANDMARKS:
        if name not in seen:
            sys.stderr.write('boot_phases: %s never appeared\n' % name)
    return seen


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--kernel', required=True)
    p.add_argument('--initrd')
    p.add_argument('--disk')
    p.add_argument('--mem', type=int, default=128)
    p.add_argument('--repeat', type=int, default=1, help='boot this many times (default 1)')
    p.add_argument('--timeout', type=int, default=300, help='seconds allowed per landmark')
    a = p.parse_args()
    if not (a.initrd or a.disk):
        p.error('need --initrd and/or --disk')

    runs = [boot_once(a) for _ in range(a.repeat)]
    width = max(len(n) for n, _ in LANDMARKS)
    print('%-*s %14s %12s %9s %s'
          % (width, 'landmark', 'instructions', 'delta', 'seconds', 'spread'))
    prev = 0.0
    for name, _ in LANDMARKS:
        vals = [r[name][0] for r in runs if name in r]
        secs = [r[name][1] for r in runs if name in r]
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        print('%-*s %14d %12d %9.2f %s'
              % (width, name, mean, mean - prev, sum(secs) / len(secs),
                 '' if len(vals) < 2 else '%+.2f%%'
                 % (100.0 * (max(vals) - min(vals)) / mean)))
        prev = mean
    return 0


if __name__ == '__main__':
    sys.exit(main())
