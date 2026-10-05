"""The virtual board: RAM, devices, device tree, image loading and the main loop.

Memory map (same layout as QEMU's `virt` machine, so stock kernels just work):

    0x0010_0000  sifive,test0 (poweroff/reboot)
    0x0010_1000  goldfish RTC                 irq 11
    0x0200_0000  CLINT
    0x0C00_0000  PLIC
    0x1000_0000  NS16550A UART                irq 10
    0x1000_1000  virtio-mmio block device     irq 1
    0x1000_2000  virtio-mmio network device    irq 2
    0x8000_0000  RAM
"""

import mmap
import os
import struct
import time

from .cpu import CPU, RAM_BASE, M64, STIP, MTIP, S_INTS
from .devices import (UART16550, PLIC, CLINT, GoldfishRTC, TestDevice,
                      VirtioBlock, VirtioNet)
from .sbi import SBI
from . import fdt
from .errors import Halt, VMError

TIMEBASE = 10_000_000       # timer ticks per second (guest time: 1 tick per instruction)
BATCH = 4096                # instructions between device/interrupt polls
MB = 1 << 20

TEST_BASE, RTC_BASE, CLINT_BASE = 0x100000, 0x101000, 0x2000000
PLIC_BASE, UART_BASE = 0xC000000, 0x10000000
VIRTIO_BASE, NET_BASE = 0x10001000, 0x10002000       # virtio-mmio slots 1 and 2
UART_IRQ, RTC_IRQ, VIRTIO_IRQ, NET_IRQ = 10, 11, 1, 2


def align_up(v, a):
    return (v + a - 1) & ~(a - 1)


