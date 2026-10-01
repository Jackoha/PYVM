#!/usr/bin/env bash
# Build everything that is missing, then boot Linux on pyvm (RISC-V RV64GC).
#
#   ./run.sh                  build what is missing, boot with the initramfs
#   ./run.sh --disk           boot with disk.img (ext4) as the root filesystem
#   ./run.sh --defconfig      use a stock-defconfig kernel (Image-defconfig) instead
#   ./run.sh --rebuild        rebuild the kernel even if an Image already exists
#   ./run.sh --build-only     build everything, do not boot
#   ./run.sh --mem 256        RAM size in MiB (default 128)
#   ./run.sh --net SPEC       network backend (default: user; see --net none, tap)
#   ./run.sh --hostfwd SPEC   forward a host port into the guest (repeatable),
#                             e.g. --hostfwd tcp::8080-:80   (see README)
#   ./run.sh -- ARGS...       extra arguments passed to `python3 -m pyvm`
#
# Steps (each is skipped when its output already exists):
#   1. linux-6.1.74/      download from kernel.org, verify sha256, extract
#   2. Image              build the kernel (tools/build_linux.sh, applies linux/patches)
#   3. busybox.static     download a static riscv64 busybox (Alpine)
#   4. rootfs.cpio        initramfs  (disk.img is only created if missing, never overwritten)
#   5. boot               python3 -m pyvm ...
#
# In the VM: `poweroff -f` shuts down, Ctrl-A then X quits immediately.

set -euo pipefail
cd "$(dirname "$0")"

LINUX_VER=6.1.74
LINUX_DIR=linux-$LINUX_VER
LINUX_TAR=$LINUX_DIR.tar.xz
LINUX_URL=https://cdn.kernel.org/pub/linux/kernel/v6.x/$LINUX_TAR
LINUX_SHA256=b7fbd1d79faed2ce3570ef79dc1223e4e19c868b86326b14a435db56ebbb2022

PYTHON=${PYTHON:-python3}
CONFIG=tiny
BOOT=initrd
REBUILD=0
RUN=1
MEM=128
NET=user
FWD=
EXTRA=()

usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --disk)       BOOT=disk ;;
        --defconfig)  CONFIG=defconfig ;;
        --rebuild)    REBUILD=1 ;;
        --build-only) RUN=0 ;;
        --mem)        MEM=${2:?--mem needs a value}; shift ;;
        --net)        NET=${2:?--net needs a value}; shift ;;
        --hostfwd)    FWD+=",hostfwd=${2:?--hostfwd needs a value}"; shift ;;
        -h|--help)    usage 0 ;;
        --)           shift; EXTRA=("$@"); break ;;
        *)            echo "unknown option: $1" >&2; usage 1 ;;
    esac
    shift
done

IMAGE=Image
[ "$CONFIG" = defconfig ] && IMAGE=Image-defconfig

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required ($2)"; }

# Fetch bytes s..e of url into file, resuming after dropped connections.
fetch_range() {
    local url=$1 file=$2 s=$3 e=$4 have try
    : > "$file"
    for ((try = 0; try < 30; try++)); do
        have=$(stat -c %s "$file")
        [ $((s + have)) -gt "$e" ] && return 0
        curl -sfL --connect-timeout 20 -r "$((s + have))-$e" "$url" >> "$file" || sleep 2
    done
    have=$(stat -c %s "$file")
    [ $((s + have)) -gt "$e" ]
}

# Download with many parallel connections (much faster on throttled links),
# falling back to a plain resumable curl if the size is unknown.
download() {
    local url=$1 out=$2 n=16 size chunk i s e ok=1 pids=()
    size=$(curl -sIL "$url" | tr -d '\r' | awk 'tolower($1)=="content-length:" {v=$2} END {print v}')
    if [ -n "$size" ] && [ "$size" -gt 10000000 ] 2>/dev/null; then
        chunk=$(( (size + n - 1) / n ))
        for ((i = 0; i < n; i++)); do
            s=$((i * chunk)); e=$((s + chunk - 1)); [ $e -ge "$size" ] && e=$((size - 1))
            fetch_range "$url" "$out.part$i" "$s" "$e" & pids+=($!)
        done
        for p in "${pids[@]}"; do wait "$p" || ok=0; done
        if [ $ok = 1 ]; then
            for ((i = 0; i < n; i++)); do cat "$out.part$i"; done > "$out.tmp"
            rm -f "$out".part*
            if [ "$(stat -c %s "$out.tmp")" = "$size" ]; then mv "$out.tmp" "$out"; return 0; fi
        fi
        rm -f "$out".part* "$out.tmp"
        echo "parallel download failed, retrying with a single connection"
    fi
    curl -fL --retry 10 -C - -o "$out" "$url"
}

