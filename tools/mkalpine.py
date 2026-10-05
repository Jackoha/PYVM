#!/usr/bin/env python3
"""Build an Alpine Linux root filesystem for pyvm, with a working package manager.

The busybox image that `mkrootfs.py` builds is deliberately tiny and has no way
to install anything.  This builds a real (if small) distribution instead: Alpine
riscv64, which brings `apk`, so inside the guest you can

    apk update && apk add curl jq git python3

Packages named on the command line are unpacked at build time, so they work on
the very first boot without needing the network:

    python3 tools/mkalpine.py --ext4 alpine.img            # curl is preinstalled
    python3 tools/mkalpine.py --ext4 alpine.img --pkg jq --pkg tmux
    python3 tools/mkalpine.py --cpio alpine.cpio --size 512

Then boot it like any other pyvm disk:

    python3 -m pyvm -k Image -d alpine.img

Everything comes from Alpine's CDN over HTTPS; the .apk files are signed, and
the root filesystem tarball is checked against its published sha256.
"""

import argparse
import collections
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkrootfs import (CpioWriter, DirWriter, INIT_SCRIPT, UDHCPC_SCRIPT,     # noqa: E402
                      HOSTS, add_tree, chown_root)

MIRROR = 'https://dl-cdn.alpinelinux.org/alpine'
BRANCH = 'v3.20'
ARCH = 'riscv64'
DEFAULT_PKGS = ['curl', 'ca-certificates']

REPOSITORIES = """\
{mirror}/{branch}/main
{mirror}/{branch}/community
"""

RESOLV = b"""# pyvm's user-mode networking answers DNS on 10.0.2.3.
# udhcpc rewrites this file from the DHCP lease.
nameserver 10.0.2.3
"""

# Alpine's own inittab expects OpenRC, which a minirootfs does not have; pyvm
# supplies its own /init instead (see mkrootfs.py) and this keeps busybox init
# from fighting it if anything ever runs it.
INITTAB = b"""::sysinit:/init
::respawn:/sbin/getty -L ttyS0 115200 vt100
::ctrlaltdel:/sbin/reboot
::shutdown:/bin/umount -a -r
"""


def fetch(url, what, timeout=180):
    sys.stderr.write('  fetching %s\n' % what)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- apk index
def load_index(repo):
    """Parse an Alpine APKINDEX into (packages, provides-map)."""
    raw = fetch(repo + '/APKINDEX.tar.gz', 'package index for %s' % repo.rsplit('/', 2)[-2])
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as t:
        text = t.extractfile('APKINDEX').read().decode('utf-8', 'replace')
    pkgs, cur = [], {}
    for line in text.split('\n'):
        if not line:
            if cur:
                pkgs.append(cur)
                cur = {}
            continue
        key, _, value = line.partition(':')
        cur.setdefault(key, []).append(value)
    if cur:
        pkgs.append(cur)
    provides = {}
    for p in pkgs:
        p['repo'] = repo
        provides.setdefault(p['P'][0], p)
        for token in (p.get('p') or [''])[0].split():
            provides.setdefault(token.split('=')[0], p)
    return pkgs, provides


def bare(dep):
    """'so:libz.so.1=1' / 'musl>=1.2' -> the name we look up."""
    return re.split(r'[=<>~]', dep, 1)[0]


def resolve(roots, provides):
    """Every package needed for `roots`, dependencies first."""
    seen, chosen, queue = set(), {}, collections.deque(roots)
    missing = []
    while queue:
        dep = queue.popleft()
        if dep.startswith('!'):
            continue                            # a conflict marker, not a dependency
        name = bare(dep)
        if name in seen:
            continue
        seen.add(name)
        p = provides.get(name)
        if p is None:
            missing.append(name)
            continue
        chosen.setdefault(p['P'][0], p)
        queue.extend((p.get('D') or [''])[0].split())
    return list(chosen.values()), missing


def install_packages(tree, names, mirror, branch, arch):
    """Unpack packages (and their dependencies) into an already-extracted tree."""
    provides = {}
    for section in ('main', 'community'):
        repo = '%s/%s/%s/%s' % (mirror, branch, section, arch)
        _, prov = load_index(repo)
        for key, value in prov.items():
            provides.setdefault(key, value)
    pkgs, missing = resolve(names, provides)
    if missing:
        raise SystemExit('cannot resolve: %s' % ', '.join(sorted(missing)))
    pkgs.sort(key=lambda p: p['P'][0])
    total = sum(int(p['S'][0]) for p in pkgs)
    sys.stderr.write('  %d packages to unpack (%.1f MiB of downloads)\n'
                     % (len(pkgs), total / (1 << 20)))
    for p in pkgs:
        name, ver = p['P'][0], p['V'][0]
        data = fetch('%s/%s-%s.apk' % (p['repo'], name, ver), '%s %s' % (name, ver))
        extract_apk(data, tree)
    return pkgs


def inside(member, path=None):
    """Extraction filter: refuse members that would escape the tree.

    Python's own `tar` filter is too strict here -- it rejects symlinks with
    absolute targets, and a root filesystem is full of legitimate ones such as
    /bin/sh -> /bin/busybox, which resolve inside the guest, not the host."""
    name = member.name.lstrip('./')
    if name.startswith('/') or '..' in name.split('/'):
        return None
    return member


def untar(t, tree, members=None):
    members = list(t.getmembers() if members is None else members)
    members = [m for m in members if inside(m) is not None]
    try:
        t.extractall(tree, members=members, filter=inside)
    except TypeError:                           # Python < 3.12 has no filters
        t.extractall(tree, members=members)


