#!/bin/sh
# Build a Linux kernel Image for pyvm.
#
#   tools/build_linux.sh <linux-source-dir> [tiny|defconfig] [output-Image]
#
# tiny (default): tinyconfig + linux/pyvm.config -> small kernel, fast boot
# defconfig:      the stock riscv defconfig (boots too, but far slower)
#
# Uses a GCC cross compiler (riscv64-linux-gnu-gcc) when available, otherwise
# clang/LLVM.  Builds out of tree in <linux-source-dir>/build-pyvm-<mode>.
set -e

SRC=${1:?usage: $0 <linux-source-dir> [tiny|defconfig] [output-Image]}
MODE=${2:-tiny}
HERE=$(cd "$(dirname "$0")/.." && pwd)
OUT=${3:-$HERE/Image}
SRC=$(cd "$SRC" && pwd)
BUILD=$SRC/build-pyvm-$MODE

if command -v riscv64-linux-gnu-gcc >/dev/null 2>&1; then
    TC="CROSS_COMPILE=riscv64-linux-gnu-"
elif command -v riscv64-unknown-linux-gnu-gcc >/dev/null 2>&1; then
    TC="CROSS_COMPILE=riscv64-unknown-linux-gnu-"
elif command -v llvm-ar >/dev/null 2>&1 && command -v clang >/dev/null 2>&1; then
    TC="LLVM=1"
else
    V=$(ls /usr/bin/llvm-ar-* 2>/dev/null | sed 's/.*llvm-ar//' | sort -t- -k2 -n | tail -1)
    if [ -n "$V" ] && command -v "clang$V" >/dev/null 2>&1; then
        TC="LLVM=$V"
    else
        echo "no riscv64 cross compiler or LLVM toolchain found" >&2
        exit 1
    fi
fi
echo "toolchain: $TC"

mk() { make -C "$SRC" O="$BUILD" ARCH=riscv $TC "$@"; }

# kernel fixes shipped with pyvm (linux/patches/*.patch); each is applied only
# if it is not already present and applies cleanly
for p in "$HERE"/linux/patches/*.patch; do
    [ -e "$p" ] || continue
    if patch -d "$SRC" -p1 -R --dry-run -s -f < "$p" >/dev/null 2>&1; then
        echo "patch already applied: $(basename "$p")"
    elif patch -d "$SRC" -p1 --dry-run -s -f < "$p" >/dev/null 2>&1; then
        echo "applying patch: $(basename "$p")"
        patch -d "$SRC" -p1 -s < "$p"
    else
        echo "warning: $(basename "$p") does not apply to this kernel, skipped" >&2
    fi
done

if [ "$MODE" = defconfig ]; then
    mk defconfig
else
    mk tinyconfig
    "$SRC/scripts/kconfig/merge_config.sh" -m -O "$BUILD" "$BUILD/.config" "$HERE/linux/pyvm.config"
fi
case "$TC" in LLVM*)
    # Fallback when the .got patch could not be applied: with clang/lld an
    # unplaced .got lands in the init section that is freed after boot, which
    # breaks kallsyms (its only user), so turn kallsyms off instead.
    if ! grep -q '\.got' "$SRC/arch/riscv/kernel/vmlinux.lds.S"; then
        echo "warning: .got not placed by the linker script; disabling CONFIG_KALLSYMS" >&2
        "$SRC/scripts/config" --file "$BUILD/.config" --disable KALLSYMS
    fi;;
esac
mk olddefconfig
mk -j"$(nproc)" Image
cp "$BUILD/arch/riscv/boot/Image" "$OUT"
echo "kernel image: $OUT"
