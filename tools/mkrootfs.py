#!/usr/bin/env python3
"""Build a root filesystem for pyvm without root privileges.

Outputs an initramfs (newc cpio, written in pure Python) and/or an ext4 disk
image (via `mke2fs -d`, from e2fsprogs).

Examples:
    # busybox initramfs  ->  python -m pyvm -k Image -i rootfs.cpio
    python tools/mkrootfs.py --busybox busybox.static --cpio rootfs.cpio

    # busybox ext4 disk  ->  python -m pyvm -k Image -d disk.img
    python tools/mkrootfs.py --busybox busybox.static --ext4 disk.img --size 64

    # add your own files (e.g. cross-compiled programs) on top
    python tools/mkrootfs.py --busybox busybox.static --dir myfiles --cpio rootfs.cpio

Device nodes (/dev/console, /dev/null) are written straight into the cpio
archive, so no mknod is needed; on the ext4 disk the kernel's devtmpfs
provides them.
"""

import argparse
import gzip
import os
import shutil
import stat
import subprocess
import sys
import tempfile

INIT_SCRIPT = r'''#!/bin/busybox sh
/bin/busybox mkdir -p /proc /sys /dev /tmp /run /root /mnt /sbin /usr/bin /usr/sbin
[ -e /bin/ls ] || /bin/busybox --install -s
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null
mount -t tmpfs tmpfs /tmp
hostname pyvm
export HOME=/root PATH=/sbin:/usr/sbin:/bin:/usr/bin PS1='\u@\h:\w\$ '
cd /root
# Networking: pyvm's virtio-net appears as eth0.  Ask its built-in DHCP server
# for an address; with "--net none" there is no eth0 and this is skipped.
ifconfig lo 127.0.0.1 netmask 255.0.0.0 up 2>/dev/null
if [ -d /sys/class/net/eth0 ]; then
    ip link set eth0 up 2>/dev/null
    if udhcpc -i eth0 -q -n -t 5 -T 1 -s /usr/share/udhcpc/default.script \
            >/dev/null 2>&1; then
        net="eth0 $(ifconfig eth0 | sed -n 's/.*inet addr:\([0-9.]*\).*/\1/p')"
    else
        net="eth0 (no DHCP lease)"
    fi
else
    net="none"
fi
# Say plainly whether files written here will still be here next boot: an
# initramfs root lives in RAM, a /dev/vda root is the disk image on the host.
rootdev=$(awk '$2 == "/" { print $1; exit }' /proc/mounts)
case "$rootdev" in
    /dev/*) store="$rootdev - changes are saved" ;;
    *)      store="initramfs (RAM) - changes are LOST at poweroff; boot with --disk to keep them" ;;
esac
echo
echo "Welcome to Linux on pyvm (RISC-V RV64GC emulated in Python)"
uname -a
echo "Network: $net"
echo "Storage: $store"
echo "Type 'exit' or 'poweroff' to shut down."
# Run the shell on the real console device so it gets a controlling tty (job
# control, Ctrl-C).  It runs as a child: this script is PID 1, and PID 1 must
# never exit (the kernel panics), so when the shell exits we power off cleanly.
con=$(cat /sys/class/tty/console/active 2>/dev/null)
con=${con##* }
[ -c "/dev/$con" ] || con=console
# busybox poweroff/halt/reboot (without -f) ask PID 1 via a signal
trap 'sync; poweroff -f' USR1 USR2
trap 'sync; reboot -f' TERM
setsid sh -c "exec sh -l </dev/$con >/dev/$con 2>&1" &
wait $!                  # (a foreground command would delay the traps)
echo "Shell exited, powering off."
sync
poweroff -f
'''

