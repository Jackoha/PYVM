# Convenience targets for pyvm.  Everything here is optional; the VM itself is
# just `python -m pyvm`.
#
#   make busybox                      download a static riscv64 busybox
#   make rootfs                       build rootfs.cpio (initramfs) and disk.img (ext4)
#   make kernel LINUX=../linux-6.1.74 build Image from your Linux source tree
#   make run                          boot Image + rootfs.cpio
#   make run-disk                     boot Image with disk.img as the root filesystem
#   make run NET='user,hostfwd=tcp::8080-:80'   boot with a host port forwarded in
#   make test                         boot, run shell checks, power off (headless)
#   make test-net                     the same, plus the network checks
#   make test-slirp                   test the network stack alone (no kernel needed)
#   make test-persist                 check the disk keeps files across a reboot

PYTHON  ?= python3
LINUX   ?= linux-6.1.74
CONFIG  ?= tiny
MEM     ?= 128
IMAGE   ?= Image
NET     ?= user

.PHONY: busybox rootfs kernel run run-disk test test-net test-slirp test-persist

busybox: busybox.static
busybox.static:
	$(PYTHON) tools/get_busybox.py -o $@

rootfs: rootfs.cpio disk.img
rootfs.cpio disk.img: busybox.static tools/mkrootfs.py
	$(PYTHON) tools/mkrootfs.py --busybox busybox.static --cpio rootfs.cpio --ext4 disk.img --size 64

kernel:
	tools/build_linux.sh $(LINUX) $(CONFIG) $(abspath $(IMAGE))

run: $(IMAGE) rootfs.cpio
	$(PYTHON) -m pyvm -k $(IMAGE) -i rootfs.cpio -m $(MEM) --net $(NET)

run-disk: $(IMAGE) disk.img
	$(PYTHON) -m pyvm -k $(IMAGE) -d disk.img -m $(MEM) --net $(NET)

test: $(IMAGE) rootfs.cpio
	$(PYTHON) tests/boot_linux.py --kernel $(IMAGE) --initrd rootfs.cpio --mem $(MEM)

test-net: $(IMAGE) rootfs.cpio
	$(PYTHON) tests/boot_linux.py --kernel $(IMAGE) --initrd rootfs.cpio --mem $(MEM) --net

test-slirp:
	$(PYTHON) tests/test_net.py

test-persist: $(IMAGE) disk.img
	$(PYTHON) tests/persist.py --kernel $(IMAGE) --disk disk.img --mem $(MEM)
