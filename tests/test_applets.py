#!/usr/bin/env python3
"""Check that mkrootfs can still read busybox's applet table.

The table is found by shape, not by symbol name, so a different busybox build
could stop matching.  That failure is quiet -- the guest just goes back to
running `busybox --install -s` at boot -- so it is worth a test.

    python3 tests/test_applets.py [-v]

The tests that need a binary are skipped when ./busybox.static is absent.
"""

import argparse
import os
import stat
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import mkrootfs                                                 # noqa: E402

BUSYBOX = os.path.join(ROOT, 'busybox.static')
VERBOSE = False
TESTS = []


def test(name, needs_busybox=False):
    def deco(fn):
        TESTS.append((name, fn, needs_busybox))
        return fn
    return deco


def eq(got, want, what):
    if got != want:
        raise AssertionError('%s: got %r, wanted %r' % (what, got, want))


# ------------------------------------------------------------------ the parse
@test('the applet table parses', True)
def t_parses(blob):
    applets = mkrootfs.busybox_applets(blob)
    if len(applets) < 200:
        raise AssertionError('only %d applets' % len(applets))
    if len(set(applets)) != len(applets):
        raise AssertionError('duplicate (dir, name) pairs')
    for d, _ in applets:
        if d not in mkrootfs.APPLET_DIRS:
            raise AssertionError('bad directory %r' % d)


@test('every applet the init script needs is there', True)
def t_needed(blob):
    names = {n for _, n in mkrootfs.busybox_applets(blob)}
    missing = [n for n in mkrootfs.APPLET_NEEDED if n not in names]
    if missing:
        raise AssertionError('missing: %s' % ', '.join(missing))


@test('applets land in the directories busybox installs them in', True)
def t_dirs(blob):
    where = {n: d for d, n in mkrootfs.busybox_applets(blob)}
    for name, want in (('sh', 'bin'), ('ls', 'bin'), ('mount', 'bin'),
                       ('init', 'sbin'), ('ifconfig', 'sbin'), ('route', 'sbin'),
                       ('awk', 'usr/bin'), ('env', 'usr/bin'),
                       ('crond', 'usr/sbin'), ('chroot', 'usr/sbin')):
        if name in where:
            eq(where[name], want, name)


@test('a binary with no table is refused, not guessed at')
def t_refused(_blob):
    for label, bad in (('empty', b''),
                       ('no table', b'\x01\x02\x03' * 40000),
                       ('names but no array', b'[\x00[[\x00ls\x00sh\x00\x00')):
        eq(mkrootfs.busybox_applets(bad), [], label)


@test('a truncated binary is refused', True)
def t_truncated(blob):
    half = mkrootfs.busybox_applets(blob[:len(blob) // 2])
    eq(half, [], 'truncated at half')


# ------------------------------------------------------------------ the image
def cpio_entries(data):
    """Parse a newc cpio back into {name: (mode, target_or_data)}."""
    out = {}
    i = 0
    while data[i:i + 6] == b'070701':
        f = [int(data[i + 6 + 8 * k:i + 14 + 8 * k], 16) for k in range(13)]
        mode, size, namesize = f[1], f[6], f[11]
        name = data[i + 110:i + 110 + namesize - 1].decode()
        i += 110 + namesize
        i += -i % 4
        body = data[i:i + size]
        i += size
        i += -i % 4
        if name == 'TRAILER!!!':
            break
        out[name] = (mode, body)
    return out


def build_cpio(**kw):
    a = types.SimpleNamespace(busybox=BUSYBOX, init=None, dir=[],
                              no_applets=False, applets_made=0)
    for k, v in kw.items():
        setattr(a, k, v)
    w = mkrootfs.CpioWriter()
    mkrootfs.populate(w, a)
    return cpio_entries(w.finish()), a


@test('the links are in the image, pointing at busybox', True)
def t_in_image(_blob):
    ents, a = build_cpio()
    if a.applets_made < 200:
        raise AssertionError('only %d links made' % a.applets_made)
    for path in ('bin/ls', 'sbin/ifconfig', 'usr/bin/awk', 'usr/sbin/crond'):
        if path not in ents:
            raise AssertionError('%s missing from the image' % path)
        mode, target = ents[path]
        if not stat.S_ISLNK(mode):
            raise AssertionError('%s is not a symlink' % path)
        eq(target, b'/bin/busybox', '%s target' % path)


@test('an applet never overwrites a file populate() wrote', True)
def t_no_clobber(_blob):
    # busybox has an `init` applet that installs to /sbin/init -- which is the
    # disk root's entry point and must stay a link to the init script.
    ents, _ = build_cpio()
    eq(ents['sbin/init'][1], b'../init', '/sbin/init')
    eq(ents['bin/sh'][1], b'busybox', '/bin/sh')
    eq(stat.S_ISREG(ents['bin/busybox'][0]), True, '/bin/busybox is a file')
    eq(stat.S_ISREG(ents['init'][0]), True, '/init is a file')


@test('--no-applets leaves the links out', True)
def t_opt_out(_blob):
    ents, a = build_cpio(no_applets=True)
    eq(a.applets_made, 0, 'links made')
    eq('bin/ls' in ents, False, 'bin/ls present')
    eq('bin/sh' in ents, True, 'bin/sh present')          # this one is explicit


@test('the init script keeps its fallback', True)
def t_fallback(_blob):
    # With --no-applets there are no links, so the guest must still be told to
    # make them itself.
    if '--install -s' not in mkrootfs.INIT_SCRIPT:
        raise AssertionError('the init script lost its `busybox --install -s`')
    if '[ -e /bin/ls ]' not in mkrootfs.INIT_SCRIPT:
        raise AssertionError('the init script lost the guard on that line')


def main():
    global VERBOSE
    p = argparse.ArgumentParser()
    p.add_argument('-v', '--verbose', action='store_true')
    VERBOSE = p.parse_args().verbose

    blob = None
    if os.path.exists(BUSYBOX):
        with open(BUSYBOX, 'rb') as f:
            blob = f.read()

    print('pyvm busybox applet table\n')
    failed = skipped = 0
    for name, fn, needs in TESTS:
        if needs and blob is None:
            skipped += 1
            print('  [skip] %s  (no ./busybox.static -- run `make busybox`)' % name)
            continue
        try:
            fn(blob)
            print('  [PASS] %s' % name)
        except Exception as e:
            failed += 1
            print('  [FAIL] %s\n         %s: %s' % (name, type(e).__name__, e))
            if VERBOSE:
                import traceback
                traceback.print_exc()
    ran = len(TESTS) - skipped
    print('\n  %d/%d passed%s' % (ran - failed, ran,
                                  ', %d skipped' % skipped if skipped else ''))
    print('  RESULT: %s' % ('PASS' if not failed else 'FAIL'))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
