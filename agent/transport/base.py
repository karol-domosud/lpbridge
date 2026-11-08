#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Transport ABC.

Transport moves a device-ready ``.prn`` blob to the printer. On macOS this
reuses Apple's own CUPS backends (``usb://``, ``socket://``) rather than
writing USB code -- CUPS handles device locking, retries and offline
detection for free.
"""


class TransportError(Exception):
    def __init__(self, message, reason="job-canceled-at-device"):
        Exception.__init__(self, message)
        self.reason = reason


class Transport(object):
    kind = "base"

    def describe(self):
        return self.kind

    def send(self, prn_path, job):
        """Deliver ``prn_path`` to the device. Return bytes sent, or raise
        :class:`TransportError`. Must not return until the bytes are the
        device's problem, not ours (invariant 2)."""
        raise NotImplementedError
