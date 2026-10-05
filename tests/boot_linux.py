#!/usr/bin/env python3
"""Boot Linux on pyvm headlessly, run some shell commands and check the output.

    python tests/boot_linux.py --kernel Image --initrd rootfs.cpio [--disk disk.img]
    python tests/boot_linux.py --kernel Image --initrd rootfs.cpio --net

With --net the guest's network is exercised too: DHCP, ping, a TCP download from
a web server started on the host, and an inbound connection through a forwarded
port.  That needs no outside network; add --net-external to also resolve a name
and fetch a page over the internet.

Exit status 0 when every check passes.
"""

import argparse
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from pyvm.console import BufferConsole          # noqa: E402
from pyvm.machine import Machine                # noqa: E402

PROMPT = b'# '

CHECKS = [
    ('uname -m', 'riscv64'),
    ('cat /proc/cpuinfo | grep isa', 'rv64imafdc'),
    ('echo $((6 * 7))', '42'),
    ("awk 'BEGIN { printf \"%.4f\\n\", atan2(0, -1) }'", '3.1416'),         # userspace FP
    ('echo hello pyvm | sed s/pyvm/world/ | tr a-z A-Z', 'HELLO WORLD'),
    ('dd if=/dev/zero bs=1024 count=64 2>/dev/null | md5sum', 'fcd6bcb56c1689fcef28b57c22475bad'),
    ('sleep 1 && echo slept', 'slept'),
    ('echo bg-job & wait; echo fg-done', 'fg-done'),                          # job control
    ('ls /proc/self/ | grep -c status', '1'),
    ('cat /sys/class/rtc/rtc0/name', 'goldfish'),
    # The board has no hardware RNG, so pyvm seeds the kernel through the device
    # tree.  If that regresses, the CRNG never initialises and anything calling
    # getrandom() -- OpenSSL, so curl and ssh -- hangs forever with no output.
    ('test $(cat /proc/sys/kernel/random/entropy_avail) -ge 256 && echo seeded', 'seeded'),
    ('head -c 16 /dev/random >/dev/null && echo random-ok', 'random-ok'),
    # kallsyms still works after init memory is freed (skipped if not configured)
    ('if [ -e /proc/kallsyms ]; then head -n 3 /proc/kallsyms | wc -l; else echo 3; fi', '3'),
]

DISK_CHECKS = [   # leaves the disk unchanged
    ('mount /dev/vda /mnt && ls /mnt/bin/busybox', '/mnt/bin/busybox'),
    ('echo written-by-guest > /mnt/.pyvm-test && sync && cat /mnt/.pyvm-test && rm /mnt/.pyvm-test'
     ' && umount /mnt && echo unmounted', 'unmounted'),
]


NET_CHECKS = [
    ('ifconfig eth0 | grep "inet addr"', 'inet addr:10.0.2.15'),
    ('ip route | grep default', 'default via 10.0.2.2'),
    ('cat /etc/resolv.conf | grep nameserver', 'nameserver 10.0.2.3'),
    ('ping -c 2 -W 2 10.0.2.2 | grep "packets received"', '2 packets received'),
]

EXTERNAL_CHECKS = [
    ('nslookup one.one.one.one 2>&1 | grep -c "1.1.1.1\\|1.0.0.1"', '1'),
    ('wget -q -O - http://1.1.1.1/ 2>&1 | head -c 200 | grep -ic "html\\|cloudflare"', '1'),
    # by name: the DNS forwarder and outbound TCP working together, as wget uses them
    ('wget -q -O - http://example.com/ | grep -ic "<title>"', '1'),
    ('wget -q -O /dev/null http://ftp.debian.org/debian/README && echo fetched', 'fetched'),
]


class HostHTTP(threading.Thread):
    """A one-page web server on the host, reachable from the guest as 10.0.2.2."""

    BODY = b'pyvm-http-ok'

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        self.start()

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(4096)
                    self.hits += 1
                    conn.sendall(b'HTTP/1.0 200 OK\r\nContent-Length: %d\r\n'
                                 b'Content-Type: text/plain\r\n\r\n%s'
                                 % (len(self.BODY), self.BODY))
                except OSError:
                    pass


class HostClient(threading.Thread):
    """Connects to a forwarded port, i.e. into the guest, and keeps the reply."""

    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self.reply = None
        self.error = None

    def run(self):
        for _ in range(40):
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=10) as c:
                    c.sendall(b'hello-guest\n')
                    data = b''
                    while True:
                        chunk = c.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    if data:
                        self.reply = data
                        return
                    # accepted but reset: the guest is not listening yet
            except OSError as e:
                self.error = e
            time.sleep(0.5)


