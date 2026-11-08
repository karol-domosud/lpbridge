#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

# Build the bundled micro-VM disk image for the vfkit runtime (v0.4).
#
#   tools/build-vm.sh [out-dir]
#
# Produces  <out>/lpbridge-vm-arm64.img  (+ .img.zst + .sha256)
# an EFI-bootable GPT disk: ESP (systemd-boot + kernel + initrd) and an ext4
# root exported from container/Containerfile.vm.
#
# Needs podman for the *build* only -- end users just get the image. Nothing
# here needs privileged mode or loop devices (mke2fs -d + mtools).
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$REPO/build}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"      # podman -v needs an absolute path; a bare name is a named volume
ARCH="${ARCH:-arm64}"
PLATFORM="linux/$ARCH"
IMG_SIZE_MB="${IMG_SIZE_MB:-3072}"
ESP_START_MB=1
ESP_SIZE_MB=200
export CONTAINERS_MACHINE_PROVIDER="${CONTAINERS_MACHINE_PROVIDER:-applehv}"

echo "== building filter image =="
podman build -q -t lpbridge:latest \
    -f "$REPO/container/Containerfile" --platform "$PLATFORM" "$REPO/container" >/dev/null

echo "== building VM rootfs image =="
podman build -q -t lpbridge-vm:latest \
    -f "$REPO/container/Containerfile.vm" --platform "$PLATFORM" \
    --build-arg FILTER_IMAGE=localhost/lpbridge:latest "$REPO/container" >/dev/null

echo "== exporting VM rootfs =="
CID=$(podman create --platform "$PLATFORM" lpbridge-vm:latest true)
podman export "$CID" > "$OUT/rootfs.tar"
podman rm "$CID" >/dev/null

