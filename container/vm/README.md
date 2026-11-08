# container/vm/ — the bundled micro-VM (v0.4)

Goal: run the filter chain with **no podman on the user's Mac**. `vfkit`
(Apple's Virtualization.framework CLI, MIT, ~8 MB — already shipped with the
podman install and bundled in `lpbridge.app`) boots a small Ubuntu guest built
from [`../Containerfile.vm`](../Containerfile.vm).

**Cold start: ~2 seconds** from `vfkit` exec to the guest agent signalling
ready — the v0.4 target.

## Pieces

| file | what |
|---|---|
| `../Containerfile.vm` | `FROM lpbridge` + systemd + kernel + this agent |
| `lpbridge-vm-agent` | the guest daemon (below) |
| `lpbridge-vm-agent.service` | systemd unit, `WantedBy=multi-user.target` |
| `../../tools/build-vm.sh` | rootfs → EFI-bootable GPT disk, no loop devices |
| `../../agent/runtime/vfkit.py` | the `Runtime` that drives vfkit + this agent |

## RPC — just files on one virtio-fs share

No vsock, no guest networking. The Mac shares
`~/Library/Application Support/lpbridge/vm-spool/` as mount tag `spool`; the
guest mounts it at `/spool` (via `/etc/fstab`).

```
Mac writes   /spool/inbox/<id>.req      one entrypoint.sh arg per line
guest runs   /usr/local/bin/entrypoint.sh <those args>
guest writes /spool/outbox/<id>/{stdout,stderr,exit}  then  .../.complete
Mac reads    the response, deletes the outbox dir
```

Job files (PDF, PPD, `.prn`) live under `/spool/jobs/…`, visible both sides.

The agent **polls** the inbox every 0.2 s rather than using inotify:
**virtio-fs does not deliver inotify events for host-side writes**, so
`inotifywait` never fires. This was a real bug, not a theoretical one.

## How the disk image is built (no privileged mode, no loop devices)

The applehv podman machine allows neither `losetup` nor `mount`, so
`build-vm.sh` writes the image directly:

* `sgdisk` lays a GPT on a sparse file (ESP `ef00` + root `8300`);
* the ESP is a real `mkfs.vfat` filesystem built in its own file, populated
  with `mtools` (`mmd`/`mcopy`), then `dd`'d into the partition —
  `mformat`-in-place produced a FAT some EDK2 builds reject;
* the root is `mke2fs -t ext4 -d <rootfs> -E offset=<n>`, which populates the
  filesystem at creation and handles usrmerge symlinks correctly;
* **device nodes are added afterwards with `debugfs -w … mknod`.** A rootless
  `podman export` carries no device nodes and the build container has no
  `CAP_MKNOD`. Without `/dev/console` the Debian initramfs's
  `exec run-init … >"${rootmnt}/dev/console"` redirect fails, the handoff dies
  **silently**, and the VM panic-loops right after mounting root. This cost a
  long debugging session; do not remove that step.

`build-vm.sh` also emits the kernel and initrd next to the image;
`vfkit.py` prefers a direct `--kernel`/`--initrd` boot (skipping the
ESP/bootloader entirely) when they are installed.

## Console gotcha

Debian's arm64 kernel has `CONFIG_VIRTIO_CONSOLE=m`, so `console=hvc0` prints
**nothing** until the initramfs loads the module (~0.5 s in) — early boot
messages and any initramfs `panic` text inside that window are lost, which is
what made the failure above look like "the VM never starts". The Containerfile
forces `virtio_console` (and the virtio transports) into the initramfs.
`vfkit` exposes no PL011, so there is no `ttyAMA0` and `earlycon` does not
help. To catch an early failure, boot with `panic=8` and watch for a reboot
loop in `console.log`.
