"""Boot menu: choose what to run before the virtual machine starts.

`python3 -m pyvm` with no arguments (on a terminal) opens this instead of
printing a usage error, so the VM is usable without remembering flags.  It finds
the kernels and root filesystems lying around, reads the Linux version out of
each kernel and the distribution out of each disk image, and hands the choice
back to the command line parser as an ordinary argument list -- the flags stay
the single source of truth, the menu is only a front end for them.

`g` fetches another Linux version: the menu steps out of curses, asks
tools/get_kernel.py what kernel.org is publishing -- the current releases, or
every release in a series you name -- then downloads and builds the version
chosen and comes back with the new image selected.

Probing an image costs a `debugfs` call, so results are cached in
`.pyvm-launcher.json` next to the images, together with the last choice made.
"""

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time

CONFIG = '.pyvm-launcher.json'
MEM_CHOICES = [64, 128, 256, 512, 1024, 2048]
NET_CHOICES = [
    ('user', 'NAT gateway in Python, no privileges needed'),
    ('user,hostfwd=tcp::8080-:5000', 'NAT plus host :8080 -> guest :5000'),
    ('none', 'no network device at all'),
    ('tap,ifname=pyvm0', 'bridge to a host TAP device (needs one made for you)'),
]
KERNEL_PATTERNS = ('Image', 'Image-*', 'vmlinux', '*.elf')
INITRD_PATTERNS = ('*.cpio', '*.cpio.gz', 'initramfs*', 'rootfs*')
DISK_PATTERNS = ('*.img', '*.ext4', '*.qcow')     # qcow is listed so we can refuse it clearly

EXT4_MAGIC = 0xEF53


# ---------------------------------------------------------------- image probing
def human(n):
    for unit in ('B', 'K', 'M', 'G'):
        if n < 1024 or unit == 'G':
            return '%d%s' % (n, unit) if unit == 'B' else '%.0f%s' % (n, unit)
        n /= 1024.0


def kernel_banner(path, limit=12 << 20):
    """The `Linux version ...` string a kernel image carries, or None.

    A riscv `Image` is not compressed, so the banner is simply sitting in it as
    plain text; an ELF `vmlinux` has it too."""
    try:
        with open(path, 'rb') as f:
            blob = f.read(limit)
    except OSError:
        return None
    # A kernel carries the banner more than once -- one copy is the template
    # without the build number -- so take the most informative occurrence.
    best = None
    start = 0
    while True:
        i = blob.find(b'Linux version ', start)
        if i < 0:
            break
        start = i + 14
        end = blob.find(b'\0', i, i + 400)
        text = blob[i:end if end > i else i + 400].decode('utf-8', 'replace')
        m = re.match(r'Linux version (\S+)', text)
        if not m:
            continue
        # the build number and date follow the compiler blurb, which itself has
        # nested parentheses, so they are easier to find on their own
        b = re.search(r'(#\d+)\s+\w{3}\s+(\w{3}\s+\d+)', text)
        label = ' '.join(x for x in ('Linux', m.group(1),
                                     ' '.join(b.groups()) if b else '') if x)
        if best is None or len(label) > len(best):
            best = label
        if b:
            break
    return best


def fs_kind(path):
    """Identify a root filesystem image by its magic numbers."""
    try:
        with open(path, 'rb') as f:
            head = f.read(6)
            f.seek(0x438)
            sb = f.read(2)
    except OSError:
        return None
    if head[:6] == b'070701':
        return 'cpio'
    if head[:2] == b'\x1f\x8b':
        return 'cpio.gz'
    if head[:4] == b'\x7fELF':
        return 'elf'
    if len(sb) == 2 and struct.unpack('<H', sb)[0] == EXT4_MAGIC:
        return 'ext4'
    return None


def ext4_release(path):
    """The distribution on an ext4 image, read with debugfs when available."""
    debugfs = shutil.which('debugfs') or '/sbin/debugfs'
    if not os.path.exists(debugfs):
        return None
    def cat(name):
        try:
            p = subprocess.run([debugfs, '-R', 'cat ' + name, path],
                               capture_output=True, timeout=10)
            return p.stdout.decode('utf-8', 'replace').strip()
        except (OSError, subprocess.SubprocessError):
            return ''
    text = cat('/etc/alpine-release')
    if text and re.match(r'^\d', text):
        return 'Alpine ' + text.split()[0]
    text = cat('/etc/os-release')
    m = re.search(r'^PRETTY_NAME="?([^"\n]+)', text, re.M)
    if m:
        return m.group(1)
    if 'busybox' in cat('/init').lower():
        return 'busybox'
    return None