need "$PYTHON" "Python 3.8 or newer"

# ---------------------------------------------------------------- 1. Linux source
if [ ! -f "$LINUX_DIR/Makefile" ]; then
    need curl "to download Linux"; need sha256sum "to verify the download"
    if [ ! -f "$LINUX_TAR" ] || ! echo "$LINUX_SHA256  $LINUX_TAR" | sha256sum -c --status; then
        step "Downloading Linux $LINUX_VER (~130 MB)"
        rm -f "$LINUX_TAR"
        download "$LINUX_URL" "$LINUX_TAR"
    fi
    step "Verifying $LINUX_TAR"
    echo "$LINUX_SHA256  $LINUX_TAR" | sha256sum -c - || die "checksum mismatch; delete $LINUX_TAR and retry"
    step "Extracting $LINUX_TAR"
    tar xf "$LINUX_TAR"
else
    step "Linux source: $LINUX_DIR (present)"
fi

# ---------------------------------------------------------------- 2. kernel
if [ ! -f "$IMAGE" ] || [ $REBUILD = 1 ] || \
   { [ "$CONFIG" = tiny ] && [ linux/pyvm.config -nt "$IMAGE" ]; }; then
    need make "to build the kernel"
    for t in flex bison bc perl; do need $t "to build the kernel"; done
    command -v riscv64-linux-gnu-gcc >/dev/null 2>&1 || command -v clang >/dev/null 2>&1 \
        || ls /usr/bin/clang-* >/dev/null 2>&1 \
        || die "need a riscv64 cross compiler (riscv64-linux-gnu-gcc) or clang/LLVM"
    step "Building the kernel ($CONFIG config) -> $IMAGE   (first build takes a few minutes)"
    tools/build_linux.sh "$LINUX_DIR" "$CONFIG" "$PWD/$IMAGE"
else
    step "Kernel: $IMAGE (present; use --rebuild to rebuild)"
fi

# ---------------------------------------------------------------- 3. busybox
if [ ! -f busybox.static ]; then
    step "Downloading static riscv64 busybox"
    "$PYTHON" tools/get_busybox.py -o busybox.static
else
    step "Busybox: busybox.static (present)"
fi

# ---------------------------------------------------------------- 4. root filesystems
if [ ! -f rootfs.cpio ] || [ busybox.static -nt rootfs.cpio ] || [ tools/mkrootfs.py -nt rootfs.cpio ]; then
    step "Building initramfs rootfs.cpio"
    "$PYTHON" tools/mkrootfs.py --busybox busybox.static --cpio rootfs.cpio
else
    step "Initramfs: rootfs.cpio (up to date)"
fi
if [ ! -f disk.img ]; then
    if command -v mke2fs >/dev/null 2>&1 || [ -x /sbin/mke2fs ]; then
        step "Building ext4 disk image disk.img (64 MiB)"
        "$PYTHON" tools/mkrootfs.py --busybox busybox.static --ext4 disk.img --size 64
    elif [ "$BOOT" = disk ]; then
        die "mke2fs (e2fsprogs) is needed to create disk.img"
    fi
else
    step "Disk: disk.img (present, kept as is)"
fi

# ---------------------------------------------------------------- 5. boot
if [ $RUN = 0 ]; then
    step "Everything is built.  Boot with: ./run.sh"
    exit 0
fi
if [ "$BOOT" = disk ]; then
    ARGS=(-k "$IMAGE" -d disk.img)
else
    ARGS=(-k "$IMAGE" -i rootfs.cpio)
fi
[ "$CONFIG" = defconfig ] && [ "$MEM" -lt 256 ] && MEM=256
if [ -n "$FWD" ]; then
    case "$NET" in
        user*) NET=$NET$FWD ;;
        *)     die "--hostfwd only applies to user networking (--net is '$NET')" ;;
    esac
fi
ARGS+=(--net "$NET")
step "Booting: $PYTHON -m pyvm ${ARGS[*]} -m $MEM ${EXTRA[*]:-}"
echo "    (shell prompt after ~15 s; 'poweroff -f' or Ctrl-A X to exit)"
exec "$PYTHON" -m pyvm "${ARGS[@]}" -m "$MEM" "${EXTRA[@]}"
