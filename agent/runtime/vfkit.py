#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""vfkit runtime -- a bundled Virtualization.framework micro-VM.

No podman on the user's Mac: the print engine (cupsd + every open driver)
runs in a small Ubuntu guest booted directly by ``vfkit`` (Apple's
Virtualization.framework CLI, MIT). The guest image is built by
``tools/build-vm.sh`` and lives at
``~/Library/Application Support/lpbridge/vm/lpbridge-vm-<arch>.img``.

The guest has one NIC on the Mac's vmnet NAT (``virtio-net,nat``): cupsd is
reachable from the Mac at the address the guest agent writes to
``<share>/.vm-ip``, the guest's CUPS backends reach LAN printers, and nothing
on the LAN can reach the guest. x86 vendor filters run under FEX (x86-64)
and qemu (i386) inside the guest; nothing depends on Rosetta.

The odd command that IPP cannot carry (``lpadmin``, package installs) goes
over a file RPC on the virtio-fs share: the Mac writes
``<share>/inbox/<id>.req`` (one entrypoint.sh argument per line), the guest
agent runs it and writes ``<share>/outbox/<id>/{stdout,stderr,exit,.complete}``.
"""

import atexit
import json
import os
import platform
import shutil
import subprocess
import threading
import time
import uuid

from .base import Runtime, RuntimeError_
from ..ipp.server import log

SUPPORT = os.path.expanduser("~/Library/Application Support/lpbridge")
VFKIT_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))), "Resources", "bin", "vfkit"),
    "/opt/podman/bin/vfkit",
    "/opt/homebrew/bin/vfkit",
    "/usr/local/bin/vfkit",
]


def _arch_tag():
    return "arm64" if platform.machine() in ("arm64", "aarch64") else "amd64"


def _find_vfkit():
    for path in VFKIT_CANDIDATES:
        if os.path.exists(path):
            return path
    found = shutil.which("vfkit")
    if found:
        return found
    return None


def _image_path():
    return os.path.join(os.environ.get("LPBRIDGE_VM_DIR", os.path.join(SUPPORT, "vm")),
                        "lpbridge-vm-%s.img" % _arch_tag())


class VfkitRuntime(Runtime):
    name = "vfkit"

    def __init__(self, cpus=2, memory_mib=1536, boot_timeout=60):
        self.vfkit = _find_vfkit()
        self.image = _image_path()
        self.cpus = cpus
        self.memory = memory_mib
        self.boot_timeout = boot_timeout
        self.share = os.path.join(SUPPORT, "vm-spool")
        self.vmdir = os.path.dirname(self.image)
        # a stable MAC keeps the NAT lease (and so cupsd's address) the same
        self.mac_address = "5a:94:ef:1b:00:01"
        self._proc = None
        self._vm_lock = threading.RLock()
        os.makedirs(os.path.join(self.share, "inbox"), exist_ok=True)
        os.makedirs(os.path.join(self.share, "outbox"), exist_ok=True)
        # never leave a VM holding the disk image after the agent is gone
        atexit.register(self.shutdown)

    # -- availability --------------------------------------------
    @classmethod
    def assets_present(cls):
        return bool(_find_vfkit()) and os.path.exists(_image_path())

    def share_root(self):
        return self.share

    def _engine_host(self):
        return self.engine_url().split("//", 1)[1].split(":", 1)[0]

    def _known_hosts(self):
        return os.path.join(SUPPORT, "ssh", "known_hosts")

    def _pin_host_key(self):
        """Write known_hosts from the guest's own host key, read over the share.

        The file RPC is already the channel we trust with root, so the host
        key it hands back is authoritative -- no trust-on-first-use, and no
        stale pin after an engine update regenerates the keys (every image
        makes its own on first start)."""
        result = self.exec(["cat", "/etc/ssh/ssh_host_ed25519_key.pub"], timeout=30)
        parts = result["stdout"].split()
        if result["exit_code"] != 0 or len(parts) < 2:
            raise RuntimeError_("could not read the engine's host key: %s" % result["stderr"].strip())
        host = self._engine_host()
        line = "%s %s %s\n" % (host, parts[0], parts[1])
        known = self._known_hosts()
        os.makedirs(os.path.dirname(known), mode=0o700, exist_ok=True)
        try:
            with open(known) as handle:
                kept = [l for l in handle if not l.startswith(host + " ")]
        except OSError:
            kept = []
        with open(known + ".tmp", "w") as handle:
            handle.writelines(kept + [line])
        os.chmod(known + ".tmp", 0o600)
        os.replace(known + ".tmp", known)

    def shell_command(self, key_path):
        return ["/usr/bin/ssh", "-i", key_path, "-o", "IdentitiesOnly=yes",
                # -o values are parsed like ssh_config lines, i.e. split on
                # whitespace: "Application Support" needs the quotes
                "-o", 'UserKnownHostsFile="%s"' % self._known_hosts(),
                "-o", "StrictHostKeyChecking=yes", "-o", "LogLevel=ERROR",
                "-o", "ServerAliveInterval=30", "root@%s" % self._engine_host()]

    def shell_start(self):
        # sshd is not enabled in the image: it runs only between the first
        # shell opening and the last one closing, so the engine has no
        # listening service at all the rest of the time
        result = self.exec(["systemctl", "start", "ssh.service"], timeout=60)
        if result["exit_code"] != 0:
            raise RuntimeError_("the engine's sshd did not start: %s"
                                % (result["stderr"] or result["stdout"]).strip()[-500:])
        self._pin_host_key()      # the keys exist now (made by the unit's ExecStartPre)

    def shell_stop(self):
        if not self._running():
            return
        try:
            self.exec(["systemctl", "stop", "ssh.service"], timeout=30)
        except Exception as exc:  # noqa: BLE001 -- the VM may be going down anyway
            log("engine sshd not stopped: %s" % exc)

    def x86_64_available(self):
        return self._running() and os.path.exists(os.path.join(self.share, ".vm-x86-64"))

    def x86_64_interpreter(self):
        """``FEX-x86_64`` or ``qemu-x86_64`` (or None), as the guest reported."""
        try:
            with open(os.path.join(self.share, ".vm-x86-64")) as handle:
                return handle.read().strip() or None
        except OSError:
            return None

    def i386_available(self):
        return self._running() and os.path.exists(os.path.join(self.share, ".vm-i386"))

    def engine_url(self):
        try:
            with open(os.path.join(self.share, ".vm-ip")) as handle:
                address = handle.read().strip()
        except OSError:
            address = ""
        if not address:
            raise RuntimeError_("the engine VM has not reported an address yet")
        return "http://%s:631" % address

    # -- VM lifecycle ------------------------------------------
    def _running(self):
        return self._proc is not None and self._proc.poll() is None

    def ensure_up(self, timeout=None):
        with self._vm_lock:
            return self._ensure_up(timeout)

    def _ensure_up(self, timeout=None):
        if self._running() and os.path.exists(os.path.join(self.share, ".vm-ready")):
            return
        if not self.vfkit:
            raise RuntimeError_("vfkit binary not found")
        if not os.path.exists(self.image):
            raise RuntimeError_("VM image missing: %s (run tools/build-vm.sh)"
                                % self.image)
        timeout = timeout or self.boot_timeout
        for marker in (".vm-ready", ".vm-ip", ".vm-x86-64", ".vm-i386"):
            try:
                os.unlink(os.path.join(self.share, marker))
            except OSError:
                pass

        efistore = os.path.join(self.vmdir, "efistore")
        console = os.path.join(self.vmdir, "console.log")
        # a VM that was killed rather than stopped leaves its control socket
        # behind, and vfkit refuses to start with "address already in use"
        sock = os.path.join(self.vmdir, "vfkit.sock")
        if os.path.exists(sock):
            try:
                os.unlink(sock)
            except OSError:
                pass
        arch = _arch_tag()
        vmlinuz = os.path.join(self.vmdir, "lpbridge-vm-%s-vmlinuz" % arch)
        initrd = os.path.join(self.vmdir, "lpbridge-vm-%s-initrd" % arch)

        args = [self.vfkit, "--cpus", str(self.cpus),
                "--memory", str(self.memory)]
        if os.path.exists(vmlinuz) and os.path.exists(initrd):
            # direct kernel boot -- skips the ESP/bootloader handoff
            args += [
                "--kernel", vmlinuz, "--initrd", initrd,
                "--kernel-cmdline",
                "root=/dev/vda2 rw console=hvc0 quiet loglevel=4 lpbridge.vm=1",
            ]
        else:
            args += ["--bootloader",
                     "efi,variable-store=%s,create" % efistore]
        args += [
            "--device", "virtio-blk,path=%s" % self.image,
            "--device", "virtio-rng",
            "--device", "virtio-serial,logFilePath=%s" % console,
            "--device", "virtio-fs,sharedDir=%s,mountTag=spool" % self.share,
            # NAT: the guest opens connections to LAN printers, the Mac reaches
            # cupsd; needs no entitlement (bridged mode would)
            "--device", "virtio-net,nat,mac=%s" % self.mac_address,
            "--restful-uri", "unix://%s" % os.path.join(self.vmdir, "vfkit.sock"),
        ]
        try:
            from .. import shell
            shell.publish_key(self)
        except Exception as exc:  # noqa: BLE001
            log("engine shell key not published: %s" % exc)
        log("booting lpbridge VM ...")
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                      stderr=open(os.path.join(
                                          self.vmdir, "vfkit.log"), "w"))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._running():
                raise RuntimeError_(
                    "vfkit exited during boot -- see %s and %s"
                    % (os.path.join(self.vmdir, "vfkit.log"), console))
            if os.path.exists(os.path.join(self.share, ".vm-ready")):
                log("VM ready in %.1fs" % (timeout - (deadline - time.time())))
                return
            time.sleep(0.5)
        self.shutdown()
        raise RuntimeError_("VM did not signal ready within %ds "
                            "(console: %s)" % (timeout, console))

    def shutdown(self):
        with self._vm_lock:
            return self._shutdown()

    def _shutdown(self):
        if not self._running():
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
        log("lpbridge VM stopped")

    # -- RPC ------------------------------------------------
    def _call(self, args, timeout=300):
        self.ensure_up()
        rid = uuid.uuid4().hex
        req = os.path.join(self.share, "inbox", "%s.req" % rid)
        out = os.path.join(self.share, "outbox", rid)
        tmp = req + ".tmp"
        with open(tmp, "w") as handle:
            handle.write("\n".join(str(a) for a in args) + "\n")
        os.rename(tmp, req)

        deadline = time.time() + timeout
        done = os.path.join(out, ".complete")
        while time.time() < deadline:
            if os.path.exists(done):
                break
            if not self._running():
                raise RuntimeError_("VM died while handling a request")
            time.sleep(0.15)
        else:
            raise RuntimeError_("VM request timed out after %ds" % timeout)

        def _read(name, binary=False):
            path = os.path.join(out, name)
            if not os.path.exists(path):
                return b"" if binary else ""
            with open(path, "rb") if binary else open(path, "r", errors="replace") as handle:
                return handle.read()

        code = int((_read("exit") or "1").strip() or 1)
        result = {"exit_code": code, "stdout": _read("stdout"),
                  "stderr": _read("stderr")}
        shutil.rmtree(out, ignore_errors=True)
        return result

    # -- Runtime API ---------------------------------------
    def exec(self, argv, timeout=120):
        return self._call(["--exec"] + list(argv), timeout=timeout)

    def build(self, profile=None, force=False, context=None):
        raise RuntimeError_(
            "the vfkit runtime uses a prebuilt image; rebuild with "
            "tools/build-vm.sh, not from the agent")
