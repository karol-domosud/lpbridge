#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Runtime ABC.

A runtime owns the Linux side: it boots the print engine (a stock CUPS server
in a guest), tells the agent where cupsd answers, shares one directory with
the guest, and runs the odd command in it. Everything else -- drivers, queues,
jobs -- goes over IPP (see :mod:`agent.engine`). Nothing here touches a USB
printer (invariant 1); the guest's own network backends drive LAN printers.
"""

import os


class RuntimeError_(Exception):
    pass


_SUPPORT = os.path.expanduser("~/Library/Application Support/lpbridge")


class Runtime(object):
    name = "base"

    #: the guest-side path of the shared directory
    GUEST_SHARE = "/spool"

    def share_root(self):
        """The Mac-side directory the guest sees as :attr:`GUEST_SHARE`."""
        raise NotImplementedError

    def job_spool_root(self):
        """Directory under which per-job spool dirs are created (shared)."""
        path = os.path.join(self.share_root(), "jobs")
        os.makedirs(path, exist_ok=True)
        return path

    def outbound_dir(self, printer_id):
        """Where the guest's ``lpbridge`` backend drops finished USB jobs."""
        path = os.path.join(self.share_root(), "outbound", printer_id)
        os.makedirs(path, exist_ok=True)
        return path

    def guest_path(self, mac_path):
        mac_path = os.path.abspath(mac_path)
        share = os.path.abspath(self.share_root())
        if mac_path != share and not mac_path.startswith(share + os.sep):
            raise RuntimeError_("path %s is not under the shared directory" % mac_path)
        return self.GUEST_SHARE + mac_path[len(share):]

    def ensure_up(self, timeout=90):
        """Block until cupsd in the guest answers, or raise."""
        raise NotImplementedError

    def engine_url(self):
        """``http://host:port`` of the guest cupsd, valid while up."""
        raise NotImplementedError

    def exec(self, argv, timeout=120):
        """Run a command in the guest. Returns ``{exit_code, stdout, stderr}``."""
        raise NotImplementedError

    def capabilities(self, ppd_path, profile=None):
        """Return a PPD's option groups, paper sizes and colour flag as a dict.

        Parsed on the Mac (``agent/ipp/ppd.py``): the PPD was read back
        from the engine already, and this needs no VM to be up.
        """
        from ..ipp import ppd
        try:
            return ppd.load(ppd_path)
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError_("could not read the PPD %s: %s" % (ppd_path, exc))

    def install_packages(self, files, timeout=600):
        """Install already-downloaded, already-verified ``.deb`` files."""
        import shutil
        import uuid
        work = os.path.join(self.job_spool_root(), "install-%s" % uuid.uuid4().hex)
        os.makedirs(work)
        try:
            for path in files:
                shutil.copyfile(path, os.path.join(work, os.path.basename(path)))
            result = self.exec(["/usr/local/bin/entrypoint.sh", "--install-packages",
                                self.guest_path(work)], timeout=timeout)
            if result["exit_code"] != 0:
                raise RuntimeError_("driver installation failed: %s"
                                    % (result["stderr"] or result["stdout"]).strip()[-2000:])
            return result["stdout"]
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def shell_command(self, key_path):
        """argv that opens an interactive root shell in the engine."""
        raise RuntimeError_("this runtime has no shell")

    def shell_start(self):
        """Bring up whatever ``shell_command`` connects to (sshd in the VM).

        The default is a no-op: a runtime whose shell needs no daemon
        (``podman exec``) has nothing to start. Called once per open shell,
        after ``ensure_up``; must be idempotent."""

    def shell_stop(self):
        """Take it down again once the last shell has closed. Idempotent."""

    def x86_64_available(self):
        """True when x86-64 binaries run in the guest (FEX or qemu-x86_64)."""
        return False

    def x86_64_interpreter(self):
        return None

    def i386_available(self):
        """True when 32-bit x86 binaries run in the guest (qemu-i386 registered)."""
        return False

    def shutdown(self):
        """Tear down the expensive part (the VM)."""
        raise NotImplementedError
