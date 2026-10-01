#!/usr/bin/env python3
"""Run riscv-tests ISA test binaries (https://github.com/riscv-software-src/riscv-tests)
on pyvm.  Each test reports pass/fail through the HTIF `tohost` word.

    python tests/run_isa_tests.py path/to/riscv-tests/out [-k pattern]
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from pyvm.console import BufferConsole          # noqa: E402
from pyvm.machine import Machine                # noqa: E402


def run_one(path, max_insns):
    con = BufferConsole()
    m = Machine(con, mem_mb=64, realtime=False)
    with open(path, 'rb') as f:
        m.boot_bare(f.read())
    try:
        code = m.run(max_insns)
    except Exception as e:                      # emulator bug: report, keep going
        return 'crash: %s: %s' % (type(e).__name__, e)
    if getattr(m, 'halt_reason', '') == 'instruction limit reached':
        return 'timeout'
    return 'ok' if code == 0 else 'fail (test #%d)' % code


def main():
    p = argparse.ArgumentParser()
    p.add_argument('dir')
    p.add_argument('-k', default='', help='only run tests whose name contains this')
    p.add_argument('--max-insns', type=int, default=20_000_000)
    a = p.parse_args()
    tests = sorted(f for f in os.listdir(a.dir)
                   if a.k in f and not f.endswith(('.err', '.dump')))
    failed = []
    t0 = time.time()
    for t in tests:
        r = run_one(os.path.join(a.dir, t), a.max_insns)
        if r != 'ok':
            failed.append(t)
            print('%-32s %s' % (t, r))
    print('%d/%d passed in %.1fs' % (len(tests) - len(failed), len(tests), time.time() - t0))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
