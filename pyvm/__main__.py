"""pyvm command line:  python -m pyvm --kernel Image [--initrd rootfs.cpio] ..."""

import argparse
import sys
import time

from . import net as netbackend
from .console import Console
from .machine import Machine
from .errors import VMError


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog='python -m pyvm',
        description='RISC-V RV64GC virtual machine written in Python. Boots Linux.',
        epilog='network backends:\n' + netbackend.describe_options(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('-k', '--kernel', help='Linux kernel: arch/riscv/boot/Image (or a vmlinux ELF)')
    p.add_argument('-i', '--initrd', help='initramfs / initrd image')
    p.add_argument('-d', '--disk', help='raw disk image, appears as /dev/vda')
    p.add_argument('--disk-ro', action='store_true', help='attach the disk read-only')
    p.add_argument('-a', '--append', help='kernel command line (replaces the default)')
    p.add_argument('-m', '--mem', type=int, default=128, help='RAM size in MiB (default 128)')
    p.add_argument('--dtb', help='use this device tree blob instead of the generated one')
    p.add_argument('--dump-dtb', metavar='FILE', help='write the generated device tree and exit')
    p.add_argument('-n', '--net', default='user', metavar='SPEC',
                   help='network backend: user[,OPT=VAL...], tap[,...] or none '
                        '(default: user); see below')
    p.add_argument('--net-dump', metavar='FILE',
                   help='write every frame the NIC sees to FILE in pcap format')
    p.add_argument('--bios', help='M-mode firmware (e.g. OpenSBI fw_jump.bin) instead of the '
                                  'built-in Python SBI')
    p.add_argument('--bare', metavar='ELF', help='run a bare-metal program in M-mode '
                                                 '(e.g. riscv-tests); exit code = test result')
    p.add_argument('--no-realtime', action='store_true',
                   help='deterministic time: idle periods are skipped instead of slept')
    p.add_argument('--max-insns', type=int, help='stop after this many instructions')
    p.add_argument('--stats', action='store_true', help='print instruction count and speed at exit')
    a = p.parse_args(argv)
    if not (a.kernel or a.bare):
        p.error('need --kernel (Linux) or --bare (bare-metal program)')
    return a


def make_net(a):
    """Build the network backend, or None when there is to be no NIC."""
    if a.bare or a.net is None:
        return None, None
    backend = netbackend.create(a.net)
    if backend is None:
        return None, None
    dump = None
    if a.net_dump:
        from .net.pcap import PcapWriter
        dump = PcapWriter(a.net_dump)
    return backend, dump


def default_bootargs(a):
    args = 'console=ttyS0 earlycon=sbi'
    if a.disk and not a.initrd:
        args += ' root=/dev/vda rw'
    return args


def read(path):
    with open(path, 'rb') as f:
        return f.read()


def main(argv=None):
    a = parse_args(sys.argv[1:] if argv is None else argv)
    console = Console()
    code = 1
    m = None
    netstats = None
    t0 = time.monotonic()
    try:
        backend, dump = make_net(a)
        m = Machine(console, mem_mb=a.mem, disk=a.disk, disk_readonly=a.disk_ro,
                    realtime=not a.no_realtime, net=backend, net_dump=dump)
        if a.bare:
            m.boot_bare(read(a.bare))
        else:
            bootargs = a.append if a.append is not None else default_bootargs(a)
            if a.dump_dtb:
                m.boot_linux(read(a.kernel), read(a.initrd) if a.initrd else None, bootargs)
                blob = m.dtb_blob
                with open(a.dump_dtb, 'wb') as f:
                    f.write(blob)
                console.close()
                print('wrote %s (%d bytes)' % (a.dump_dtb, len(blob)))
                return 0
            m.boot_linux(read(a.kernel), read(a.initrd) if a.initrd else None, bootargs,
                         dtb=read(a.dtb) if a.dtb else None,
                         bios=read(a.bios) if a.bios else None)
            if console.tty:
                sys.stderr.write('pyvm: RISC-V RV64GC, %d MiB RAM. Press Ctrl-A X to quit.\r\n'
                                 % a.mem)
                if m.net is not None:
                    sys.stderr.write('pyvm: %s\r\n' % backend.describe())
        code = m.run(a.max_insns)
        reason = getattr(m, 'halt_reason', '')
        if not a.bare or a.stats:
            sys.stderr.write('\r\npyvm: %s (exit code %d)\r\n' % (reason, code))
    except VMError as e:
        console.flush()
        sys.stderr.write('\r\npyvm: fatal: %s\r\n' % e)
        if m is not None:
            sys.stderr.write(m.cpu.dump().replace('\n', '\r\n') + '\r\n')
        code = 1
    except KeyboardInterrupt:
        sys.stderr.write('\r\npyvm: interrupted\r\n')
        code = 130
    finally:
        if m is not None:
            netstats = m.net.stats() if m.net is not None else None
            m.close()
        console.close()
    if a.stats and m is not None:
        dt = time.monotonic() - t0
        n = m.stats_insns
        sys.stderr.write('pyvm: %d instructions in %.1f s (%.2f MIPS)\n'
                         % (n, dt, n / dt / 1e6 if dt else 0))
        if netstats:
            sys.stderr.write('pyvm: %s\n' % netstats)
    return code


if __name__ == '__main__':
    sys.exit(main())
