#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""usb-raw transport: push the device blob through a hidden macOS raw queue.

The queue itself is created by :mod:`agent.macos` (shared with the installer).
Here we only submit jobs and wait for CUPS to report them done -- CUPS owns
device locking, retries and offline detection.
"""

import os
import re
import subprocess
import time

from .base import Transport, TransportError
from .. import macos
from ..ipp.server import log

LP = "/usr/bin/lp"
LPSTAT = "/usr/bin/lpstat"
LPADMIN = "/usr/sbin/lpadmin"
_REQ_RE = re.compile(r"request id is (\S+)")


def _run(args, timeout=30):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


class RawQueueTransport(Transport):
    kind = "usb-raw"

    def __init__(self, profile, uri=None, match=None, poll_timeout=180):
        self.profile = profile
        self.queue = "_lpbridge_%s" % profile["id"]
        self.configured_uri = uri
        self.match = match or {}
        self.poll_timeout = poll_timeout
        self._raw_ppd = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "packaging", "raw.ppd")

    def describe(self):
        return "usb-raw via %s" % self.queue

    def _ensure_queue(self):
        try:
            return macos.ensure_raw_queue(
                self.queue, match=self.match, uri=self.configured_uri,
                raw_ppd=self._raw_ppd if os.path.exists(self._raw_ppd) else None)
        except RuntimeError as exc:
            raise TransportError(str(exc), reason="printer-stopped")

    def send(self, prn_path, job):
        if not os.path.exists(prn_path):
            raise TransportError("no .prn to deliver")
        self._ensure_queue()
        result = _run([LP, "-d", self.queue, "-o",
                       "document-format=application/vnd.cups-raw",
                       "-t", "lpbridge-%d" % job["id"], prn_path])
        if result.returncode != 0:
            err = result.stderr.strip()
            if "not found" in err.lower() or "unable" in err.lower():
                _run([LPADMIN, "-x", self.queue])  # identical printer swapped in
                self._ensure_queue()
                result = _run([LP, "-d", self.queue, "-o",
                               "document-format=application/vnd.cups-raw",
                               prn_path])
            if result.returncode != 0:
                raise TransportError("lp failed: %s" % result.stderr.strip(),
                                     reason="printer-stopped")
        self._wait_for(_request_id(result.stdout))
        return os.path.getsize(prn_path)

    def _wait_for(self, cups_id):
        if not cups_id:
            return
        deadline = time.time() + self.poll_timeout
        while time.time() < deadline:
            out = _run([LPSTAT, "-W", "not-completed", "-o", self.queue]).stdout
            if cups_id not in out:
                return
            time.sleep(2)
        raise TransportError("device did not accept the job within %ds"
                             % self.poll_timeout, reason="printer-stopped")


def _request_id(text):
    m = _REQ_RE.search(text or "")
    return m.group(1) if m else None
