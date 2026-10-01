# pyvm — a RISC-V virtual machine in Python that boots Linux

pyvm is a full-system emulator written in pure Python (no C extensions, no
dependencies beyond the standard library). It emulates a 64-bit RISC-V board
closely modelled on QEMU's `virt` machine, so a stock Linux kernel boots on it
to a shell.

## Architecture

| | |
|---|---|
| **ISA** | **RISC-V 64-bit, RV64GC** = RV64IMAFDC + Zicsr + Zifencei |
| Privilege | Machine / Supervisor / User modes (priv. spec 1.12) |
| MMU | **Sv39** paging, hardware A/D-bit updates, no ASIDs |
| FPU | single + double precision; exact IEEE-754 rounding, all 5 rounding modes, accrued flags |
| Harts | 1 |
| Firmware | built-in SBI v1.0 implemented in Python (TIME, IPI, RFENCE, HSM, SRST, DBCN, legacy console), **or** a real M-mode firmware such as OpenSBI via `--bios` |
| Timer | 10 MHz timebase; guest time advances 1 tick per instruction, and idle (WFI) periods are slept in real time |
| Network | virtio-net, with a NAT gateway (DHCP/DNS/ICMP/TCP/UDP, port forwarding) written in Python — or a host TAP device |

### Memory map / devices

| Address | Device | IRQ | Linux driver |
|---|---|---|---|
| `0x0010_0000` | SiFive test device (poweroff/reboot) | – | `syscon-poweroff` (bios mode) |
| `0x0010_1000` | Goldfish RTC (host wall clock) | 11 | `rtc-goldfish` |
| `0x0200_0000` | CLINT (mtime/mtimecmp/msip) | – | (used by M-mode firmware) |
| `0x0C00_0000` | PLIC (2 contexts: M, S) | – | `sifive-plic` |
| `0x1000_0000` | NS16550A UART | 10 | `8250` → **`ttyS0`** |
| `0x1000_1000` | virtio-mmio v2 block device (with `--disk`) | 1 | `virtio_blk` → **`/dev/vda`** |
| `0x1000_2000` | virtio-mmio v2 network device | 2 | `virtio_net` → **`eth0`** |
| `0x8000_0000` | RAM (default 128 MiB) | | |

The device tree is generated in Python at boot (no `dtc` needed). Linux sees:
`riscv,isa = "rv64imafdc_zicsr_zifencei"`, `mmu-type = "riscv,sv39"`.

## Quick start

```sh
./run.sh
```

That is all: it downloads and verifies Linux 6.1.74, builds the kernel, fetches
busybox, creates the root filesystems and boots, skipping any step whose output
already exists. Options: `--disk` (boot from the ext4 disk), `--defconfig`
(stock kernel config), `--rebuild`, `--build-only`, `--mem N`, `--net SPEC`,
`--hostfwd SPEC` (forward a host port into the guest), `-- <pyvm args>`.