class Driver(threading.Thread):
    def __init__(self, con, checks, timeout):
        super().__init__(daemon=True)
        self.con = con
        self.checks = checks
        self.timeout = timeout
        self.results = []
        self.failed = False
        self.prompt_time = None

    def wait_for(self, token, start, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            i = self.con.output.find(token, start)
            if i >= 0:
                return i + len(token)
            time.sleep(0.05)
        return -1

    on_prompt = None

    def run(self):
        t0 = time.time()
        pos = self.wait_for(PROMPT, 0, self.timeout)
        self.prompt_time = time.time() - t0
        if pos < 0:
            self.failed = True
            self.results.append(('<boot to shell prompt>', False, 0))
            self.con.quit_requested = True
            return
        for n, (cmd, expect) in enumerate(self.checks):
            if self.on_prompt is not None and cmd.startswith('nc -l'):
                self.on_prompt()                  # the host dials in while nc listens
            # End every command with a marker the shell has to compute, so the
            # echoed input line or a stray prompt can never be mistaken for it.
            marker = ('@@%d@@' % (1000 + n)).encode()
            t0 = time.time()
            self.con.feed(('%s; echo "@@$((1000+%d))@@"\n' % (cmd, n)).encode())
            end = self.wait_for(marker, pos, 300)
            out = self.con.output[pos:end].decode(errors='replace') if end > 0 else ''
            ok = end > 0 and expect in out.split('\n', 1)[-1]
            self.results.append((cmd, ok, time.time() - t0))
            if not ok:
                self.failed = True
                sys.stderr.write('--- output of %r:\n%s\n' % (cmd, out))
            pos = end if end > 0 else len(self.con.output)
        self.con.feed(b'poweroff -f\n')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--kernel', required=True)
    p.add_argument('--initrd')
    p.add_argument('--disk')
    p.add_argument('--mem', type=int, default=128)
    p.add_argument('--net', action='store_true',
                   help='attach user-mode networking and check it (needs a kernel '
                        'with CONFIG_VIRTIO_NET)')
    p.add_argument('--net-external', action='store_true',
                   help='with --net, also check DNS and a fetch from the internet')
    p.add_argument('--timeout', type=int, default=3600, help='seconds allowed to reach the prompt')
    p.add_argument('-v', '--verbose', action='store_true', help='print the console output')
    a = p.parse_args()

    con = BufferConsole()
    http = fwd_port = backend = None
    netkw = {}
    if a.net or a.net_external:
        from pyvm import net as netbackend                               # noqa: E402
        http = HostHTTP()
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        fwd_port = probe.getsockname()[1]
        probe.close()
        backend = netbackend.create('user,hostfwd=tcp:127.0.0.1:%d-:5000' % fwd_port)
        netkw['net'] = backend
    m = Machine(con, mem_mb=a.mem, disk=a.disk, **netkw)
    append = 'console=ttyS0 earlycon=sbi' + (' root=/dev/vda rw' if a.disk and not a.initrd else '')
    m.boot_linux(open(a.kernel, 'rb').read(),
                 open(a.initrd, 'rb').read() if a.initrd else None, append)
    if a.disk and a.initrd:
        checks = CHECKS + DISK_CHECKS                 # disk used as a data disk
    elif a.disk:
        checks = CHECKS + [('grep " / " /proc/mounts', '/dev/root / ext4')]   # disk is the root fs
    else:
        checks = CHECKS
    client = None
    if backend is not None:
        checks = checks + NET_CHECKS + [
            # the gateway address stands for the host, where HostHTTP is listening
            ('wget -q -O - http://10.0.2.2:%d/' % http.port, 'pyvm-http-ok'),
        ]
        if a.net_external:
            checks = checks + EXTERNAL_CHECKS
        client = HostClient(fwd_port)
        checks = checks + [
            # serve one connection on :5000; the host reaches it through hostfwd
            ('nc -l -p 5000 -e /bin/echo guest-says-hi >/dev/null 2>&1; echo served',
             'served'),
        ]
    drv = Driver(con, checks, a.timeout)
    if client is not None:
        drv.on_prompt = client.start
    t0 = time.time()
    drv.start()
    code = m.run()
    wall = time.time() - t0
    con.flush()
    if a.verbose:
        sys.stdout.write(con.output.decode(errors='replace'))
    print('\n==== pyvm boot test (%s)' % m.halt_reason)
    if drv.prompt_time is not None:
        print('  boot to shell prompt: %.1f s' % drv.prompt_time)
    for cmd, ok, dt in drv.results:
        print('  [%s] %-60s %6.1fs' % ('PASS' if ok else 'FAIL', cmd[:60], dt))
    n = m.cpu.instret
    print('  %d instructions, %.1f s wall, %.2f MIPS' % (n, wall, n / wall / 1e6))
    netok = True
    if backend is not None:
        if client is not None:
            client.join(timeout=10)
            netok = client.reply is not None and b'guest-says-hi' in client.reply
            print('  [%s] %-60s' % ('PASS' if netok else 'FAIL',
                                    'host -> guest through the forwarded port'))
            if not netok:
                print('         reply=%r error=%s' % (client.reply, client.error))
        print('  %s' % m.net.stats())
        if backend.errors:
            print('  %d network stack errors' % backend.errors)
            netok = False
        m.close()
    ok = not drv.failed and len(drv.results) == len(checks) and code == 0 and netok
    print('  RESULT: %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