UDHCPC_SCRIPT = r'''#!/bin/sh
# Called by busybox udhcpc to apply (or drop) a lease.  $1 is the event.
[ -n "$1" ] || { echo "udhcpc: called with no action" >&2; exit 1; }
case "$1" in
    deconfig)
        ifconfig "$interface" 0.0.0.0
        ;;
    renew | bound)
        ifconfig "$interface" "$ip" netmask "${subnet:-255.255.255.0}"
        if [ -n "$router" ]; then
            while route del default gw 0.0.0.0 dev "$interface" 2>/dev/null; do :; done
            for r in $router; do
                route add default gw "$r" dev "$interface"
            done
        fi
        : > /etc/resolv.conf
        [ -n "$domain" ] && echo "search $domain" >> /etc/resolv.conf
        for s in $dns; do
            echo "nameserver $s" >> /etc/resolv.conf
        done
        ;;
    leasefail | nak)
        echo "udhcpc: $1" >&2
        ;;
esac
exit 0
'''

RESOLV_CONF = b"""# pyvm's user-mode networking answers DNS on 10.0.2.3.
# udhcpc rewrites this file from the DHCP lease.
nameserver 10.0.2.3
"""

HOSTS = b"""127.0.0.1\tlocalhost
::1\t\tlocalhost
10.0.2.15\tpyvm
10.0.2.2\thost gateway
"""

DIRS = ('bin', 'sbin', 'dev', 'etc', 'proc', 'sys', 'tmp', 'root', 'mnt', 'run',
        'usr', 'usr/bin', 'usr/sbin', 'usr/share', 'usr/share/udhcpc')


# ---------------------------------------------------------------------- cpio
class CpioWriter:
    def __init__(self):
        self.out = bytearray()
        self.ino = 721

    def _entry(self, name, mode, data=b'', nlink=1, rdev=(0, 0)):
        nb = (name.lstrip('/') or '.').encode() + b'\0'
        self.ino += 1
        fields = [self.ino, mode, 0, 0, nlink, 0, len(data), 0, 0, rdev[0], rdev[1], len(nb), 0]
        self.out += b'070701' + b''.join(b'%08X' % f for f in fields) + nb
        self.out += b'\0' * (-len(self.out) % 4)
        self.out += data
        self.out += b'\0' * (-len(self.out) % 4)

    def dir(self, name, perm=0o755):
        self._entry(name, stat.S_IFDIR | perm, nlink=2)

    def file(self, name, data, perm=0o644):
        self._entry(name, stat.S_IFREG | perm, data)

    def symlink(self, name, target):
        self._entry(name, stat.S_IFLNK | 0o777, target.encode())

    def chardev(self, name, major, minor, perm=0o600):
        self._entry(name, stat.S_IFCHR | perm, rdev=(major, minor))

    def finish(self):
        self._entry('TRAILER!!!', 0)
        self.out += b'\0' * (-len(self.out) % 512)
        return bytes(self.out)


# ---------------------------------------------------------------------- directory
class DirWriter:
    """Same interface as CpioWriter, but materialises the tree on disk."""

    def __init__(self, root):
        self.root = root

    def _p(self, name):
        return os.path.join(self.root, name.lstrip('/'))

    def dir(self, name, perm=0o755):
        os.makedirs(self._p(name), exist_ok=True)
        os.chmod(self._p(name), perm)

    def file(self, name, data, perm=0o644):
        with open(self._p(name), 'wb') as f:
            f.write(data)
        os.chmod(self._p(name), perm)

    def symlink(self, name, target):
        if os.path.lexists(self._p(name)):
            os.remove(self._p(name))
        os.symlink(target, self._p(name))

    def chardev(self, name, major, minor, perm=0o600):
        pass            # needs root; devtmpfs provides /dev on a disk root


