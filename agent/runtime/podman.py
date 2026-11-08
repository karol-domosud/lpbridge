#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Podman runtime.

Talks to the ``podman`` CLI only -- no bindings, no daemon. The engine is one
long-lived container (``lpbridge-engine``) running cupsd, with the shared
directory bind-mounted at ``/spool`` and port 631 published on 127.0.0.1
only. Podman's own NAT gives the container the same LAN reach the vfkit guest
has. Nothing here needs ``--privileged`` or ``--device`` (invariant 1).

This is the developer runtime (tests, image builds); end users get the
bundled vfkit VM.
"""

import json
import os
import shutil
import subprocess
import time

from .base import Runtime, RuntimeError_
from ..ipp.server import log

_PODMAN_CANDIDATES = [
    "/opt/podman/bin/podman",
    "/opt/homebrew/bin/podman",
    "/usr/local/bin/podman",
]
_SUPPORT = os.path.expanduser("~/Library/Application Support/lpbridge")


def _find_podman():
    for path in _PODMAN_CANDIDATES:
        if os.path.exists(path):
            return path
    found = shutil.which("podman")
    if found:
        return found
    raise RuntimeError_("podman not found; install it "
                        "(brew install podman) and run 'podman machine init'")


def _repo_path(*parts):
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, *parts)


class PodmanRuntime(Runtime):
    name = "podman"

    #: the engine image (cupsd + every open driver family)
    IMAGE = "lpbridge:latest"
    CONTAINER = "lpbridge-engine"

    def __init__(self, machine_provider="applehv", machine_name=None,
                 port=6631, share=None, container=None):
        self.podman = _find_podman()
        self.machine_provider = machine_provider
        self.machine_name = machine_name
        self.port = port
        self.container = container or self.CONTAINER
        self.share = share or os.path.join(_SUPPORT, "engine-spool")
        os.makedirs(self.share, exist_ok=True)
        self._up = False

    # -- helpers ----------------------------------------------------
    def _env(self):
        env = dict(os.environ)
        if self.machine_provider:
            env["CONTAINERS_MACHINE_PROVIDER"] = self.machine_provider
        return env

    def _run(self, args, capture=True, timeout=None, check=False, stdin=None):
        return subprocess.run(
            [self.podman] + args, env=self._env(),
            capture_output=capture, text=True, errors="replace",
            timeout=timeout, check=check, input=stdin)

    def share_root(self):
        return self.share

    def engine_url(self):
        return "http://127.0.0.1:%d" % self.port

    # -- machine lifecycle ---------------------------------------
    def _machine_state(self):
        args = ["machine", "inspect"]
        if self.machine_name:
            args.append(self.machine_name)
        result = self._run(args)
        if result.returncode != 0:
            return None  # no machine defined
        try:
            data = json.loads(result.stdout)
        except ValueError:
            return None
        if not data:
            return None
        return (data[0].get("State") or "").lower()

    def _machine_up(self, timeout=90):
        state = self._machine_state()
        if state is None:
            raise RuntimeError_(
                "no podman machine. Run once:\n"
                "  CONTAINERS_MACHINE_PROVIDER=%s podman machine init --now"
                % self.machine_provider)
        if state != "running":
            log("podman machine is %s; starting" % state)
            args = ["machine", "start"]
            if self.machine_name:
                args.append(self.machine_name)
            result = self._run(args, timeout=timeout)
            if result.returncode != 0:
                raise RuntimeError_("podman machine start failed: %s"
                                    % (result.stderr or result.stdout).strip())
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._info_ok():
                return
            time.sleep(2)
        raise RuntimeError_("podman machine did not become ready in %ds" % timeout)

    def _info_ok(self):
        result = self._run(["info", "--format", "{{.Host.Arch}}"], timeout=15)
        return result.returncode == 0

    # -- the engine container ------------------------------------
    def _container_state(self):
        result = self._run(["container", "inspect", "--format", "{{.State.Status}}",
                            self.container], timeout=30)
        return result.stdout.strip() if result.returncode == 0 else None

    def _cupsd_answers(self):
        import http.client
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            conn.request("GET", "/")
            status = conn.getresponse().status
            conn.close()
            return status in (200, 403, 404)
        except OSError:
            return False

    def ensure_up(self, timeout=90):
        if self._up and self._cupsd_answers():
            return
        self._machine_up(timeout)
        self.ensure_image()
        state = self._container_state()
        if state == "running":
            pass
        else:
            if state is not None:
                logs = self._run(["logs", "--tail", "30", self.container], timeout=30)
                for line in (logs.stderr + logs.stdout).strip().splitlines()[-8:]:
                    log("  engine (previous run): %s" % line)
                self._run(["rm", "-f", self.container], timeout=60)
            log("starting the engine container ...")
            args = ["run", "-d", "--name", self.container,
                    "--platform", "linux/arm64",
                    "-p", "127.0.0.1:%d:631" % self.port,
                    "-v", "%s:%s" % (self.share, self.GUEST_SHARE),
                    self.IMAGE, "--serve"]
            result = self._run(args, timeout=120)
            if result.returncode != 0:
                raise RuntimeError_("could not start the engine container: %s"
                                    % (result.stderr or result.stdout).strip())
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._cupsd_answers():
                self._up = True
                return
            if self._container_state() != "running":
                logs = self._run(["logs", "--tail", "20", self.container], timeout=30)
                raise RuntimeError_("engine container exited: %s"
                                    % (logs.stderr or logs.stdout).strip())
            time.sleep(0.5)
        raise RuntimeError_("cupsd in the engine container did not answer in %ds" % timeout)

    def shell_command(self, key_path):
        # no sshd in the dev container; podman gets us a tty directly
        return [self.podman, "exec", "-it", self.container, "bash", "-l"]

    def exec(self, argv, timeout=120):
        self.ensure_up()
        result = self._run(["exec", self.container] + list(argv), timeout=timeout)
        return {"exit_code": result.returncode, "stdout": result.stdout or "",
                "stderr": result.stderr or ""}

    def shutdown(self):
        if not self._up:
            return
        # stop the container; the podman machine belongs to the user
        self._run(["stop", "-t", "5", self.container], timeout=60)
        self._up = False
        log("engine container stopped")

    # -- image ----------------------------------------------------
    def has_image(self):
        result = self._run(["image", "exists", self.IMAGE])
        return result.returncode == 0

    def build(self, force=False, context=None):
        if self.has_image() and not force:
            return self.IMAGE
        context = context or _repo_path("container")
        args = ["build", "-t", self.IMAGE, "-f", os.path.join(context, "Containerfile"),
                "--platform", "linux/arm64", context]
        log("building image %s -- first run pulls ~700 MB, a few minutes" % self.IMAGE)
        result = self._run(args, capture=False, timeout=2400)
        if result.returncode != 0:
            raise RuntimeError_("image build failed (%s)" % self.IMAGE)
        # a new image means a new engine container
        self._run(["rm", "-f", self.container], timeout=60)
        self._up = False
        return self.IMAGE

    def ensure_image(self):
        if not self.has_image():
            self.build()
        return self.IMAGE