The guest comes up with a working network ([below](#networking)): it asks pyvm's
built-in DHCP server for an address, so `ping`, `wget` and `nslookup` work in the
shell without any setup.

### Doing it by hand

Requires Python ≥ 3.8 (tested on CPython 3.13 and PyPy 3.11) on Linux, macOS or WSL.

```sh
# 1. userspace: static busybox for riscv64 (from Alpine) -> initramfs + ext4 disk
python3 tools/get_busybox.py                 # -> busybox.static
python3 tools/mkrootfs.py --busybox busybox.static --cpio rootfs.cpio --ext4 disk.img

# 2. kernel: build Linux 6.1.74 (the source is in ./linux-6.1.74) -> ./Image
tools/build_linux.sh linux-6.1.74

# 3. boot (networking is on by default: --net none turns it off)
python3 -m pyvm -k Image -i rootfs.cpio        # initramfs root
python3 -m pyvm -k Image -d disk.img           # or: ext4 root on /dev/vda
python3 -m pyvm -k Image -i rootfs.cpio --net user,hostfwd=tcp::8080-:80
```

Or use `make busybox rootfs`, `make kernel`, `make run`.

The repository holds source only. The Linux tarball, `Image`, `busybox.static`,
`rootfs.cpio` and `disk.img` are downloaded or built on demand and are listed in
`.gitignore`, so a fresh clone needs one `./run.sh` first (a few minutes, mostly
the kernel build); after that it boots straight away.

Press **Ctrl-A then X** to quit the VM, or type `poweroff -f` in the guest.
Ctrl-C goes to the guest (the terminal is put into raw mode).

## Adding your Linux 6.1.74

pyvm boots the standard RISC-V **`arch/riscv/boot/Image`** (an ELF `vmlinux` works too).

**From source (recommended):**

```sh
tools/build_linux.sh /path/to/linux-6.1.74            # small config, fastest boot
tools/build_linux.sh /path/to/linux-6.1.74 defconfig  # stock riscv defconfig
```

The script builds out of tree (`<src>/build-pyvm-<mode>/`) and copies the result
to `./Image`. It uses a `riscv64-linux-gnu-gcc` cross compiler if one is
installed, otherwise clang/LLVM (`LLVM=1`, or `LLVM=-18` style suffixes).

The default "tiny" mode is `make tinyconfig` plus the fragment
[`linux/pyvm.config`](linux/pyvm.config), which enables only what this machine
needs. If you configure the kernel yourself, make sure you have at least:

```
CONFIG_ARCH_RV64I=y  CONFIG_MMU=y  CONFIG_FPU=y  CONFIG_RISCV_ISA_C=y
CONFIG_SIFIVE_PLIC=y  CONFIG_RISCV_SBI_V01=y
CONFIG_SERIAL_8250=y  CONFIG_SERIAL_8250_CONSOLE=y  CONFIG_SERIAL_OF_PLATFORM=y
CONFIG_SERIAL_EARLYCON_RISCV_SBI=y  CONFIG_BLK_DEV_INITRD=y  CONFIG_BINFMT_ELF=y
CONFIG_DEVTMPFS=y  CONFIG_PROC_FS=y  CONFIG_SYSFS=y  CONFIG_TTY=y
CONFIG_VIRTIO_MMIO=y  CONFIG_VIRTIO_BLK=y  CONFIG_EXT4_FS=y     # for --disk
CONFIG_NET=y  CONFIG_INET=y  CONFIG_PACKET=y                    # for --net
CONFIG_NETDEVICES=y  CONFIG_VIRTIO_NET=y                        # (PACKET: udhcpc)
```

Keep `CONFIG_SMP` single-hart friendly (any `NR_CPUS` works; there is one
hart), and do **not** rely on Svpbmt/Zicbom/Sstc/V/H — they are not
emulated, and the kernel only uses them if the device tree advertises them.

**Kernel fix for clang builds.** Linux 6.1's riscv linker script does not
place the `.got` section. With clang/lld, the GOT (used for the kallsyms
tables) then lands in the init area that is freed after boot, and the first
stack trace afterwards dies with `kernel BUG at kernel/kallsyms.c:335`.
[`linux/patches/0001-riscv-place-got-in-data-section.patch`](linux/patches/0001-riscv-place-got-in-data-section.patch)
moves `.got` into the permanent data section; `build_linux.sh` applies it
automatically (it is a no-op for GCC builds). The copy of Linux 6.1.74 in this
directory already has it applied.

**Prebuilt Image:** just pass it with `-k`.

## Networking

The guest gets a virtio-net NIC (`eth0`). Two backends drive it, chosen with
`--net`; **user networking is the default**, so `make run` already has a working
network.

### `--net user` — a NAT gateway written in Python

No privileges, no host configuration, nothing to set up: guest connections are
translated into ordinary host sockets, the way QEMU's `-netdev user` works.
pyvm implements the whole gateway itself — ARP, a DHCP server, an ICMP echo
responder, a DNS forwarder, IPv4 fragmentation and reassembly, UDP flows and a
TCP state machine with windowing and retransmission.

| Address | |
|---|---|
| `10.0.2.0/24` | the virtual network |
| `10.0.2.2` | gateway — and it *is* the host: `10.0.2.2:22` reaches the host's own SSH |
| `10.0.2.3` | DNS forwarder, onto the resolvers in the host's `/etc/resolv.conf` |
| `10.0.2.15` | the guest, handed out by the built-in DHCP server |

Both supplied root filesystems run `udhcpc` at boot, so the guest is already
configured by the time you get a prompt — it prints `Network: eth0 10.0.2.15`
on the way up. From the shell:

```sh
ifconfig eth0                       # inet addr:10.0.2.15  Mask:255.255.255.0
ip route                            # default via 10.0.2.2 dev eth0
ping -c 2 10.0.2.2                  # the gateway answers echo requests
nslookup example.com                # through the forwarder on 10.0.2.3
wget -q -O - http://example.com/    # ordinary outbound TCP
```

Options go after `user`, comma-separated:

```sh
python3 -m pyvm -k Image -i rootfs.cpio --net user,hostfwd=tcp::8080-:80
```

Something in the guest has to be listening on the forwarded port — the busybox
root filesystem here has no SSH or web server, but `nc` is enough to try it out.
Boot with `--net user,hostfwd=tcp::8080-:5000`, then:

```
# nc -l -p 5000 -e /bin/echo hello-from-the-guest     # in the guest
$ nc 127.0.0.1 8080                                   # on the host
hello-from-the-guest
```

| Option | |
|---|---|
| `hostfwd=tcp::8080-:80` | forward a host port into the guest — repeatable, `tcp` or `udp`. Full form `tcp:hostaddr:hostport-:guestport`; the host address defaults to `127.0.0.1`, so pass `0.0.0.0` to expose the guest to your LAN |
| `net=10.0.2.0/24` | the virtual network |
| `host=`, `dns=`, `guest=` | override the gateway, DNS and guest addresses |
| `dnsserver=1.1.1.1` | forward DNS here instead of the host's resolvers |
| `restrict=on` | isolate the guest: only DHCP and DNS work, nothing reaches the host or the internet |
| `mac=`, `mtu=`, `debug=on` | NIC address, MTU, and tracebacks for dropped packets |

#### `ping` to the outside needs one host setting

TCP, UDP and DNS reach the internet with no setup at all. **ICMP is the
exception**, because Linux lets only privileged processes send it: pyvm needs
either a ping socket (allowed per group by a sysctl) or `CAP_NET_RAW`. Most
distributions enable the first; WSL2 and some containers ship it disabled, which
looks like this:

```
root@pyvm:~# ping google.com
PING google.com (74.125.204.139): 56 data bytes
pyvm: net: ping to the outside needs an ICMP socket, which this user may not open
5 packets transmitted, 0 packets received, 100% packet loss
```

Note that the name *did* resolve — only the echo request was dropped. Allow it
on the host, once:

```sh
sudo sysctl -w net.ipv4.ping_group_range="0 $(id -g)"
```

pyvm re-checks every few seconds, so a running VM starts pinging without a
restart. To make it survive a reboot, either `/etc/sysctl.d/10-ping.conf`:

```
net.ipv4.ping_group_range = 0 1000
```

or, on WSL2 (where sysctls reset with the VM), `/etc/wsl.conf`:

```ini
[boot]
command = "sysctl -w net.ipv4.ping_group_range='0 1000'"
```

Pings to the gateway (`10.0.2.2`) are answered by pyvm itself and always work,
so they are a good way to check the NIC without needing any of this.

**Other limits** — the same as every slirp-style stack: ICMP errors are not
relayed, raw IP protocols other than TCP/UDP/ICMP-echo do not pass, and the
guest is not reachable except through `hostfwd`. For those, use TAP.

> Busybox here provides **`wget`**, not `curl` — `curl: not found` is expected.

### `--net tap` — a real interface

The guest becomes a genuine host on a host network link. This needs a TAP device
you are allowed to open:

```sh
sudo ip tuntap add dev pyvm0 mode tap user $USER
sudo ip addr add 10.0.3.1/24 dev pyvm0
sudo ip link set pyvm0 up
python3 -m pyvm -k Image -i rootfs.cpio --net tap,ifname=pyvm0
```

Then give the guest an address on that link (`ip addr add 10.0.3.15/24 dev eth0;
ip route add default via 10.0.3.1`), and set up NAT on the host if it should
reach further (`iptables -t nat -A POSTROUTING -s 10.0.3.0/24 -j MASQUERADE`).
`--net tap,fd=N` uses a descriptor you opened yourself.

### Capturing traffic

`--net-dump FILE` writes every frame the NIC sees to a pcap file:

```sh
python3 -m pyvm -k Image -i rootfs.cpio --net-dump net.pcap
tcpdump -nr net.pcap        # or open it in Wireshark
```

## Command line

```
python3 -m pyvm [options]
  -k, --kernel IMAGE    Linux Image (or vmlinux ELF)
  -i, --initrd FILE     initramfs (cpio, optionally gzip'd)
  -d, --disk FILE       raw disk image -> /dev/vda   (--disk-ro for read-only)
  -n, --net SPEC        network backend: user[,OPT=VAL...] (default), tap or none
  --net-dump FILE       write every frame the NIC sees to FILE in pcap format
  -a, --append ARGS     kernel command line
                        default: "console=ttyS0 earlycon=sbi" (+ "root=/dev/vda rw" with --disk)
  -m, --mem MiB         RAM size (default 128)
  --bios FILE           boot M-mode firmware (e.g. OpenSBI fw_jump.bin) instead of the
                        built-in Python SBI; the kernel is loaded at 0x80200000
  --dtb FILE            use this device tree instead of the generated one
  --dump-dtb FILE       write the generated device tree and exit
  --bare ELF            run a bare-metal program in M-mode (riscv-tests, HTIF tohost)
  --no-realtime         deterministic time: skip idle periods instead of sleeping
  --max-insns N         stop after N instructions
  --stats               print instruction count and speed at exit
```

## Performance

Measured on one core of the development machine:

| Workload (Linux 6.1.74, busybox initramfs) | CPython 3.13 |
|---|---|
| tiny config (`tools/build_linux.sh`) → shell prompt | **~14 s** (~50 M instructions) |
| the same, with `--net user` configured by DHCP | **~14 s** (the NIC costs nothing to boot) |
| stock `defconfig` → shell prompt | ~33 s (~110 M instructions) |
| average speed while booting | ~3.6–4.5 MIPS |
| tight ALU loop | ~8 MIPS |

Networking is driven from the same loop as everything else: the backend's host
sockets live on their own thread, the device is polled once per instruction
batch, and an arriving packet wakes the hart out of `WFI` instead of waiting for
the next timer tick, so round trips to the host cost about a millisecond.

PyPy runs pyvm correctly but is only slightly faster (~5 MIPS), because the
design is tuned for CPython.

How it gets there:
- each instruction word is decoded **once** into a specialised Python closure;
  closures are memoized by instruction bits and cached per physical page;
- guest RAM is an `mmap` viewed through typed `memoryview`s, so aligned
  loads and stores are a single index operation;
- data TLBs are dicts keyed per (privilege, SUM, MXR), so syscalls and
  `copy_to_user` never flush them;
- stores into pages holding decoded code take a slow path that invalidates only
  the affected handlers, so self-modifying code and DMA stay coherent.

## Testing

```sh
# ISA compliance: riscv-tests (rv64ui/um/ua/uf/ud/uc/si/mi, physical + virtual-memory envs)
python3 tests/run_isa_tests.py /path/to/riscv-tests/isa-binaries    # 244/244 pass

# full system: boot Linux, run shell commands, power off
python3 tests/boot_linux.py --kernel Image --initrd rootfs.cpio
python3 tests/boot_linux.py --kernel Image --initrd rootfs.cpio --disk disk.img

# networking, inside the guest: DHCP, ping, a TCP download from a server on the
# host, and an inbound connection through a forwarded port
python3 tests/boot_linux.py --kernel Image --initrd rootfs.cpio --net
python3 tests/boot_linux.py --kernel Image --initrd rootfs.cpio --net --net-external

# the network stack on its own, against a fake guest: no kernel, runs in a second
python3 tests/test_net.py -v        # 10/10 pass, incl. 256 KiB TCP both ways
```

## Source layout

```
pyvm/
  cpu.py         hart state, CSRs, traps/interrupts, Sv39 MMU + TLBs, run loop
  decode.py      RV64GC decoder -> per-instruction closures (incl. compressed)
  fpu.py         exact IEEE-754 binary32/binary64 arithmetic for F/D
  sbi.py         built-in SBI firmware
  machine.py     board: RAM, bus, device tree, image loading, main loop, idle
  fdt.py         device tree blob writer
  console.py     host terminal <-> UART (raw mode, Ctrl-A X)
  devices/       uart.py plic.py clint.py rtc.py testdev.py
                 virtio.py      shared virtio-mmio v2 transport (registers, virtqueues)
                 virtio_blk.py  block device  -> /dev/vda
                 virtio_net.py  network device -> eth0
  net/           slirp.py  user-mode NAT gateway: ARP, DHCP, DNS, ICMP, UDP, TCP
                 tap.py    host TAP device
                 frame.py  Ethernet/ARP/IPv4/ICMP/UDP/TCP headers and checksums
                 pcap.py   --net-dump writer
linux-6.1.74/     Linux source (patched); built out of tree in build-pyvm-<mode>/
linux/pyvm.config kernel config fragment
linux/patches/    kernel fixes applied by build_linux.sh
tools/
  build_linux.sh  build a kernel Image for pyvm   (run.sh drives all of tools/)
  get_busybox.py  download a static riscv64 busybox
  mkrootfs.py     make an initramfs (pure Python cpio) and/or ext4 disk image
tests/            riscv-tests runner, Linux boot test, network stack test
```

## Limitations

- one hart; no SMP
- PMP registers exist but are not enforced; no ASIDs; Sv39 only (Linux falls back
  from Sv57/Sv48 automatically)
- no vector (V), hypervisor (H), Svpbmt, Sstc or Zicbom extensions
- virtio-net offers no checksum or segmentation offload, so throughput is bounded
  by Python; user networking carries TCP, UDP and ICMP echo, not arbitrary IP
  protocols (use `--net tap` for those)
- IPv6 is not routed by the user-mode gateway