class Machine:
    def __init__(self, console, mem_mb=128, disk=None, disk_readonly=False, realtime=True,
                 net=None, net_dump=None):
        if mem_mb < 16:
            raise VMError('need at least 16 MiB of RAM')
        self.console = console
        self.ram_size = mem_mb * MB
        self.ram = mmap.mmap(-1, self.ram_size)
        self.realtime = realtime
        self.plic = None
        self.cpu = CPU(self)
        self.plic = PLIC(self)
        self.clint = CLINT(self)
        self.uart = UART16550(self, UART_IRQ)
        self.rtc = GoldfishRTC(self)
        self.test = TestDevice(self)
        devs = [(TEST_BASE, self.test), (RTC_BASE, self.rtc), (CLINT_BASE, self.clint),
                (PLIC_BASE, self.plic), (UART_BASE, self.uart)]
        self.disk = None
        if disk:
            self.disk = VirtioBlock(self, VIRTIO_IRQ, disk, disk_readonly)
            devs.append((VIRTIO_BASE, self.disk))
        self.net = None
        if net is not None:
            self.net = VirtioNet(self, NET_IRQ, net, net_dump)
            devs.append((NET_BASE, self.net))
            net.start(console.wake)
        self.mmio = [(base, base + dev.size, dev) for base, dev in devs]
        self.htif = None            # (tohost, fromhost) physical addresses for test programs
        self.bootargs = ''
        self.stats_insns = 0

    def close(self):
        """Release the devices' host resources (disk file, network sockets)."""
        for _, _, dev in self.mmio:
            close = getattr(dev, 'close', None)
            if close is not None:
                close()

    # ------------------------------------------------------------------ bus
    def mmio_read(self, pa, size):
        for base, end, dev in self.mmio:
            if base <= pa < end:
                return dev.read(pa - base, size) & ((1 << (8 * size)) - 1)
        return None

    def mmio_write(self, pa, size, v):
        for base, end, dev in self.mmio:
            if base <= pa < end:
                dev.write(pa - base, size, v)
                return True
        return False

    def phys_read(self, pa, n):
        o = pa - RAM_BASE
        if o < 0 or o + n > self.ram_size:
            return None
        return self.ram[o:o + n]

    def phys_write(self, pa, data):
        """DMA-style write into guest RAM (keeps the decoded-code cache coherent)."""
        n = len(data)
        o = pa - RAM_BASE
        if o < 0 or o + n > self.ram_size:
            return False
        if n:
            self.ram[o:o + n] = data
            cp = self.cpu.code_pages
            if cp:
                for rp in range(o >> 12, ((o + n - 1) >> 12) + 1):
                    if rp in cp:
                        self.cpu.invalidate_code(rp)
        return True

    def load_bytes(self, pa, data):
        if not self.phys_write(pa, data):
            raise VMError('image does not fit in RAM at %#x (%d bytes)' % (pa, len(data)))

    # ------------------------------------------------------------------ device tree
    def make_fdt(self, bootargs, initrd=None, syscon_reset=False):
        root = fdt.Node('')
        root.prop('#address-cells', 2).prop('#size-cells', 2)
        root.prop('compatible', fdt.string('pyvm,virt', 'riscv-virtio'))
        root.prop('model', 'pyvm RISC-V virtual machine')

        ch = root.child('chosen')
        ch.prop('bootargs', bootargs)
        # Seed the kernel's RNG the way a real bootloader does.  Without this
        # there is no entropy source on this board at all, the CRNG never
        # initialises, and anything calling getrandom() -- OpenSSL, and so
        # curl, ssh, python's secrets -- blocks forever.  Linux credits the
        # seed only when it trusts the bootloader, hence CONFIG_RANDOM_TRUST_
        # BOOTLOADER or random.trust_bootloader=1 on the command line.
        ch.prop('rng-seed', os.urandom(64))
        ch.prop('stdout-path', '/soc/serial@%x' % UART_BASE)
        if initrd:
            ch.prop('linux,initrd-start', fdt.u64(initrd[0]))
            ch.prop('linux,initrd-end', fdt.u64(initrd[1]))

        mem = root.child('memory@%x' % RAM_BASE)
        mem.prop('device_type', 'memory')
        mem.prop('reg', fdt.cells64(RAM_BASE, self.ram_size))

        cpus = root.child('cpus')
        cpus.prop('#address-cells', 1).prop('#size-cells', 0)
        cpus.prop('timebase-frequency', TIMEBASE)
        c0 = cpus.child('cpu@0')
        c0.prop('device_type', 'cpu').prop('reg', 0).prop('status', 'okay')
        c0.prop('compatible', 'riscv')
        c0.prop('riscv,isa', 'rv64imafdc_zicsr_zifencei')
        c0.prop('riscv,isa-base', 'rv64i')
        c0.prop('riscv,isa-extensions', fdt.string('i', 'm', 'a', 'f', 'd', 'c', 'zicsr', 'zifencei'))
        c0.prop('mmu-type', 'riscv,sv39')
        intc = c0.child('interrupt-controller')
        intc.prop('#interrupt-cells', 1).prop('interrupt-controller')
        intc.prop('compatible', 'riscv,cpu-intc').prop('phandle', 1)

        soc = root.child('soc')
        soc.prop('#address-cells', 2).prop('#size-cells', 2)
        soc.prop('compatible', 'simple-bus').prop('ranges')

        n = soc.child('test@%x' % TEST_BASE)
        n.prop('compatible', fdt.string('sifive,test1', 'sifive,test0', 'syscon'))
        n.prop('reg', fdt.cells64(TEST_BASE, 0x1000)).prop('phandle', 3)

        n = soc.child('rtc@%x' % RTC_BASE)
        n.prop('compatible', 'google,goldfish-rtc')
        n.prop('reg', fdt.cells64(RTC_BASE, 0x1000))
        n.prop('interrupts', RTC_IRQ).prop('interrupt-parent', 2)

        n = soc.child('clint@%x' % CLINT_BASE)
        n.prop('compatible', fdt.string('sifive,clint0', 'riscv,clint0'))
        n.prop('reg', fdt.cells64(CLINT_BASE, 0x10000))
        n.prop('interrupts-extended', fdt.u32(1, 3, 1, 7))

        n = soc.child('plic@%x' % PLIC_BASE)
        n.prop('compatible', fdt.string('sifive,plic-1.0.0', 'riscv,plic0'))
        n.prop('reg', fdt.cells64(PLIC_BASE, self.plic.size))
        n.prop('interrupts-extended', fdt.u32(1, 11, 1, 9))
        n.prop('riscv,ndev', 31)
        n.prop('interrupt-controller').prop('#interrupt-cells', 1).prop('#address-cells', 0)
        n.prop('phandle', 2)

        n = soc.child('serial@%x' % UART_BASE)
        n.prop('compatible', 'ns16550a')
        n.prop('reg', fdt.cells64(UART_BASE, 0x100))
        n.prop('clock-frequency', 3686400)
        n.prop('interrupts', UART_IRQ).prop('interrupt-parent', 2)

        if self.disk:
            n = soc.child('virtio_mmio@%x' % VIRTIO_BASE)
            n.prop('compatible', 'virtio,mmio')
            n.prop('reg', fdt.cells64(VIRTIO_BASE, 0x1000))
            n.prop('interrupts', VIRTIO_IRQ).prop('interrupt-parent', 2)
        if self.net:
            n = soc.child('virtio_mmio@%x' % NET_BASE)
            n.prop('compatible', 'virtio,mmio')
            n.prop('reg', fdt.cells64(NET_BASE, 0x1000))
            n.prop('interrupts', NET_IRQ).prop('interrupt-parent', 2)
        if syscon_reset:
            # for external firmware (OpenSBI); with the built-in SBI the kernel
            # powers off through the SBI SRST extension instead
            n = root.child('poweroff')
            n.prop('compatible', 'syscon-poweroff').prop('regmap', 3)
            n.prop('offset', 0).prop('value', 0x5555)
            n = root.child('reboot')
            n.prop('compatible', 'syscon-reboot').prop('regmap', 3)
            n.prop('offset', 0).prop('value', 0x7777)
        return fdt.build(root)

    # ------------------------------------------------------------------ image loading
    def load_kernel(self, data, default_addr):
        """Load a RISC-V Linux `Image`, an ELF file, or a raw binary.

        Returns (entry, end_of_image, elf_symbols)."""
        if data[:4] == b'\x7fELF':
            return self.load_elf(data, default_addr)
        if len(data) >= 64 and (data[56:60] == b'RSC\x05' or data[48:56] == b'RISCV\0\0\0'):
            text_offset, image_size = struct.unpack_from('<QQ', data, 8)
            addr = RAM_BASE + text_offset
            self.load_bytes(addr, data)
            return addr, addr + max(image_size, len(data)), {}
        self.load_bytes(default_addr, data)
        return default_addr, default_addr + len(data), {}

    def load_elf(self, data, default_addr):
        if data[4] != 2 or data[5] != 1 or struct.unpack_from('<H', data, 18)[0] != 243:
            raise VMError('not a little-endian 64-bit RISC-V ELF file')
        entry, phoff, shoff = struct.unpack_from('<QQQ', data, 24)
        phentsize, phnum, shentsize, shnum, shstrndx = struct.unpack_from('<HHHHH', data, 54)
        segs = []
        for k in range(phnum):
            p = phoff + k * phentsize
            ptype, _, off, vaddr, paddr, filesz, memsz = struct.unpack_from('<IIQQQQQ', data, p)
            if ptype == 1 and memsz:
                segs.append((off, vaddr, paddr, filesz, memsz))
        if not segs:
            raise VMError('ELF file has no loadable segments')
        lo = min(s[2] for s in segs)
        hi = max(s[2] + s[4] for s in segs)
        if RAM_BASE <= lo and hi <= RAM_BASE + self.ram_size:
            bias = 0                                    # load at physical addresses
            entry_pa = entry if RAM_BASE <= entry < RAM_BASE + self.ram_size else \
                entry - min(s[1] for s in segs) + lo
        else:                                           # e.g. vmlinux linked at a VA
            vlo = min(s[1] for s in segs)
            bias = default_addr - lo
            entry_pa = entry - vlo + default_addr
            segs = [(o, v, v - vlo + lo, f, m) for o, v, p, f, m in segs]
            hi = max(s[2] + s[4] for s in segs)
        for off, vaddr, paddr, filesz, memsz in segs:
            self.load_bytes(paddr + bias, data[off:off + filesz] + bytes(memsz - filesz))
        return entry_pa, hi + bias, self._elf_symbols(data, shoff, shentsize, shnum)

    @staticmethod
    def _elf_symbols(data, shoff, shentsize, shnum):
        syms = {}
        if not shoff:
            return syms
        secs = [struct.unpack_from('<IIQQQQIIQQ', data, shoff + k * shentsize) for k in range(shnum)]
        for s in secs:
            if s[1] != 2:                               # SHT_SYMTAB
                continue
            strtab = secs[s[6]]
            for k in range(s[5] // 24):
                name, info, other, shndx, value, size = struct.unpack_from('<IBBHQQ', data, s[4] + 24 * k)
                if name:
                    end = data.index(b'\0', strtab[4] + name)
                    syms[data[strtab[4] + name:end].decode(errors='replace')] = value
        return syms

    # ------------------------------------------------------------------ boot modes
    def boot_linux(self, kernel, initrd=None, bootargs='', dtb=None, bios=None):
        """Boot a Linux kernel.  With bios=None the built-in Python SBI is used and
        the kernel starts directly in S-mode; otherwise `bios` (e.g. OpenSBI
        fw_jump.bin) is started in M-mode at the start of RAM."""
        cpu = self.cpu
        kbase = RAM_BASE + 0x200000
        entry, kend, _ = self.load_kernel(kernel, kbase)
        if bios is not None:
            self.load_bytes(RAM_BASE, bios)

        dtb_reserve = 2 * MB
        top = RAM_BASE + self.ram_size - dtb_reserve
        rd_range = None
        if initrd is not None:
            start = RAM_BASE + (self.ram_size // 2 if self.ram_size < 256 * MB else 128 * MB)
            start = max(start, align_up(kend, 2 * MB))
            if start + len(initrd) > top:
                start = align_up(kend, 4096)
            if start + len(initrd) > top:
                raise VMError('initrd (%d KiB) does not fit in RAM; use more --mem'
                              % (len(initrd) >> 10))
            self.load_bytes(start, initrd)
            rd_range = (start, start + len(initrd))
        self.bootargs = bootargs
        blob = dtb if dtb is not None else self.make_fdt(bootargs, rd_range, bios is not None)
        dtb_addr = (RAM_BASE + self.ram_size - len(blob)) & ~(2 * MB - 1)
        if dtb_addr < (rd_range[1] if rd_range else kend):
            dtb_addr = align_up(rd_range[1] if rd_range else kend, 4096)
        self.load_bytes(dtb_addr, blob)
        self.dtb_blob = blob

        cpu.x[10] = 0               # a0 = hart id
        cpu.x[11] = dtb_addr         # a1 = device tree
        if bios is None:
            cpu.sbi = SBI(self)
            cpu.medeleg = 0xB1FF     # everything except ecall-from-S goes to the kernel
            cpu.mideleg = S_INTS
            cpu.mcounteren = 7
            cpu.scounteren = 7
            cpu.priv = 1
            cpu.pc = entry
        else:
            cpu.priv = 3
            cpu.pc = RAM_BASE
        cpu.update_tlbs()
        return dtb_addr

    def boot_bare(self, image):
        """Run a bare-metal program in M-mode (e.g. riscv-tests).  HTIF `tohost`
        is honoured when the ELF has that symbol."""
        cpu = self.cpu
        entry, _, syms = self.load_kernel(image, RAM_BASE)
        if 'tohost' in syms:
            self.htif = (syms['tohost'], syms.get('fromhost'))
        cpu.priv = 3
        cpu.pc = entry
        cpu.update_tlbs()

    # ------------------------------------------------------------------ main loop
    def run(self, max_insns=None):
        """Run until the guest halts.  Returns the exit code."""
        cpu = self.cpu
        con = self.console
        uart = self.uart
        clint = self.clint
        net = self.net
        executed = 0
        try:
            while True:
                if cpu.wfi:
                    self.idle()
                else:
                    # stop the batch exactly when the next (future) timer event is due;
                    # an already-expired timer is pending in mip and needs no batch cut
                    n = BATCH
                    now = cpu.mtime
                    d = cpu.stimecmp - now
                    if 0 < d < n:
                        n = d
                    d = clint.mtimecmp - now
                    if 0 < d < n:
                        n = d
                    ex = cpu.run_batch(n)
                    cpu.mtime += ex
                    cpu.instret += ex
                    executed += ex
                    if max_insns is not None and executed >= max_insns:
                        raise Halt(0, 'instruction limit reached')
                t = cpu.mtime
                if t >= cpu.stimecmp:
                    cpu.mip |= STIP
                if t >= clint.mtimecmp:
                    cpu.mip |= MTIP
                if con.inq:
                    uart.poll(con)
                if net is not None:
                    net.poll()
                if con.buf:
                    con.flush()
                if cpu.mip & cpu.mie:
                    cpu.check_irq()
                if self.htif is not None:
                    self.poll_htif()
                if con.quit_requested:
                    raise Halt(0, 'quit (Ctrl-A X)')
        except Halt as h:
            self.halt_reason = h.reason
            return h.code
        finally:
            self.stats_insns += executed
            con.flush()

    def idle(self):
        """The hart executed WFI: wait for the next timer event or console input."""
        cpu = self.cpu
        con = self.console
        clint = self.clint
        net = self.net
        while True:
            if net is not None:
                net.poll()
            if cpu.mtime >= cpu.stimecmp:
                cpu.mip |= STIP
            if cpu.mtime >= clint.mtimecmp:
                cpu.mip |= MTIP
            if cpu.mip & cpu.mie:
                cpu.wfi = False
                return
            # only timers whose interrupt is enabled can end the WFI
            deadline = M64
            if cpu.mie & STIP:
                deadline = cpu.stimecmp
            if cpu.mie & MTIP and clint.mtimecmp < deadline:
                deadline = clint.mtimecmp
            ticks = deadline - cpu.mtime        # > 0, or the interrupt would be pending
            if self.realtime or deadline == M64:
                timeout = min(ticks / TIMEBASE, 0.5)
                t0 = time.monotonic()
                got = con.wait(timeout)
                adv = int((time.monotonic() - t0) * TIMEBASE)
                if net is not None and net.poll():
                    got = True
                if not got and ticks <= 0.5 * TIMEBASE:
                    adv = max(adv, ticks)       # slept the whole way: reach the deadline
                cpu.mtime += max(adv, 1)
            elif con.inq:
                got = True
            else:
                cpu.mtime = deadline            # deterministic mode: skip idle time
                got = False
            if got:
                self.uart.poll(con)
            if con.quit_requested:
                raise Halt(0, 'quit (Ctrl-A X)')

    def poll_htif(self):
        tohost, fromhost = self.htif
        o = tohost - RAM_BASE
        v = self.cpu.mQ[o >> 3]
        if not v:
            return
        dev, cmd, payload = v >> 56, (v >> 48) & 0xFF, v & ((1 << 48) - 1)
        self.cpu.mQ[o >> 3] = 0
        if dev == 0 and cmd == 0:
            if payload & 1:
                raise Halt(payload >> 1, 'htif exit')
            args = struct.unpack('<8Q', self.phys_read(payload, 64))
            ret = -1
            if args[0] == 64:                          # write(fd, buf, len)
                data = self.phys_read(args[2], args[3])
                if data is not None:
                    self.console.write(data)
                    ret = args[3]
            self.phys_write(payload, struct.pack('<q', ret))
            if fromhost:
                self.phys_write(fromhost, struct.pack('<Q', 1))
        elif dev == 1 and cmd == 1:
            self.console.write(bytes((payload & 0xFF,)))
            if fromhost:
                self.phys_write(fromhost, struct.pack('<Q', (1 << 56) | (1 << 48)))
