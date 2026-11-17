#!/usr/bin/env python3
"""Check the boot menu without needing a terminal.

The menu draws into a curses window and reads keys from it, so a stand-in
window is enough to test the layout, the key handling and the argument list it
produces.

    python3 tests/test_launcher.py [-v]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import curses                                                   # noqa: E402
from pyvm import launcher                                       # noqa: E402

VERBOSE = False
TESTS = []


def test(name):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


class FakeWin:
    """Records what the menu draws, and replays a script of keypresses."""

    def __init__(self, keys=(), size=(24, 100)):
        self.h, self.w = size
        self.keys = list(keys)
        self.grid = [[' '] * self.w for _ in range(self.h)]

    # ---- curses window interface the menu uses
    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.grid = [[' '] * self.w for _ in range(self.h)]

    def addnstr(self, y, x, text, n, attr=0):
        if not 0 <= y < self.h:
            raise curses.error('addnstr outside the window')
        for i, ch in enumerate(str(text)[:n]):
            if 0 <= x + i < self.w:
                self.grid[y][x + i] = ch

    addstr = addnstr

    def move(self, y, x):
        pass

    def refresh(self):
        pass

    def getch(self):
        return self.keys.pop(0) if self.keys else ord('q')

    # ---- helpers for assertions
    def text(self):
        return '\n'.join(''.join(row).rstrip() for row in self.grid)

    def row(self, y):
        return ''.join(self.grid[y]).rstrip()


def fresh_menu(tmpcfg):
    """A menu with no saved state, so tests do not depend on past runs."""
    return launcher.Menu('.', probe=launcher.Probe('.', path=tmpcfg))


@test('every field is drawn, with its value and detail')
def t_draw(cfg):
    menu = fresh_menu(cfg)
    win = FakeWin()
    launcher._draw(win, menu)
    screen = win.text()
    missing = [f.label for f in menu.fields if f.label not in screen]
    assert not missing, 'not drawn: %s' % missing
    assert 'RISC-V' in screen, 'no title'
    assert 'python3 -m pyvm' in screen, 'the equivalent command line is not shown'
    assert 'enter boot' in screen, 'no key help'
    if VERBOSE:
        print('\n' + screen + '\n')


@test('a short window truncates instead of crashing')
def t_small(cfg):
    menu = fresh_menu(cfg)
    for size in ((8, 40), (6, 20), (24, 100), (40, 200)):
        win = FakeWin(size=size)
        launcher._draw(win, menu)          # must not raise curses.error
    if VERBOSE:
        print('   drew cleanly at 8x40, 6x20, 24x100 and 40x200')


@test('arrows move the cursor and change values')
def t_keys(cfg):
    menu = fresh_menu(cfg)
    mem = menu.by_key['mem']
    before = mem.value
    win = FakeWin(keys=[curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_RIGHT, ord('q')])
    assert launcher._loop(win, menu) is None, 'q should cancel'
    assert menu.cursor == 2, 'cursor at %d, expected the Memory row' % menu.cursor
    assert mem.value != before, 'right arrow did not change the value'
    if VERBOSE:
        print('   memory %s -> %s' % (before, mem.value))


@test('enter returns a usable command line, and it parses')
def t_boot(cfg):
    menu = fresh_menu(cfg)
    win = FakeWin(keys=[10])
    argv = launcher._loop(win, menu)
    assert argv, 'enter did not produce an argv'
    from pyvm.__main__ import parse_args
    a = parse_args(argv)
    assert a.kernel and os.path.exists(a.kernel), 'kernel %r missing' % a.kernel
    assert a.initrd or a.disk, 'no root filesystem in %r' % argv
    if VERBOSE:
        print('   argv: %s' % ' '.join(argv))


@test('choosing a disk root passes -d, an initramfs passes -i')
def t_root_kind(cfg):
    menu = fresh_menu(cfg)
    root = menu.by_key['root']
    seen = set()
    for _ in range(len(root.options)):
        value = root.value
        argv = menu.argv()
        if value is None:
            assert '-i' not in argv and '-d' not in argv
            seen.add('none')
        else:
            path, kind = value
            flag = '-i' if kind == 'initrd' else '-d'
            assert flag in argv and path in argv, '%s not passed with %s' % (path, flag)
            seen.add(kind)
        root.step(1)
    assert 'none' in seen, 'the no-root option disappeared'
    if VERBOSE:
        print('   root kinds offered: %s' % ', '.join(sorted(seen)))


@test('a bad instruction limit is refused before booting')
def t_validate(cfg):
    menu = fresh_menu(cfg)
    menu.by_key['limit'].value = 'twelve'
    assert menu.problems(), 'nonsense limit accepted'
    # Enter must refuse to boot and say why.  The complaint is cleared by the
    # next keypress, so look at what was drawn rather than at menu.message.
    win = FakeWin(keys=[10, ord('q')])
    assert launcher._loop(win, menu) is None
    assert 'not a number' in win.text(), 'no complaint drawn:\n%s' % win.text()
    menu.by_key['limit'].value = '1000'
    assert not menu.problems()
    assert '--max-insns' in menu.argv()
    if VERBOSE:
        print('   refused %r, accepted 1000' % 'twelve')


@test('settings are saved and restored')
def t_persist(cfg):
    menu = fresh_menu(cfg)
    menu.by_key['mem'].index = 0
    menu.by_key['stats'].value = True
    menu.by_key['append'].value = 'console=ttyS0 quiet'
    launcher._loop(FakeWin(keys=[10]), menu)     # booting saves the choice
    again = launcher.Menu('.', probe=launcher.Probe('.', path=cfg))
    assert again.by_key['mem'].value == launcher.MEM_CHOICES[0], 'memory not restored'
    assert again.by_key['stats'].value is True, 'flag not restored'
    assert again.by_key['append'].value == 'console=ttyS0 quiet', 'text not restored'
    if VERBOSE:
        print('   restored memory=%s stats=on cmdline=%r'
              % (again.by_key['mem'].show(), again.by_key['append'].value))


def main():
    global VERBOSE
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('-v', '--verbose', action='store_true')
    a = p.parse_args()
    VERBOSE = a.verbose
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.launcher-test.json')
    failed = 0
    print('pyvm boot menu\n')
    try:
        for name, fn in TESTS:
            if os.path.exists(cfg):
                os.remove(cfg)
            try:
                fn(cfg)
                print('  [PASS] %s' % name)
            except Exception as e:
                failed += 1
                print('  [FAIL] %s\n         %s: %s' % (name, type(e).__name__, e))
                if VERBOSE:
                    import traceback
                    traceback.print_exc()
    finally:
        if os.path.exists(cfg):
            os.remove(cfg)
    print('\n  %d/%d passed' % (len(TESTS) - failed, len(TESTS)))
    print('  RESULT: %s' % ('PASS' if not failed else 'FAIL'))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
