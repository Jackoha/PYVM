> this is just a vibe project. thank you claude.

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

**Let pyvm fetch one** (any released version -- see
[Choosing a Linux version](#choosing-a-linux-version-and-fetching-it)):

```sh
python3 tools/get_kernel.py --list      # what kernel.org is publishing
python3 tools/get_kernel.py 6.6.162     # download, verify, build -> ./Image-6.6.162
```

**From a source tree you already have:**

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
CONFIG_RANDOM_TRUST_BOOTLOADER=y                                # entropy, see below
```

**Entropy, and why TLS hangs without it.** This board has no hardware random
number generator — no `virtio-rng`, no CPU instruction (Zkr is not emulated).
Linux therefore has nothing to seed `/dev/random` from, its CRNG never finishes
initialising, and **every call to `getrandom()` blocks forever**. Programs that
avoid it (busybox `wget`, `nslookup`) work fine, which makes the failure
confusing: `curl --version` prints happily, while `curl https://...` hangs
without a single byte of output, because OpenSSL seeds its RNG at startup.

pyvm fixes this the way a real bootloader does: it puts 64 bytes from the host's
`os.urandom()` into the device tree as `/chosen/rng-seed`, which Linux mixes in
during `parse_dtb()`. The kernel only *credits* that seed as entropy when
`CONFIG_RANDOM_TRUST_BOOTLOADER=y`, so that option is required — it is in
[`linux/pyvm.config`](linux/pyvm.config).

Its Kconfig default is `y`, so a stock `defconfig` kernel already has it; only
`make tinyconfig` switches it off, which is why pyvm's fragment sets it back.

Note that the `random.trust_bootloader=1` **command-line parameter does not
help**, however plausible it looks. It is an `early_param`, and `setup_arch()`
calls `parse_dtb()` (which mixes the seed) before `parse_early_param()` (which
would parse the flag), so the seed has already been discarded as untrusted by
the time the parameter is read. The config option is the only way.

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

## The boot menu

Run pyvm with no arguments on a terminal and it asks what to boot instead of
printing a usage error:

```sh
python3 -m pyvm          # or: ./run.sh --menu,  or: python3 -m pyvm --menu
```

```
pyvm  --  RISC-V RV64GC virtual machine in Python

 Kernel              Image                  Linux 6.1.74 #3 Oct  5, 4M
 Root filesystem     alpine.img             Alpine 3.20.10, ext4 disk, 512M  --  disk: ...
 Memory              256 MiB                256 MiB of guest RAM
 Network             user                   NAT gateway in Python, no privileges needed
 Disk read-only      off
 Real time           on
 Print stats         off
 Kernel cmdline      (default)
 Capture to pcap     (off)
 Stop after N insns  (no limit)

 python3 -m pyvm -k ./Image -d ./alpine.img -m 256 --net user
 ↑↓ move   ←→ change   enter boot   e edit   g get kernel   s save   q quit
```

It finds the kernels and root filesystems in the directory and says what each
one is: the **Linux version and build date** read out of every kernel image, and
the **distribution** read out of every disk image, so picking a version is a
matter of arrowing through them. The line above the keys is the command the menu
stands for, so the flags stay discoverable rather than being hidden behind a UI.

Choices are remembered in `.pyvm-launcher.json`, along with the probe results so
the menu opens instantly next time. Arguments given with `--menu` still win, so
`python3 -m pyvm --menu -m 512` boots the menu's choice with different memory.
Anything that would stop the guest booting -- no kernel, a nonsense instruction
limit -- is refused with a reason instead of failing after the screen clears.

With arguments, or when stdin is not a terminal, pyvm behaves exactly as before,
so scripts and the test suite are unaffected. `tests/test_launcher.py` covers
the menu without needing a terminal.

### Choosing a Linux version, and fetching it

Press **`g`** in the menu and it asks kernel.org what it is publishing, then
downloads, verifies and builds the version you pick -- and comes back with the
new image selected, ready to boot:

```
  Linux versions kernel.org is publishing:

     1) 7.3.5        stable     2026-11-17
     2) 7.2.15       stable     2026-11-01  (end of life)
     3) 6.18.64      longterm   2026-11-17
     4) 6.12.118     longterm   2026-11-17
     5) 6.6.162      longterm   2026-11-06  (built here already)
     6) 6.1.192      longterm   2026-11-06

  Those are only the newest of each series.  Type a series to see
  every release in it: 7.3, 7.2, 6.18, 6.12, 6.6, 6.1, 5.15, 5.10
  -- or an older one (6.4, 5.4, ...).

  number, version, series, or Enter to go back: 6.6
```

Naming a series lists everything in it, and any of those can be picked by
number or by name:

```
  Linux 6.6 -- 163 releases, newest first:

     1) 6.6.162      2) 6.6.161      3) 6.6.160      4) 6.6.159      5) 6.6.158
     6) 6.6.157      7) 6.6.156      8) 6.6.155      9) 6.6.154     10) 6.6.153
     ...
        (and 103 older, back to 6.6 -- type the one you want)
```

**The list is not a guess.** kernel.org publishes one `sha256sums.asc` per
series directory naming every tarball in it, so a version is listed exactly
when it can be both downloaded and verified -- about **1100 in v5.x, 770 in
v6.x and 50 in v7.x**. One download describes a whole series, and it is kept
in `.pyvm-kernels.json` for a day (`--no-cache` re-reads it); a published
checksum never changes, so a cached one is as good as a fresh one.

The same thing from the command line:

```sh
python3 tools/get_kernel.py --list              # what kernel.org offers now
python3 tools/get_kernel.py --list 6.6          # every 6.6 release, 163 of them
python3 tools/get_kernel.py --list 6            # every 6.x release, 767 of them
python3 tools/get_kernel.py 6.6.162             # -> ./Image-6.6.162
python3 tools/get_kernel.py 6.6.162 --fetch-only          # source only, no build
python3 tools/get_kernel.py --installed         # kernels already built here
make kernels                                    # the list
make get-kernel VER=6.6.162                     # fetch and build one
```

Each step is skipped when its result is already there, so an interrupted
download or a failed build costs nothing to retry:

| step | result |
|---|---|
| download | `linux-<version>.tar.xz`, resumable, 16 connections at once |
| verify | sha256 compared against kernel.org's signed `sha256sums.asc` |
| extract | `linux-<version>/` |
| build | `Image-<version>`, via `tools/build_linux.sh` and the same `linux/pyvm.config` |

The image is named after its version, which is also how the menu can tell them
apart -- it reads the `Linux version` banner out of each one, so after fetching
a few you arrow through `Linux 6.1.74`, `Linux 6.6.162`, and so on.

Two honest caveats:

- **What the checksum proves.** `sha256sums.asc` is PGP-signed by kernel.org,
  but comparing the sum against it only tells you the tarball matches what that
  file says, over the same TLS connection that served the tarball. Add
  `--verify-signature` for the real thing: it checks the PGP signature on the
  tarball with `gpg`, and tells you plainly when it cannot (no gpg, or their
  signing keys are not in your keyring).
- **Release candidates are refused.** kernel.org publishes no checksum list
  covering `-rc` tarballs or `linux-next`, and this script will not install
  something it cannot check. Released versions only.

A version pyvm has not been tried with may simply not build with the compiler
you have, or may need changes to `linux/pyvm.config`; the build output says
which, and nothing else is touched when it fails.

**6.6.162 was fetched and booted this way** to check the whole path works
(clang 18, the `linux/patches/` fix applied cleanly, all of
`tests/boot_linux.py` passed, networking included). It is worth knowing that a
newer kernel is not automatically a faster boot here:

| | instructions to the prompt | wall | decoded pages |
|---|---|---|---|
| Linux 6.1.74 | 48.4 M | 15.0 s | 451 |
| Linux 6.6.162 | **42.7 M** | **16.3 s** | 469 |

6.6 gets there in 12% fewer instructions but takes 8% longer, so its
instruction mix costs this interpreter more per instruction. The number of
decoded pages is almost the same, so that is not a decode-cache effect; beyond
that we have not pinned it down.

## Keeping your files

pyvm can boot two different roots, and they behave very differently:

| | | |
|---|---|---|
| `./run.sh` | `rootfs.cpio` as an **initramfs** | lives in RAM — **everything you write is gone at poweroff** |
| `./run.sh --disk` | `disk.img`, a 64 MiB ext4 image | a real file on the host — **your changes are still there next boot** |

So if you want the VM to remember what you did, boot with the disk:

```sh
./run.sh --disk            # or: make run-disk
                           # or: python3 -m pyvm -k Image -d disk.img
```

The guest says which one it got, right under the welcome banner:

```
Storage: /dev/root - changes are saved
Storage: initramfs (RAM) - changes are LOST at poweroff; boot with --disk to keep them
```

(`/proc/mounts` calls a `root=`-mounted device `/dev/root`; it is `/dev/vda`,
the disk image.)

Anything you do then survives — new files, installed binaries, edited configs,
shell history:

```
root@pyvm:~# echo "my notes" > /root/notes.txt
root@pyvm:~# poweroff -f
$ ./run.sh --disk
root@pyvm:~# cat /root/notes.txt
my notes
```

`poweroff -f`, `poweroff` and `exit` all flush the filesystem before stopping, so
they are safe. **Ctrl-A X is not** — it pulls the virtual power cord, and
whatever Linux still had in its page cache is lost. Use it only to escape a
wedged guest, and type `sync` first if you have just written something you care
about. `tests/persist.py` checks all three.

`--disk-ro` attaches the image read-only when you want a throwaway session that
cannot alter it, and `tools/mkrootfs.py --ext4 disk.img --size 256` makes a
bigger one. Note that `disk.img` is in `.gitignore`, so files you create inside
the guest stay on your machine and are not pushed to GitHub.

To change a file *on* `disk.img` from the host, either boot the VM and edit it
in the guest, or rebuild the image with `tools/mkrootfs.py` (pass `--dir DIR` to
graft in files you want to keep). Do **not** patch a used image with `debugfs`:
once the guest has mounted it, ext4's journal can replay over edits made behind
its back, silently undoing them. `mkrootfs.py` only uses `debugfs` on an image
it has just created, whose journal is still empty.

## Installing software: `curl`, `apk` and friends

The busybox root filesystem is deliberately tiny: one static binary, no compiler,
no package manager, and `curl: not found` (busybox provides `wget` instead).
When you want real programs, boot the **Alpine Linux** image instead. It is the
same kernel and the same VM, just a userland with a package manager:

```sh
./run.sh --alpine          # builds alpine.img the first time, then boots it
                           # or: make alpine && make run-alpine
```

`curl` and the CA certificates are already installed, and `apk` works against
Alpine's real repositories over the network pyvm provides:

```
pyvm:~# curl -sI https://www.alpinelinux.org/ | head -1
pyvm:~# apk update
pyvm:~# apk add jq git python3 build-base
```

`apk update` reports about 22,000 packages across `main` and `community`, all
built for riscv64. Because the image is an ext4 disk, anything you install
**stays installed** across reboots.

#### `apk` is slow, and interrupting it causes a confusing error

Installing a package takes **minutes**, not seconds: `apk add sl` measured 367 s,
`apk update` 416 s. Usually you should not do it in the guest at all --
[install from the host](#install-from-the-host-not-inside-the-guest) instead,
which takes seconds. If you do run it here, pass `--no-progress`, because the
progress bar writes constantly to the emulated serial console and console I/O is
one of the more expensive things this VM does.

The trap is that `apk` looks hung, so you interrupt it and try again, and the
first one has not finished letting go of its lock:

```
# apk add sl
ERROR: Unable to lock database: temporary error (try again later)
ERROR: Failed to open apk database: temporary error (try again later)
```

"temporary error (try again later)" is apk's own wording for `EAGAIN`, here from
`flock()` on `/lib/apk/db/lock`. Check with `ps | grep apk` and either wait for
it or `kill` it; the kernel drops the lock when the process exits, so there is
no stale lock to clean up. Deleting `/lib/apk/db/lock` is only worth trying if
`ps` shows nothing and it still fails.

Do not stop the VM in the middle of an `apk` transaction (especially not with
Ctrl-A X, which does not flush) — that can leave the package database
half-written.

#### HTTPS works, but verifying a certificate is slow

Expect TLS to be the slowest thing the guest does. Verifying a certificate
chain means parsing a 217 KiB trust store and doing public-key maths, which at a
few MIPS takes **tens of seconds**:

```
curl http://example.com/                 -> 200 in   2 s
curl https://dl-cdn.alpinelinux.org/     -> 200 in  29 s
curl -k https://example.com/             -> 200 in   9 s   (verification skipped)
curl https://example.com/                -> fails
```

The last line is not a pyvm bug — the handshake and the verification both work.
The problem is that some servers, Cloudflare among them, close an idle
connection after about ten seconds, which is less than the guest needs. A
capture shows the guest acknowledging the certificate, the server sending FIN
while the guest is still doing the maths, and curl then reporting
`Send failure: Broken pipe`. Against a more patient server the same request
succeeds. If you hit it, `-k` skips verification, `--cacert` with a trimmed
bundle cuts the parsing cost, and plain HTTP is unaffected.

### Install from the host, not inside the guest

`apk` works in the guest but it is painfully slow there -- measured on this
machine, `apk add sl` took **367 seconds** and `apk update` **416**. Nothing is
wrong; the VM simply runs at a few MIPS, and unpacking a package is real work.

So do it out here instead. If a riscv64 user-mode emulator is installed
(`qemu-user-static`, which also registers riscv64 with binfmt), `mkalpine.py`
runs **the guest's own `apk`** on the host against the image's tree:

```sh
make apk PKG="htop ncdu"
# or: python3 tools/mkalpine.py --into alpine.img --pkg htop --pkg ncdu
```

The same two packages that need minutes inside the VM install in **under three
seconds**, and because it is really apk doing the work, apk's own database is
updated -- the guest knows the packages are installed and will not fetch them
again. Everything already in the image is kept: the image is read out, the
packages are added, and it is rebuilt and checked before replacing the original,
so an interrupted run cannot damage the disk you had.

Two things to know. Post-install scripts are skipped, because apk runs them
through `chroot()` and that needs privileges we do not have out here; run
`apk fix` in the guest if a package turns out to need its script. And without
`qemu-user-static` the tool falls back to unpacking `.apk` files itself, which
works but leaves apk's database unaware of them -- `--unpack` forces that path
if you want it.

### Baking packages into a fresh image

Installing inside the guest is slow — the VM runs at a few MIPS, so unpacking a
large package takes a while. `tools/mkalpine.py` can instead unpack packages on
the host at build time, so they are present on the very first boot and need no
network:

```sh
python3 tools/mkalpine.py --ext4 alpine.img --size 512 \
    --pkg curl --pkg jq --pkg python3
```

It reads Alpine's `APKINDEX`, resolves dependencies itself (`curl` alone pulls in
17 packages), verifies the root filesystem tarball against its published
sha256, and unpacks everything into the image. `--no-pkg` gives bare Alpine,
`--cpio` writes an initramfs instead of a disk, and `--dir DIR` grafts in files
of your own.

| | busybox (`disk.img`) | Alpine (`alpine.img`) |
|---|---|---|
| size | 64 MiB image, ~2 MiB used | 512 MiB image, ~30 MiB used |
| boot | fastest | a little slower |
| userland | one static busybox | musl, busybox, `apk` |
| packages | none | `apk add` anything in Alpine's riscv64 repos |

Both are ordinary pyvm disks, so `--disk-ro`, `--net` and everything else work
the same way. Neither image is in git (`.gitignore`); `run.sh` and
`tools/mkalpine.py` rebuild them.

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
| tiny config (`tools/build_linux.sh`) → shell prompt | **~11 s** (~47 M instructions) |
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

### Where a boot's instructions go

`tests/boot_phases.py` reports the instruction count as the guest passes
landmarks in its own console output.  Instruction counts repeat to about
0.02% between runs, so they are the honest way to compare two images; wall
time on a loaded host is not.

```sh
python3 tests/boot_phases.py --kernel Image --initrd rootfs.cpio --repeat 2
python3 tests/boot_phases.py --kernel Image --disk disk.img
```

Most of a boot is the kernel: of the ~47 M instructions to the prompt with the
busybox initramfs, ~34 M happen before `/init` runs, and ~26 M of those are
unpacking the 1 MB initramfs into a ramfs.  Userspace is the remaining ~7 M.

One thing in there was worth fixing.  busybox is a single binary that decides
what to be from `argv[0]`, so it needs ~300 symlinks (`/bin/ls`,
`/usr/bin/awk`, …) pointing at it, and the init script used to make them at
every boot with `busybox --install -s`.  `tools/mkrootfs.py` now bakes them
into the image at build time instead, reading busybox's own applet table out
of the binary (see `busybox_applets()`), so the guest does not have to:

| root filesystem | before | after | |
|---|---|---|---|
| ext4 disk, first boot | 73.4 M | **43.0 M** | −41% |
| initramfs, every boot | 48.6 M | **46.5 M** | −4.3% |

The disk gains far more because each symlink there is a journalled ext4
metadata write through the emulated virtio-blk device — 30.6 M instructions
for the 303 links, against 6.3 M for the same links in a ramfs.  An initramfs
gives most of its saving back, because the kernel then has 303 more cpio
entries to unpack (4.2 M), but what is left applies to every boot, while a
disk only ever paid on its first one.

Two other shapes were measured and rejected: cpio **hardlinks** instead of
symlinks are worse than changing nothing (50.9 M — the kernel links *and*
opens each entry), and making **no links at all** does not work, because this
busybox is built without a standalone shell, so without links even `mount` is
not found and `/init` dies.

The links are still identical to what the guest used to make: the names and
directories read out of the binary match, one for one, the 305 links a real
`busybox --install -s` left behind.  The init script keeps its
`[ -e /bin/ls ] || busybox --install -s` guard, so an image built with
`--no-applets`, or from a busybox whose table `mkrootfs` cannot read, still
works the old way.

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

# persistence: write a file, stop the VM three different ways, boot again and
# look for it (says which ways of exiting are safe)
python3 tests/persist.py --kernel Image --disk disk.img

# the boot menu, against a fake curses window: drawing, keys, the argv it builds
python3 tests/test_launcher.py

# busybox's applet table: that mkrootfs can still read it out of the binary,
# that it refuses to guess, and that the links land in the image
python3 tests/test_applets.py

# where a boot spends its instructions (use this to check an optimisation)
python3 tests/boot_phases.py --kernel Image --initrd rootfs.cpio --repeat 2
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
  launcher.py    the boot menu: finds images, reads their versions, builds argv
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
  mkalpine.py     build an Alpine riscv64 root filesystem with apk and curl,
                  resolving .apk dependencies from APKINDEX itself
tests/            riscv-tests runner, Linux boot test, network stack test,
                  disk persistence test, boot menu test
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
