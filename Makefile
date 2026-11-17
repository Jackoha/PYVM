# Convenience targets for pyvm.  Everything here is optional; the VM itself is
# just `python -m pyvm`.
#
#   make busybox                      download a static riscv64 busybox
#   make rootfs                       build rootfs.cpio (initramfs) and disk.img (ext4)
#   make kernel LINUX=../linux-6.1.74 build Image from your Linux source tree
#   make run                          boot Image + rootfs.cpio
#   make run-disk                     boot Image with disk.img as the root filesystem
#   make alpine                       build alpine.img (Alpine Linux: apk, curl)
#   make apk PKG="htop jq"            install packages into alpine.img from the HOST
#                                     (seconds, instead of minutes inside the VM)
#   make run-alpine                   boot Alpine (a package manager in the guest)
#   make run NET='user,hostfwd=tcp::8080-:80'   boot with a host port forwarded in
#   make test                         boot, run shell checks, power off (headless)
#   make test-net                     the same, plus the network checks
#   make test-slirp                   test the network stack alone (no kernel needed)
#   make test-persist                 check the disk keeps files across a reboot
#   make test-applets                 check busybox's applet table can be read
#   make boot-phases                  where a boot spends its instructions

PYTHON  ?= python3
LINUX   ?= linux-6.1.74
CONFIG  ?= tiny
MEM     ?= 128
IMAGE   ?= Image
NET     ?= user

.PHONY: busybox rootfs kernel alpine apk run run-disk run-alpine test test-net \
	test-slirp test-persist test-launcher test-applets boot-phases

busybox: busybox.static
busybox.static:
	$(PYTHON) tools/get_busybox.py -o $@

rootfs: rootfs.cpio disk.img
rootfs.cpio: busybox.static tools/mkrootfs.py
	$(PYTHON) tools/mkrootfs.py --busybox busybox.static --cpio rootfs.cpio

# disk.img holds whatever the guest wrote, so it is built once and then left
# alone -- delete it yourself to start over.  (run.sh has always done this;
# the rule above used to build both, which meant touching tools/mkrootfs.py
# was enough to make `make run` overwrite the disk and lose the guest's files.)
disk.img:
	$(PYTHON) tools/mkrootfs.py --busybox busybox.static --ext4 $@ --size 64

kernel:
	tools/build_linux.sh $(LINUX) $(CONFIG) $(abspath $(IMAGE))

alpine: alpine.img
alpine.img: tools/mkalpine.py tools/mkrootfs.py
	$(PYTHON) tools/mkalpine.py --ext4 $@ --size 512

# Installing inside the guest takes minutes; doing it out here takes seconds.
apk: alpine.img
	@test -n "$(PKG)" || { echo 'usage: make apk PKG="htop jq"'; exit 1; }
	$(PYTHON) tools/mkalpine.py --into alpine.img $(patsubst %,--pkg %,$(PKG))

run: $(IMAGE) rootfs.cpio
	$(PYTHON) -m pyvm -k $(IMAGE) -i rootfs.cpio -m $(MEM) --net $(NET)

run-disk: $(IMAGE) disk.img
	$(PYTHON) -m pyvm -k $(IMAGE) -d disk.img -m $(MEM) --net $(NET)

run-alpine: $(IMAGE) alpine.img
	$(PYTHON) -m pyvm -k $(IMAGE) -d alpine.img -m 256 --net $(NET)

test: $(IMAGE) rootfs.cpio
	$(PYTHON) tests/boot_linux.py --kernel $(IMAGE) --initrd rootfs.cpio --mem $(MEM)

test-net: $(IMAGE) rootfs.cpio
	$(PYTHON) tests/boot_linux.py --kernel $(IMAGE) --initrd rootfs.cpio --mem $(MEM) --net

test-slirp:
	$(PYTHON) tests/test_net.py

test-persist: $(IMAGE) disk.img
	$(PYTHON) tests/persist.py --kernel $(IMAGE) --disk disk.img --mem $(MEM)

test-launcher:
	$(PYTHON) tests/test_launcher.py

test-applets:
	$(PYTHON) tests/test_applets.py

boot-phases: $(IMAGE) rootfs.cpio
	$(PYTHON) tests/boot_phases.py --kernel $(IMAGE) --initrd rootfs.cpio \
		--mem $(MEM) --repeat 2
