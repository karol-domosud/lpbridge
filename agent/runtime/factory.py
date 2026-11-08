#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Choose a runtime.

* ``podman``  -- talks to a user-installed ``podman machine`` (v0.1-0.3).
* ``vfkit``   -- a bundled Virtualization.framework micro-VM, no podman
  needed (v0.4). Requires the VM image + the ``vfkit`` binary to be present
  (built by ``tools/build-vm.sh`` / shipped in the .app).
* ``auto``    -- vfkit if its assets are in place, else podman.
"""

import os

from .base import RuntimeError_
from ..ipp.server import log

SUPPORT = os.path.expanduser("~/Library/Application Support/lpbridge")


def _vfkit_assets():
    from .vfkit import VfkitRuntime
    return VfkitRuntime.assets_present()


def _podman():
    from .podman import PodmanRuntime
    return PodmanRuntime()


def make_runtime(kind="auto", machine_provider="applehv"):
    if kind == "podman":
        from .podman import PodmanRuntime
        return PodmanRuntime(machine_provider=machine_provider)

    if kind == "vfkit":
        from .vfkit import VfkitRuntime
        if not VfkitRuntime.assets_present():
            raise RuntimeError_(
                "vfkit runtime needs the VM image -- run tools/build-vm.sh "
                "or use --runtime podman")
        return VfkitRuntime()

    if kind == "auto":
        if not _vfkit_assets():
            log("runtime: podman (no VM image)")
            return _podman()
        from .vfkit import VfkitRuntime
        rt = VfkitRuntime(boot_timeout=45)
        try:
            rt.ensure_up()
            log("runtime: vfkit (bundled VM)")
            return rt
        except Exception as exc:  # noqa: BLE001
            log("vfkit VM unavailable (%s); falling back to podman" % exc)
            try:
                rt.shutdown()
            except Exception:  # noqa: BLE001
                pass
            return _podman()

    raise RuntimeError_("unknown runtime: %s" % kind)
