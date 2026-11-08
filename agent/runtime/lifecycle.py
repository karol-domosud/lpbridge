#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""VM lifecycle + power awareness.

    STOPPED --job--> STARTING --ready--> READY --job done--> IDLE(grace)
       ^                                                        |  |
       +-- grace expires / on battery / going to sleep ---------+  +- new job

Rules enforced here:
  * grace window (default 120 s) so five documents in a row don't boot five
    VMs; the timer resets on every job;
  * distinguish container lifecycle (cheap, per job) from VM lifecycle (the
    expensive, battery-costing part) -- only the VM is torn down here;
  * hold ``caffeinate -i`` while a job is in flight;
  * on battery, or when the machine is heading to sleep, tear the VM down
    immediately once no job is running -- don't let a forgotten VM eat a
    flight's worth of charge.
"""

import os
import subprocess
import threading
import time

from ..ipp.server import log

GRACE_SECONDS = 120

STOPPED, STARTING, READY, IDLE, DRAINING = "stopped", "starting", "ready", \
    "idle", "draining"


def _on_battery():
    try:
        out = subprocess.run(["/usr/bin/pmset", "-g", "batt"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "Battery Power" in out


class Lifecycle(object):
    def __init__(self, runtime, grace=GRACE_SECONDS, respect_battery=True):
        self.runtime = runtime
        self.grace = grace
        self.respect_battery = respect_battery
        self._lock = threading.Lock()
        self._inflight = 0
        self._caffeinate = None
        self._deadline = 0
        self._setup_grace = False
        self._state = STOPPED
        self._stop = False
        self._t = threading.Thread(target=self._loop, name="lifecycle",
                                   daemon=True)
        self._t.start()

    @property
    def state(self):
        return self._state

    # -- job hooks -------------------------------------------------
    def job_started(self, setup=False):
        with self._lock:
            # Setup gets the normal grace window even on battery, so selecting
            # a printer after lookup does not immediately require another boot.
            self._setup_grace = setup if self._inflight == 0 else (self._setup_grace or setup)
            self._inflight += 1
            self._state = READY
            if self._inflight == 1 and self._caffeinate is None \
                    and os.path.exists("/usr/bin/caffeinate"):
                self._caffeinate = subprocess.Popen(
                    ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
                log("caffeinate held (job in flight)")

    def job_finished(self):
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            if self._inflight == 0:
                self._deadline = time.time() + self.grace
                self._state = IDLE
                if self._caffeinate is not None:
                    self._caffeinate.terminate()
                    self._caffeinate = None
                    log("caffeinate released; VM idle grace %ds" % self.grace)

    def mark_starting(self):
        with self._lock:
            if self._state in (STOPPED, IDLE):
                self._state = STARTING

    # -- background loop ----------------------------------------
    def _loop(self):
        while not self._stop:
            time.sleep(5)
            with self._lock:
                idle = self._inflight == 0
                expired = self._deadline and time.time() > self._deadline
                state = self._state
                setup_grace = self._setup_grace
            if not idle:
                continue
            reason = None
            if expired:
                reason = "idle grace expired"
            elif state in (READY, IDLE) and not setup_grace and self.respect_battery and _on_battery():
                reason = "running on battery"
            if reason:
                self._teardown(reason)

    def _teardown(self, reason):
        with self._lock:
            if self._inflight or self._state in (STOPPED, DRAINING):
                return
            self._setup_grace = False
            self._state = DRAINING
            self._deadline = 0
            # Keep job_started from racing the idle shutdown decision.
            log("tearing down VM (%s)" % reason)
            try:
                self.runtime.shutdown()
            except Exception as exc:  # noqa: BLE001
                log("VM shutdown failed: %s" % exc)
            self._state = STOPPED

    def stop(self):
        self._stop = True
        with self._lock:
            if self._caffeinate is not None:
                self._caffeinate.terminate()
                self._caffeinate = None
