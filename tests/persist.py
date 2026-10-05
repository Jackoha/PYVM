#!/usr/bin/env python3
"""Check that the ext4 disk remembers things across a power cycle, and which
ways of stopping the VM are safe.

Each scenario boots with disk.img as the root filesystem, writes a file, stops
the VM in a particular way, then boots again and looks for the file:

    sync      `sync; poweroff -f`  -- the careful way
    poweroff  `poweroff -f`        -- no explicit sync first
    hard      pull the plug        -- what Ctrl-A X does

    python3 tests/persist.py --kernel Image --disk disk.img

Exit status 0 if a clean poweroff persists the data.  The hard-quit result is
reported but not required to pass: it says how much a yanked VM can lose.
The disk is left as it was found.
"""

import argparse
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from pyvm.console import BufferConsole          # noqa: E402
from pyvm.machine import Machine                # noqa: E402

PROMPT = b'# '
ANSI = re.compile(rb'\x1b\[[0-9;?]*[a-zA-Z]')


class Shell(threading.Thread):
    """Drive the guest shell, then stop the VM the requested way."""

    def __init__(self, con, cmds, stop, timeout):
        super().__init__(daemon=True)
        self.con = con
        self.cmds = cmds
        self.stop = stop
        self.timeout = timeout
        self.out = {}
        self.reached_prompt = False

    def _wait(self, token, start, timeout):
        end = time.time() + timeout
        while time.time() < end:
            i = self.con.output.find(token, start)
            if i >= 0:
                return i + len(token)
            time.sleep(0.05)
        return -1

    @staticmethod
    def _clean(body, cmd, marker):
        """Keep only what the command printed: no prompt, echo, marker or ANSI."""
        text = ANSI.sub(b'', bytes(body)).replace(b'\r', b'')
        keep = []
        for line in text.split(b'\n'):
            s = line.strip()
            if not s or marker in s or cmd.encode() in s or s.endswith(b'#'):
                continue
            keep.append(s)
        return b'\n'.join(keep)

    def run(self):
        pos = self._wait(PROMPT, 0, self.timeout)
        if pos < 0:
            self.con.quit_requested = True
            return
        self.reached_prompt = True
        for n, cmd in enumerate(self.cmds):
            marker = ('@@%d@@' % (900 + n)).encode()
            self.con.feed(('%s; echo "@@$((900+%d))@@"\n' % (cmd, n)).encode())
            end = self._wait(marker, pos, 300)
            self.out[cmd] = self._clean(self.con.output[pos:end] if end > 0 else b'',
                                        cmd, marker)
            pos = end if end > 0 else len(self.con.output)
        if self.stop == 'sync':
            self.con.feed(b'sync; poweroff -f\n')
        elif self.stop == 'poweroff':
            self.con.feed(b'poweroff -f\n')
        else:                                   # 'hard': exactly what Ctrl-A X does
            self.con.quit_requested = True


def boot(kernel, disk, cmds, stop, mem, timeout):
    con = BufferConsole()
    m = Machine(con, mem_mb=mem, disk=disk)
    m.boot_linux(open(kernel, 'rb').read(), None,
                 'console=ttyS0 earlycon=sbi root=/dev/vda rw')
    sh = Shell(con, cmds, stop, timeout)
    sh.start()
    m.run()
    m.close()
    if not sh.reached_prompt:
        raise SystemExit('the guest never reached a shell prompt')
    return sh.out


def scenario(a, stop):
    token = ('%s-%d' % (stop, time.time() * 1000)).encode()
    boot(a.kernel, a.disk,
         ['echo %s > /root/persist.txt' % token.decode()], stop, a.mem, a.timeout)
    out = boot(a.kernel, a.disk,
               ['cat /root/persist.txt 2>&1', 'rm -f /root/persist.txt'],
               'sync', a.mem, a.timeout)
    got = out['cat /root/persist.txt 2>&1']
    return token in got, got


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--kernel', default='Image')
    p.add_argument('--disk', default='disk.img')
    p.add_argument('--mem', type=int, default=128)
    p.add_argument('--timeout', type=int, default=600)
    a = p.parse_args()

    labels = {'sync': '`sync; poweroff -f`', 'poweroff': '`poweroff -f` (no sync)',
              'hard': 'hard quit (Ctrl-A X)'}
    results = {}
    for stop in ('sync', 'poweroff', 'hard'):
        print('==== %-24s write, stop, reboot, look' % labels[stop])
        results[stop] = scenario(a, stop)
        kept, got = results[stop]
        print('     %s   %s' % ('kept the file' if kept else 'LOST the file',
                                got[:70].decode(errors='replace')))
    print()
    for stop in ('sync', 'poweroff', 'hard'):
        kept, _ = results[stop]
        required = stop != 'hard'
        mark = 'PASS' if kept else ('FAIL' if required else 'lost')
        print('  [%s] %-26s %s' % (mark, labels[stop],
                                   'data survived' if kept else 'data lost'))
    ok = results['sync'][0] and results['poweroff'][0]
    print('\n  RESULT: %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