class Probe:
    """Describes images, caching what is expensive to work out."""

    def __init__(self, root='.', path=None):
        self.root = root
        self.path = path or os.path.join(root, CONFIG)
        self.data = {}
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            pass
        self.cache = self.data.setdefault('probe', {})

    def save(self, settings=None):
        if settings is not None:
            self.data['last'] = settings
        try:
            with open(self.path, 'w') as f:
                json.dump(self.data, f, indent=1, sort_keys=True)
        except OSError:
            pass                     # a read-only directory is not worth failing over

    def last(self):
        return self.data.get('last') or {}

    def describe(self, path, kernel=False):
        """A one-line description of an image, from the cache when possible."""
        try:
            st = os.stat(path)
        except OSError:
            return '(missing)'
        key = '%s|%d|%d|%d' % (os.path.basename(path), st.st_size, int(st.st_mtime), kernel)
        if key in self.cache:
            return self.cache[key]
        if kernel:
            text = kernel_banner(path) or 'unrecognised kernel'
        else:
            kind = fs_kind(path)
            if kind is None:
                text = 'unknown format'
            elif kind.startswith('cpio'):
                text = 'initramfs (%s)' % kind
            elif kind == 'ext4':
                text = ', '.join(x for x in (ext4_release(path), 'ext4 disk') if x)
            else:
                text = kind
        text = '%s, %s' % (text, human(st.st_size))
        self.cache[key] = text
        return text


def discover(root='.'):
    """-> (kernels, roots).  A root is (path, kind) with kind 'initrd' or 'disk'."""
    import glob
    def find(patterns):
        out = []
        for pat in patterns:
            for p in sorted(glob.glob(os.path.join(root, pat))):
                if os.path.isfile(p) and p not in out:
                    out.append(p)
        return out
    kernels = [p for p in find(KERNEL_PATTERNS)
               if fs_kind(p) in (None, 'elf') and os.path.getsize(p) > 1 << 18]
    roots = []
    for p in find(INITRD_PATTERNS):
        if (fs_kind(p) or '').startswith('cpio'):
            roots.append((p, 'initrd'))
    for p in find(DISK_PATTERNS):
        if fs_kind(p) == 'ext4':
            roots.append((p, 'disk'))
    return kernels, roots


