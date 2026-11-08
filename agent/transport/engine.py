#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Network printers: the engine's own CUPS backend already delivered the job.

The guest's ``socket://`` / ``ipp://`` / ``lpd://`` backend talks to the
printer directly over the Mac's NAT, so by the time cupsd reports the job
completed the bytes are the printer's problem (invariant 2). Nothing to do on
the Mac side except describe it.
"""

from .base import Transport


class EngineTransport(Transport):
    kind = "engine"

    def __init__(self, target):
        self.target = target

    def describe(self):
        return "engine -> %s" % (self.target or "network printer")

    def send(self, prn_path, job):
        return 0