def extract_apk(data, tree):
    """An .apk is concatenated gzip members: metadata (dot-files) then the files."""
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as t:
        untar(t, tree, [m for m in t.getmembers() if not m.name.startswith('.')])


# ---------------------------------------------------------------- root filesystem
def minirootfs_url(mirror, branch, arch):
    """Newest alpine-minirootfs for this branch, with its published sha256."""
    base = '%s/%s/releases/%s/' % (mirror, branch, arch)
    index = fetch(base, 'release list').decode('utf-8', 'replace')
    names = sorted(set(re.findall(r'href="(alpine-minirootfs-[\d.]+-%s\.tar\.gz)"'
                                  % arch, index)),
                   key=lambda n: [int(x) for x in re.findall(r'\d+', n)])
    if not names:
        raise SystemExit('no alpine-minirootfs found at ' + base)
    name = names[-1]
    want = None
    try:
        digest = fetch(base + name + '.sha256', 'checksum').decode().split()
        want = digest[0] if digest else None
    except Exception:
        sys.stderr.write('  (no published sha256 alongside %s)\n' % name)
    return base + name, name, want


def build_tree(tree, a):
    url, name, want = minirootfs_url(a.mirror, a.branch, a.arch)
    tar = fetch(url, '%s (~3 MiB)' % name)
    got = hashlib.sha256(tar).hexdigest()
    if want and got != want:
        raise SystemExit('sha256 mismatch for %s:\n  want %s\n  got  %s' % (name, want, got))
    sys.stderr.write('  sha256 %s %s\n' % (got[:16] + '...', 'verified' if want else '(unchecked)'))
    with tarfile.open(fileobj=io.BytesIO(tar), mode='r:gz') as t:
        untar(t, tree)

    pkgs = []
    if a.pkg:
        pkgs = install_packages(tree, a.pkg, a.mirror, a.branch, a.arch)

    # pyvm's own init: mounts, DHCP on eth0, a login shell on the console
    write(tree, 'init', INIT_SCRIPT.encode(), 0o755)
    link(tree, 'sbin/init', '../init')
    write(tree, 'usr/share/udhcpc/default.script', UDHCPC_SCRIPT.encode(), 0o755)
    write(tree, 'etc/inittab', INITTAB)
    write(tree, 'etc/hostname', b'pyvm\n')
    write(tree, 'etc/hosts', HOSTS)
    write(tree, 'etc/resolv.conf', RESOLV)
    write(tree, 'etc/apk/repositories',
          REPOSITORIES.format(mirror=a.mirror, branch=a.branch).encode())
    for d in ('proc', 'sys', 'dev', 'mnt', 'root'):
        os.makedirs(os.path.join(tree, d), exist_ok=True)
    for extra in a.dir:
        add_tree(DirWriter(tree), extra)
    return pkgs


def write(tree, path, data, mode=0o644):
    full = os.path.join(tree, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    if os.path.lexists(full):
        os.remove(full)
    with open(full, 'wb') as f:
        f.write(data)
    os.chmod(full, mode)


def link(tree, path, target):
    full = os.path.join(tree, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    if os.path.lexists(full):
        os.remove(full)
    os.symlink(target, full)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--ext4', metavar='FILE', help='write an ext4 disk image')
    p.add_argument('--cpio', metavar='FILE', help='write an initramfs instead (no apk writes)')
    p.add_argument('--size', type=int, default=512, help='ext4 image size in MiB (default 512)')
    p.add_argument('--pkg', action='append', default=None,
                   help='package to unpack at build time (repeatable; default: %s)'
                        % ' '.join(DEFAULT_PKGS))
    p.add_argument('--no-pkg', action='store_true', help='bare Alpine, install nothing')
    p.add_argument('--dir', action='append', default=[], help='extra tree to graft in')
    p.add_argument('--branch', default=BRANCH)
    p.add_argument('--arch', default=ARCH)
    p.add_argument('--mirror', default=MIRROR)
    a = p.parse_args()
    if not (a.ext4 or a.cpio):
        p.error('need --ext4 and/or --cpio')
    a.pkg = [] if a.no_pkg else (a.pkg if a.pkg is not None else list(DEFAULT_PKGS))

    tmp = tempfile.mkdtemp(prefix='pyvm-alpine-')
    try:
        sys.stderr.write('Building an Alpine %s %s root filesystem\n' % (a.branch, a.arch))
        pkgs = build_tree(tmp, a)
        if a.cpio:
            w = CpioWriter()
            add_tree(w, tmp)
            w.chardev('dev/console', 5, 1)
            w.chardev('dev/null', 1, 3, 0o666)
            data = w.finish()
            with open(a.cpio, 'wb') as f:
                f.write(data)
            print('wrote %s (%d KiB)' % (a.cpio, len(data) >> 10))
        if a.ext4:
            mke2fs = shutil.which('mke2fs') or '/sbin/mke2fs'
            if os.path.exists(a.ext4):
                os.remove(a.ext4)
            subprocess.run([mke2fs, '-q', '-t', 'ext4', '-E', 'root_owner=0:0',
                            '-d', tmp, a.ext4, '%dM' % a.size], check=True)
            chown_root(a.ext4, tmp)
            print('wrote %s (%d MiB ext4)' % (a.ext4, a.size))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if pkgs:
        print('preinstalled: %s' % ' '.join(sorted(p['P'][0] for p in pkgs)))
    print('boot it with: python3 -m pyvm -k Image %s %s'
          % ('-d' if a.ext4 else '-i', a.ext4 or a.cpio))
    print('then inside the guest: apk update && apk add <package>')


if __name__ == '__main__':
    main()