def add_tree(w, root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel = os.path.relpath(dirpath, root)
        if rel != '.':
            w.dir(rel, stat.S_IMODE(os.lstat(dirpath).st_mode))
        links = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        for fn in sorted(filenames + links):
            full = os.path.join(dirpath, fn)
            name = os.path.normpath(os.path.join(rel, fn))
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                w.symlink(name, os.readlink(full))
            elif stat.S_ISREG(st.st_mode):
                with open(full, 'rb') as f:
                    w.file(name, f.read(), stat.S_IMODE(st.st_mode))


def populate(w, a):
    for d in DIRS:
        w.dir(d, 0o1777 if d == 'tmp' else 0o755)
    w.chardev('dev/console', 5, 1)
    w.chardev('dev/null', 1, 3, 0o666)
    if a.busybox:
        with open(a.busybox, 'rb') as f:
            w.file('bin/busybox', f.read(), 0o755)
        w.symlink('bin/sh', 'busybox')
        init = INIT_SCRIPT
        if a.init:
            with open(a.init) as f:
                init = f.read()
        w.file('init', init.encode(), 0o755)          # initramfs entry point
        w.symlink('sbin/init', '../init')              # disk root entry point
        w.file('etc/passwd', b'root:x:0:0:root:/root:/bin/sh\n')
        w.file('etc/group', b'root:x:0:\n')
        w.file('etc/hostname', b'pyvm\n')
        w.file('etc/hosts', HOSTS)
        w.file('etc/resolv.conf', RESOLV_CONF)
        w.file('usr/share/udhcpc/default.script', UDHCPC_SCRIPT.encode(), 0o755)
    for d in a.dir:
        add_tree(w, d)


def chown_root(image, tree):
    """mke2fs -d copies the host user's uid/gid; make every file root-owned."""
    debugfs = shutil.which('debugfs') or '/sbin/debugfs'
    cmds = []
    for dirpath, dirnames, filenames in os.walk(tree):
        for name in [''] + dirnames + filenames:
            path = '/' + os.path.relpath(os.path.join(dirpath, name), tree)
            path = os.path.normpath(path)
            cmds += ['sif "%s" uid 0' % path, 'sif "%s" gid 0' % path]
    with tempfile.NamedTemporaryFile('w', suffix='.debugfs') as f:
        f.write('\n'.join(sorted(set(cmds))) + '\n')
        f.flush()
        subprocess.run([debugfs, '-w', '-f', f.name, image], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--busybox', help='statically linked riscv64 busybox (see tools/get_busybox.py)')
    p.add_argument('--init', help='custom init script (default: start a busybox shell)')
    p.add_argument('--dir', action='append', default=[], help='extra directory tree to add (repeatable)')
    p.add_argument('--cpio', metavar='FILE', help='write an initramfs (newc cpio)')
    p.add_argument('--gzip', action='store_true', help='gzip the initramfs')
    p.add_argument('--ext4', metavar='FILE', help='write an ext4 disk image (needs mke2fs)')
    p.add_argument('--size', type=int, default=64, help='ext4 image size in MiB (default 64)')
    a = p.parse_args()
    if not (a.busybox or a.dir):
        p.error('need --busybox and/or --dir')
    if not (a.cpio or a.ext4):
        p.error('need --cpio and/or --ext4')

    if a.cpio:
        w = CpioWriter()
        populate(w, a)
        data = w.finish()
        if a.gzip:
            data = gzip.compress(data, 9)
        with open(a.cpio, 'wb') as f:
            f.write(data)
        print('wrote %s (%d KiB)' % (a.cpio, len(data) >> 10))

    if a.ext4:
        mke2fs = shutil.which('mke2fs') or '/sbin/mke2fs'
        tmp = tempfile.mkdtemp(prefix='pyvm-rootfs-')
        try:
            populate(DirWriter(tmp), a)
            if os.path.exists(a.ext4):
                os.remove(a.ext4)
            subprocess.run([mke2fs, '-q', '-t', 'ext4', '-E', 'root_owner=0:0', '-d', tmp,
                            a.ext4, '%dM' % a.size], check=True)
            chown_root(a.ext4, tmp)
        finally:
            shutil.rmtree(tmp)
        print('wrote %s (%d MiB ext4)' % (a.ext4, a.size))


if __name__ == '__main__':
    sys.exit(main())