# ------------------------------------------------------------- fetching kernels
def _tool(name):
    """A script in the project's tools/ directory, next to this package."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, 'tools', name)


def _ask_get_kernel(args, root, timeout):
    """Run tools/get_kernel.py for JSON.  -> (parsed, error)."""
    try:
        p = subprocess.run([sys.executable, _tool('get_kernel.py')] + args
                           + ['--json', '--root', root],
                           capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    if p.returncode != 0:
        err = p.stderr.decode('utf-8', 'replace').strip().splitlines()
        return None, err[0] if err else 'tools/get_kernel.py failed'
    try:
        return json.loads(p.stdout.decode('utf-8', 'replace')), None
    except ValueError:
        return None, 'tools/get_kernel.py printed something unexpected'


def kernel_versions(root='.', timeout=180):
    """The releases kernel.org is publishing now.  -> (rows, error)."""
    got, err = _ask_get_kernel(['--list'], root, timeout)
    if err:
        return [], err
    return got.get('releases', []), None


def kernel_series(spec, root='.', timeout=300):
    """Every fetchable release in one series, newest first.  -> (list, error)."""
    got, err = _ask_get_kernel(['--list', spec], root, timeout)
    if err:
        return [], err
    return got.get('versions', []), None


SERIES_RE = re.compile(r'\d+(\.\d+)?\Z')
SERIES_LIMIT = 60          # a series can hold ~200 releases; show the newest


def _ask(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def _series_of(version):
    return '.'.join(version.split('.')[:2])


def choose_in_series(spec, root='.'):
    """List every fetchable release in a series and pick one.  -> version/None."""
    print('\n  Asking kernel.org for every %s release...' % spec)
    versions, err = kernel_series(spec, root)
    if err:
        print('  kernel.org: %s' % err)
        return None
    if not versions:
        print('  kernel.org has no releases in %s' % spec)
        return None
    print('\n  Linux %s -- %d releases, newest first:\n' % (spec, len(versions)))
    for i in range(0, min(len(versions), SERIES_LIMIT), 5):
        print('  ' + '  '.join('%4d) %-8s' % (i + j + 1, v)
                               for j, v in enumerate(versions[i:i + 5])).rstrip())
    if len(versions) > SERIES_LIMIT:
        print('        (and %d older, back to %s -- type the one you want)'
              % (len(versions) - SERIES_LIMIT, versions[-1]))
    while True:
        answer = _ask('\n  number, version, or Enter to go back: ')
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(versions):
            return versions[int(answer) - 1]
        if answer.lstrip('vV') in versions:
            return answer.lstrip('vV')
        print('  %r is not one of the %s releases above' % (answer, spec))


def choose_version(root='.'):
    """Ask which Linux version to fetch.  -> a version string, or None."""
    print('\n  Asking kernel.org which versions it has...')
    rows, err = kernel_versions(root)
    if err:
        print('  kernel.org: %s' % err)
        return None
    usable = [r for r in rows if r.get('usable')]
    if not usable:
        print('  kernel.org listed no installable version')
        return None
    print('\n  Linux versions kernel.org is publishing:\n')
    for i, r in enumerate(usable, 1):
        notes = ' '.join(x for x in ('(end of life)' if r['eol'] else '',
                                     '(built here already)' if r['installed'] else '')
                         if x)
        print('    %2d) %-12s %-10s %-11s %s'
              % (i, r['version'], r['moniker'], r['date'], notes))
    series = sorted({_series_of(r['version']) for r in usable},
                    key=lambda v: [int(x) for x in v.split('.')], reverse=True)
    print('\n  Those are only the newest of each series.  Type a series to see'
          '\n  every release in it: %s -- or an older one (6.4, 5.4, ...).'
          % ', '.join(series))
    while True:
        answer = _ask('\n  number, version, series, or Enter to go back: ')
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(usable):
            return usable[int(answer) - 1]['version']
        spec = answer.lstrip('vV')
        if SERIES_RE.match(spec):              # 6.6 or 6: a series, not a release
            picked = choose_in_series(spec, root)
            if picked:
                return picked
            continue
        return spec


def get_kernel(root='.'):
    """Choose a Linux version, download and build it.  -> (image path, message).

    This runs on the real terminal rather than inside the menu: fetching
    ~140 MB and building a kernel takes minutes and prints a great deal, and
    none of that belongs in a curses window.
    """
    if not os.path.exists(_tool('get_kernel.py')):
        return None, 'tools/get_kernel.py is missing'
    version = choose_version(root)
    if not version:
        return None, ''
    print()
    try:
        r = subprocess.run([sys.executable, _tool('get_kernel.py'), version,
                            '--root', root])
    except KeyboardInterrupt:
        return None, 'interrupted'
    if r.returncode != 0:
        _pause('  Linux %s was not built -- the output above says why.' % version)
        return None, 'could not build Linux %s' % version
    image = os.path.join(root, 'Image-%s' % version)
    if not os.path.exists(image):
        return None, 'the build finished but %s is not there' % os.path.basename(image)
    _pause('  Linux %s is ready.' % version)
    return image, 'Linux %s built, and selected above' % version


def _pause(text):
    try:
        input('\n%s  Press Enter to go back to the menu. ' % text)
    except (EOFError, KeyboardInterrupt):
        pass


# ---------------------------------------------------------------- settings model
class Field:
    """One row of the menu.  `key` names it in the saved configuration."""

    def __init__(self, key, label, help=''):
        self.key = key
        self.label = label
        self.help = help

    def detail(self, menu):
        return ''


class Pick(Field):
    """Cycles through a list of (value, detail) pairs."""

    def __init__(self, key, label, options, index=0, help='', fmt=None):
        super().__init__(key, label, help)
        self.options = list(options)
        self.index = min(index, max(0, len(self.options) - 1))
        self.fmt = fmt

    @property
    def value(self):
        return self.options[self.index][0] if self.options else None

    def show(self):
        if not self.options:
            return '(none found)'
        v = self.value
        if self.fmt:
            return self.fmt(v)
        if isinstance(v, tuple):            # (path, kind) for a root filesystem
            v = v[0]
        if v is None:
            return '(none)'
        return os.path.basename(v) if isinstance(v, str) and os.sep in v else str(v)

    def detail(self, menu):
        return self.options[self.index][1] if self.options else ''

    def step(self, delta):
        if self.options:
            self.index = (self.index + delta) % len(self.options)

    def load(self, saved):
        for i, (v, _) in enumerate(self.options):
            if v == saved:
                self.index = i
                return

    def dump(self):
        return self.value


class Flag(Field):
    def __init__(self, key, label, value=False, help=''):
        super().__init__(key, label, help)
        self.value = bool(value)

    def show(self):
        return 'on' if self.value else 'off'

    def step(self, delta):
        self.value = not self.value

    def load(self, saved):
        self.value = bool(saved)

    def dump(self):
        return self.value


class Line(Field):
    """Free text, edited in place."""

    def __init__(self, key, label, value='', hint='', help=''):
        super().__init__(key, label, help)
        self.value = value
        self.hint = hint

    def show(self):
        return self.value or ('(%s)' % self.hint if self.hint else '(empty)')

    def step(self, delta):
        return 'edit'

    def load(self, saved):
        self.value = str(saved or '')

    def dump(self):
        return self.value


# ---------------------------------------------------------------- the menu
KEYS = ' ↑↓ move   ←→ change   enter boot   e edit   g get kernel   s save   q quit '


class Menu:
    def __init__(self, root='.', probe=None):
        self.root = root
        self.probe = probe or Probe(root)
        kernels, roots = discover(root)
        kopts = self.kernel_options(kernels)
        ropts = [(None, 'no root filesystem (the kernel will panic)')]
        for path, kind in roots:
            tag = 'initramfs, in RAM: changes are lost' if kind == 'initrd' \
                else 'disk: changes are saved'
            ropts.append(((path, kind), '%s  --  %s' % (self.probe.describe(path), tag)))
        self.fields = [
            Pick('kernel', 'Kernel', kopts,
                 help='which kernel image to boot -- g fetches another version'),
            Pick('root', 'Root filesystem', ropts, index=1 if len(ropts) > 1 else 0,
                 help='initramfs lives in RAM; a disk image keeps your changes'),
            Pick('mem', 'Memory', [(m, '%d MiB of guest RAM' % m) for m in MEM_CHOICES],
                 index=MEM_CHOICES.index(256), help='RAM given to the guest',
                 fmt=lambda m: '%d MiB' % m),
            Pick('net', 'Network', NET_CHOICES, help='see README: Networking'),
            Flag('disk_ro', 'Disk read-only', False, help='attach the disk image read-only'),
            Flag('realtime', 'Real time', True,
                 help='off = skip idle periods, so the guest clock is deterministic'),
            Flag('stats', 'Print stats', False, help='instruction count and MIPS at exit'),
            Line('append', 'Kernel cmdline', '', 'default',
                 help='replaces the default console=/root= arguments'),
            Line('dump', 'Capture to pcap', '', 'off',
                 help='write every frame the NIC sees to this file'),
            Line('limit', 'Stop after N insns', '', 'no limit',
                 help='for benchmarking; empty means run until the guest exits'),
        ]
        self.by_key = {f.key: f for f in self.fields}
        self.cursor = 0
        self.message = ''
        self.restore(self.probe.last())

    # ------------------------------------------------------------ images
    def kernel_options(self, kernels=None):
        if kernels is None:
            kernels = discover(self.root)[0]
        return [(k, self.probe.describe(k, kernel=True)) for k in kernels]

    def rescan(self, select=None):
        """Pick up images that appeared since the menu opened."""
        f = self.by_key['kernel']
        f.options = self.kernel_options()
        f.index = 0
        if select:
            f.load(select)

    # ------------------------------------------------------------ persistence
    def restore(self, saved):
        for key, value in (saved or {}).items():
            f = self.by_key.get(key)
            if f is None:
                continue
            if key == 'root' and isinstance(value, list):
                value = tuple(value)
            f.load(value)

    def snapshot(self):
        return {f.key: f.dump() for f in self.fields}

    # ------------------------------------------------------------ result
    def argv(self):
        """The command line this menu describes."""
        g = lambda k: self.by_key[k]
        out = []
        kernel = g('kernel').value
        if not kernel:
            return None
        out += ['-k', kernel]
        root = g('root').value
        if root:
            path, kind = root
            out += (['-i', path] if kind == 'initrd' else ['-d', path])
            if kind == 'disk' and g('disk_ro').value:
                out.append('--disk-ro')
        out += ['-m', str(g('mem').value)]
        out += ['--net', g('net').value]
        if g('append').value.strip():
            out += ['-a', g('append').value.strip()]
        if g('dump').value.strip():
            out += ['--net-dump', g('dump').value.strip()]
        if g('limit').value.strip().isdigit():
            out += ['--max-insns', g('limit').value.strip()]
        if not g('realtime').value:
            out.append('--no-realtime')
        if g('stats').value:
            out.append('--stats')
        return out

    def problems(self):
        """Anything that would stop this configuration from booting."""
        bad = []
        if not self.by_key['kernel'].value:
            bad.append('no kernel found -- press g to fetch one from kernel.org')
        if not self.by_key['root'].value:
            bad.append('no root filesystem chosen: the kernel will panic')
        limit = self.by_key['limit'].value.strip()
        if limit and not limit.isdigit():
            bad.append('instruction limit %r is not a number' % limit)
        return bad


# ---------------------------------------------------------------- curses front end
TITLE = 'pyvm  --  RISC-V RV64GC virtual machine in Python'


def _edit(win, y, x, width, value):
    """Read a line in place.  Enter accepts, Esc cancels, returns None then."""
    import curses
    buf = list(value)
    curses.curs_set(1)
    try:
        while True:
            text = ''.join(buf)[-width:]
            win.addstr(y, x, text.ljust(width)[:width], curses.A_REVERSE)
            win.move(y, x + min(len(text), width - 1))
            win.refresh()
            c = win.getch()
            if c in (10, 13, curses.KEY_ENTER):
                return ''.join(buf)
            if c == 27:
                return None
            if c in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
            elif c == curses.KEY_RESIZE:
                return ''.join(buf)
            elif 32 <= c < 127:
                buf.append(chr(c))
    finally:
        curses.curs_set(0)


def _shell_out(win, fn):
    """Hand the terminal back, run fn on it, then restore the menu."""
    import curses
    try:
        curses.def_prog_mode()
        curses.endwin()
    except curses.error:
        pass                          # not a real screen: nothing to hand back
    try:
        return fn()
    finally:
        try:
            curses.reset_prog_mode()
        except curses.error:
            pass
        win.clearok(True)
        win.refresh()


def _pair(n):
    """A colour attribute, or plain text where colours are unavailable."""
    import curses
    try:
        return curses.color_pair(n)
    except (curses.error, ValueError):
        return 0


def _draw(win, menu):
    import curses
    win.erase()
    h, w = win.getmaxyx()
    bold = curses.A_BOLD
    dim = curses.A_DIM
    win.addnstr(0, 0, TITLE.ljust(w), w - 1, bold | _pair(1))
    labelw = max(len(f.label) for f in menu.fields) + 2
    valuew = 22
    for i, f in enumerate(menu.fields):
        y = 2 + i
        if y >= h - 4:
            break
        here = i == menu.cursor
        attr = curses.A_REVERSE if here else curses.A_NORMAL
        win.addnstr(y, 1, f.label.ljust(labelw), labelw, dim if not here else attr)
        win.addnstr(y, 1 + labelw, f.show().ljust(valuew), valuew,
                    attr | (bold if here else 0))
        room = w - (2 + labelw + valuew) - 1
        if room > 4:
            win.addnstr(y, 2 + labelw + valuew, f.detail(menu), room, dim)
    # the command this menu stands for, so the flags stay discoverable
    argv = menu.argv()
    if argv is not None and h > 7:
        line = 'python3 -m pyvm ' + ' '.join(argv)
        win.addnstr(h - 4, 1, line[:w - 2], w - 2, dim)
    if h > 6:
        note = menu.message or menu.fields[menu.cursor].help
        win.addnstr(h - 3, 1, note[:w - 2], w - 2,
                    _pair(2) if menu.message else dim)
    if h > 5:
        win.addnstr(h - 2, 0, KEYS.ljust(w), w - 1, _pair(1))
    win.refresh()


def _loop(win, menu):
    """-> argv to boot with, or None if the user quit."""
    import curses
    try:
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
    except curses.error:
        pass
    while True:
        _draw(win, menu)
        try:
            c = win.getch()
        except KeyboardInterrupt:
            return None
        menu.message = ''
        f = menu.fields[menu.cursor]
        if c in (curses.KEY_UP, ord('k')):
            menu.cursor = (menu.cursor - 1) % len(menu.fields)
        elif c in (curses.KEY_DOWN, ord('j'), 9):
            menu.cursor = (menu.cursor + 1) % len(menu.fields)
        elif c in (curses.KEY_LEFT, ord('h')):
            f.step(-1)
        elif c in (curses.KEY_RIGHT, ord('l'), ord(' ')):
            if f.step(1) == 'edit':
                c = ord('e')
        elif c in (ord('q'), 27):
            return None
        elif c in (10, 13, curses.KEY_ENTER):
            bad = menu.problems()
            if bad:
                menu.message = bad[0]
                continue
            menu.probe.save(menu.snapshot())
            return menu.argv()
        elif c in (ord('g'), ord('G')):
            image, note = _shell_out(win, lambda: get_kernel(menu.root))
            if image:
                menu.rescan(select=image)
            menu.message = note
        elif c == ord('s'):
            menu.probe.save(menu.snapshot())
            menu.message = 'saved as the default for next time'
        if c == ord('e') and isinstance(f, Line):
            h, w = win.getmaxyx()
            labelw = max(len(x.label) for x in menu.fields) + 2
            got = _edit(win, 2 + menu.cursor, 1 + labelw, 22, f.value)
            if got is not None:
                f.value = got


def curses_menu(root='.'):
    import curses
    menu = Menu(root)
    return curses.wrapper(_loop, menu)


# ---------------------------------------------------------------- plain fallback
def text_menu(root='.'):
    """Used when curses will not start (a dumb terminal, say)."""
    menu = Menu(root)
    while True:
        print('\n%s\n' % TITLE)
        for i, f in enumerate(menu.fields, 1):
            print('  %2d) %-20s %-24s %s' % (i, f.label, f.show(), f.detail(menu)[:40]))
        argv = menu.argv()
        print('\n  python3 -m pyvm %s' % (' '.join(argv) if argv else '(incomplete)'))
        for bad in menu.problems():
            print('  ! %s' % bad)
        try:
            answer = input('\n  number to change, g for another kernel, '
                           'Enter to boot, q to quit: ').strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if answer.lower() in ('q', 'quit'):
            return None
        if answer.lower() == 'g':
            image, note = get_kernel(menu.root)
            if image:
                menu.rescan(select=image)
            if note:
                print('  %s' % note)
            continue
        if not answer:
            if menu.problems():
                continue
            menu.probe.save(menu.snapshot())
            return menu.argv()
        if not answer.isdigit() or not 1 <= int(answer) <= len(menu.fields):
            continue
        f = menu.fields[int(answer) - 1]
        if isinstance(f, Line):
            try:
                f.value = input('  %s (now %r): ' % (f.label, f.value)).strip()
            except (EOFError, KeyboardInterrupt):
                return None
        elif isinstance(f, Pick):
            for i, (_, detail) in enumerate(f.options, 1):
                mark = '*' if i - 1 == f.index else ' '
                print('      %s %d) %s' % (mark, i, detail))
            try:
                pick = input('      choose: ').strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if pick.isdigit() and 1 <= int(pick) <= len(f.options):
                f.index = int(pick) - 1
        else:
            f.step(1)


def choose(root='.'):
    """Show the boot menu; return the argv to run, or None if cancelled."""
    try:
        import curses                                          # noqa: F401
    except ImportError:
        return text_menu(root)
    if os.environ.get('TERM', 'dumb') in ('', 'dumb', 'unknown'):
        return text_menu(root)
    try:
        return curses_menu(root)
    except Exception:
        # a terminal curses cannot drive should not stop the VM from starting
        return text_menu(root)