echo "== assembling disk image =="
# Everything below runs in one throwaway Linux container: lay down a GPT, write
# the ESP with mtools, write ext4 with mke2fs -d. No privileged mode, no loop.
podman run --rm --platform "$PLATFORM" \
    -v "$OUT:/out" \
    -e IMG_SIZE_MB="$IMG_SIZE_MB" -e ESP_START_MB="$ESP_START_MB" \
    -e ESP_SIZE_MB="$ESP_SIZE_MB" -e ARCH="$ARCH" \
    --entrypoint /bin/bash \
    docker.io/library/debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171 -euo pipefail -c '
        apt-get -qq update >/dev/null
        apt-get -qq install -y --no-install-recommends \
            gdisk dosfstools mtools e2fsprogs zstd coreutils >/dev/null

        ROOT=/rootfs; mkdir -p "$ROOT"
        tar -C "$ROOT" -xf /out/rootfs.tar
        rm -f /out/rootfs.tar

        KV=$(ls "$ROOT/lib/modules")
        cp "$ROOT/boot/vmlinuz-$KV"    /tmp/vmlinuz
        cp "$ROOT/boot/initrd.img-$KV" /tmp/initrd
        BOOTEFI="$ROOT/usr/lib/systemd/boot/efi/systemd-bootaa64.efi"
        [ -f "$BOOTEFI" ] || BOOTEFI=$(find "$ROOT" -name "systemd-boot*.efi" | head -1)

        IMG=/out/lpbridge-vm-${ARCH}.img
        rm -f "$IMG"
        truncate -s "${IMG_SIZE_MB}M" "$IMG"

        ESP_START=$((ESP_START_MB*1024*1024))
        ESP_SIZE=$((ESP_SIZE_MB*1024*1024))
        ROOT_START=$((ESP_START+ESP_SIZE))
        ROOT_SIZE=$(( IMG_SIZE_MB*1024*1024 - ROOT_START - 1024*1024 ))

        sgdisk -Z "$IMG" >/dev/null
        sgdisk -n "1:${ESP_START_MB}MiB:+${ESP_SIZE_MB}MiB" -t 1:ef00 -c 1:ESP \
               -n "2:$((ESP_START_MB+ESP_SIZE_MB))MiB:0" -t 2:8300 -c 2:root "$IMG" >/dev/null

        # --- ESP: a real mkfs.vfat filesystem in its own file, then dd in ---
        export MTOOLS_SKIP_CHECK=1
        ESP=/tmp/esp.img
        truncate -s "$ESP_SIZE" "$ESP"
        mkfs.vfat -F 32 -n LPBRIDGE "$ESP"
        mmd    -i "$ESP" ::/EFI ::/EFI/BOOT ::/EFI/systemd ::/loader ::/loader/entries
        mcopy  -i "$ESP" "$BOOTEFI" ::/EFI/BOOT/BOOTAA64.EFI
        mcopy  -i "$ESP" "$BOOTEFI" ::/EFI/systemd/systemd-bootaa64.efi
        mcopy  -i "$ESP" /tmp/vmlinuz ::/vmlinuz
        mcopy  -i "$ESP" /tmp/initrd  ::/initrd
        printf "default lpbridge\ntimeout 0\nconsole-mode max\neditor no\n" > /tmp/loader.conf
        mcopy  -i "$ESP" /tmp/loader.conf ::/loader/loader.conf
        printf "title lpbridge\nlinux /vmlinuz\ninitrd /initrd\noptions root=/dev/vda2 rw console=hvc0 console=tty0 loglevel=4 systemd.show_status=1 lpbridge.vm=1\n" > /tmp/entry.conf
        mcopy  -i "$ESP" /tmp/entry.conf ::/loader/entries/lpbridge.conf
        dd if="$ESP" of="$IMG" bs=1M seek="$ESP_START_MB" conv=notrunc status=none
        rm -f "$ESP"
        # also expose the raw kernel+initrd next to the image for the vfkit
        # --kernel path (used when EFI boot is unavailable)
        # Virtualization.framework'"'"'s direct kernel boot wants a raw arm64
        # Image; distributions ship compressed ones. Ubuntu 24.04: a plain
        # gzip Image. Newer kernels: an EFI zboot PE wrapping one (header at
        # 0x24 is "zimg", then payload offset and size as little-endian u32,
        # then the compression name). systemd-boot on the ESP boots either
        # as shipped; only the side copy for vfkit --kernel gets unwrapped.
        if [ "$(head -c 2 /tmp/vmlinuz | od -An -tx1 | tr -d " ")" = "1f8b" ]; then
            echo "  gzip kernel: decompressing for direct boot"
            gunzip -c /tmp/vmlinuz > /tmp/Image
            head -c 64 /tmp/Image | od -An -c | grep -q "A   R   M" || { echo "  ERROR: decompressed kernel is not an arm64 Image" >&2; exit 1; }
            cp /tmp/Image /out/lpbridge-vm-${ARCH}-vmlinuz
        elif [ "$(dd if=/tmp/vmlinuz bs=1 skip=36 count=4 2>/dev/null)" = "zimg" ]; then
            off=$(od -An -tu4 -j40 -N4 /tmp/vmlinuz | tr -d " ")
            len=$(od -An -tu4 -j44 -N4 /tmp/vmlinuz | tr -d " ")
            comp=$(dd if=/tmp/vmlinuz bs=1 skip=48 count=32 2>/dev/null | tr -d "\0")
            echo "  zboot kernel: payload at $off ($len bytes, $comp)"
            case "$comp" in
                gzip) dd if=/tmp/vmlinuz bs=1M iflag=skip_bytes,count_bytes skip="$off" count="$len" status=none | gunzip > /tmp/Image ;;
                zstd) dd if=/tmp/vmlinuz bs=1M iflag=skip_bytes,count_bytes skip="$off" count="$len" status=none | zstd -d > /tmp/Image ;;
                *) echo "  ERROR: unknown zboot compression: $comp" >&2; exit 1 ;;
            esac
            head -c 64 /tmp/Image | od -An -c | grep -q "A   R   M" || { echo "  ERROR: unwrapped kernel is not an arm64 Image" >&2; exit 1; }
            cp /tmp/Image /out/lpbridge-vm-${ARCH}-vmlinuz
        else
            cp /tmp/vmlinuz /out/lpbridge-vm-${ARCH}-vmlinuz
        fi
        cp /tmp/initrd  /out/lpbridge-vm-${ARCH}-initrd

        # --- ext4 root, populated at creation, no mount ---
        rm -rf "$ROOT/boot"/*    # kernel/initrd now live on the ESP
        BLOCKS=$((ROOT_SIZE/4096))
        mke2fs -q -F -t ext4 -b 4096 -L lpbridge-root \
               -d "$ROOT" -E offset=$ROOT_START "$IMG" "$BLOCKS"

        # A rootless `podman export` carries no device nodes and this container
        # has no CAP_MKNOD, so create the essential ones in the image with
        # debugfs. Without /dev/console the Debian initramfs redirects run-init
        # into a missing path, the handoff fails silently, and the VM
        # panic-loops right after mounting root.
        printf "%s\n" "cd /dev" \
            "mknod console c 5 1" "mknod null c 1 3" "mknod zero c 1 5" \
            "mknod tty c 5 0" "mknod ptmx c 5 2" "mknod random c 1 8" \
            "mknod urandom c 1 9" "mknod kmsg c 1 11" \
          | debugfs -w "$IMG?offset=$ROOT_START" >/dev/null 2>&1
        debugfs -R "stat /dev/console" "$IMG?offset=$ROOT_START" 2>/dev/null \
            | grep -q "05:01" && echo "  /dev/console node OK" \
            || { echo "  ERROR: /dev/console node missing" >&2; exit 1; }

        echo "== compressing =="
        cp "$IMG" /out/lpbridge-vm-${ARCH}.img.keep
        zstd -q -19 -f "$IMG" -o "$IMG.zst"
        mv /out/lpbridge-vm-${ARCH}.img.keep "$IMG"
        sha256sum "$IMG.zst" | tee "$IMG.zst.sha256"
        ls -la /out/
    '
echo
echo "built:"
echo "  $OUT/lpbridge-vm-$ARCH.img       (raw, for the vfkit runtime)"
echo "  $OUT/lpbridge-vm-$ARCH.img.zst   (compressed, for distribution)"
echo
echo "install for local use:"
echo "  mkdir -p ~/Library/Application\\ Support/lpbridge/vm"
echo "  cp $OUT/lpbridge-vm-$ARCH.img ~/Library/Application\\ Support/lpbridge/vm/"
echo "  agent picks it up as --runtime vfkit / --runtime auto"
