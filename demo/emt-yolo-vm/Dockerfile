# EMT YOLO as a very light KubeVirt VM (arm64), shipped as a containerDisk.
# The guest's root is the YOLO container's own filesystem (Debian slim, ONNX
# Runtime, OpenCV, the models), plus a kernel, a minimal initrd and drift-init
# as PID 1. Boot: UEFI -> a standalone GRUB (no config on disk) -> kernel.
# Build on an arm64 host: ./build.sh (needs drift-emt-yolo:udp in Docker).

# Stage 1: the guest root = the app image + kernel + a minimal initrd.
FROM drift-emt-yolo:udp AS rootfs
# Kernel first; then MODULES=list with only virtio and ext4, and the initrd is
# regenerated small (it only has to mount /dev/vda2).
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      linux-image-cloud-arm64 initramfs-tools zstd iproute2 \
 && sed -i 's/^MODULES=.*/MODULES=list/; s/^COMPRESS=.*/COMPRESS=zstd/' /etc/initramfs-tools/initramfs.conf \
 && printf 'virtio_pci\nvirtio_blk\nvirtio_net\next4\n' >> /etc/initramfs-tools/modules \
 && update-initramfs -u -k all \
 && ln -sf $(basename $(ls /boot/vmlinuz-* | head -1)) /boot/vmlinuz \
 && ln -sf $(basename $(ls /boot/initrd.img-* | head -1)) /boot/initrd.img \
 && ls -la /boot \
 && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man /usr/share/locale /root/.cache
COPY drift-init /sbin/drift-init
COPY drift.env /etc/drift.env
RUN chmod 755 /sbin/drift-init && mkdir -p /root

# Stage 2: assemble a GPT disk (EFI system partition + ext4 root) with no loop
# devices, and compress it to qcow2.
FROM debian:bookworm-slim AS disk
RUN apt-get update && apt-get install -y --no-install-recommends \
      grub-efi-arm64-bin dosfstools mtools e2fsprogs gdisk qemu-utils \
 && rm -rf /var/lib/apt/lists/*
COPY --from=rootfs / /rootfs
COPY grub.cfg /grub.cfg
RUN set -e; \
    rm -rf /rootfs/proc/* /rootfs/sys/* /rootfs/dev/* /rootfs/run/* /rootfs/tmp/* /rootfs/.dockerenv; \
    grub-mkstandalone -O arm64-efi -o /BOOTAA64.EFI --modules="part_gpt ext2 linux normal" "boot/grub/grub.cfg=/grub.cfg"; \
    mkfs.vfat -C /esp.img 16384 >/dev/null; \
    mmd -i /esp.img ::/EFI ::/EFI/BOOT; mcopy -i /esp.img /BOOTAA64.EFI ::/EFI/BOOT/BOOTAA64.EFI; \
    used=$(du -sm /rootfs | cut -f1); size=$((used + used / 4 + 128)); \
    mkfs.ext4 -q -L drift-root -d /rootfs /root.img ${size}M; \
    total=$((1 + 16 + size + 1)); truncate -s ${total}M /disk.img; \
    s2=34816; \
    sgdisk -n1:2048:34815 -t1:ef00 -n2:$s2:+${size}M -t2:8300 /disk.img; \
    dd if=/esp.img of=/disk.img bs=512 seek=2048 conv=notrunc status=none; \
    dd if=/root.img of=/disk.img bs=512 seek=$s2 conv=notrunc status=none; \
    sgdisk -v /disk.img; \
    qemu-img convert -c -O qcow2 /disk.img /disk.qcow2; \
    ls -la /disk.img /disk.qcow2

# Stage 3: the containerDisk (KubeVirt boots /disk/*).
FROM scratch
COPY --from=disk --chown=107:107 /disk.qcow2 /disk/emt-yolo.qcow2
