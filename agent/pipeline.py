#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""The job pipeline: assemble -> the engine's cupsd -> (USB relay).

Kept separate from :mod:`agent.ipp.server` so the IPP layer owns nothing but
protocol and job state, and this layer owns nothing but "get this PDF printed
by the engine and mirror what the engine says about it".

The engine is a stock CUPS server in the guest (see :mod:`agent.engine`). A
job is submitted with the client's own IPP job attributes passed through, so
cupsd applies media, duplex, quality and vendor options exactly as it would
for a Linux desktop. Its job state is polled and mirrored honestly:

* network printers are driven by the engine's own backends -- the job is
  done when cupsd says so, and cupsd's printer-state message (``Connecting
  to printer``, ``Out of paper`` ...) is surfaced while it isn't;
* USB printers (and dry runs) use the ``lpbridge`` backend: the engine
  drops the device stream on the shared directory and the Mac-side
  transport pushes it through Apple's usb:// backend. Only then is the job
  complete (invariant 2).
"""

import os
import re
import time
import zlib

from .ipp import attributes, codec
from .ipp.client import IppError
from .ipp.codec import first as codec_first
from .ipp.server import PipelineError, log, _Canceled

_PDF_PAGE_RE = re.compile(rb"/Type\s*/Page[^s]")

# IPP job template attributes forwarded verbatim to the engine's cupsd. It
# validates and maps them onto the queue's PPD itself (media-col included).
PASSTHROUGH = (
    "copies", "media", "media-col", "sides", "print-color-mode", "print-quality",
    "printer-resolution", "page-ranges", "number-up", "orientation-requested",
    "output-bin", "finishings", "print-scaling", "job-hold-until", "job-priority",
    "multiple-document-handling", "print-content-optimize", "job-sheets",
)

# cupsd job-state-reasons -> the reason lpbridge reports to macOS
_ABORT_REASONS = {
    "aborted-by-system": "job-canceled-at-device",
    "job-canceled-by-user": "job-canceled-by-user",
    "job-canceled-at-device": "job-canceled-at-device",
    "job-canceled-by-operator": "job-canceled-by-operator",
    "cups-missing-filter-warning": "printer-stopped",
}


def _count_pages(pdf_bytes):
    try:
        return max(1, len(_PDF_PAGE_RE.findall(pdf_bytes)))
    except Exception:  # noqa: BLE001
        return 1


class Pipeline(object):
    def __init__(self, profile, caps, runtime, transport, engine=None, queue=None,
                 relay=True, keep_spool=False, job_timeout=1800, spool_root=None,
                 ppd_path=None, reprovision=None):
        self.profile = profile
        self.caps = caps
        self.runtime = runtime
        self.transport = transport
        self.engine = engine
        self.queue = queue or profile["id"]
        self.relay = relay            # output comes back through the lpbridge backend
        self.keep_spool = keep_spool
        self.job_timeout = job_timeout
        self.ppd_path = ppd_path
        self.reprovision = reprovision   # re-creates the engine queue if it vanished

    # -- server-side, no disk -----------------------------------------
    def assemble(self, job):
        data = b"".join(job["chunks"])
        if job.get("compression") == "gzip":
            try:
                data = zlib.decompress(data, 16 + zlib.MAX_WBITS)
            except zlib.error as exc:
                log("job %d: gzip decode failed (%s)" % (job["id"], exc))
                return b""
        return data

    # -- what the engine gets ------------------------------------------
    def job_attributes(self, job):
        """The client's job template attributes, plus the PPD options lpbridge
        maps from IPP for the vendor options cupsd would not know about."""
        forwarded = []
        source = job["attributes"]
        for name in PASSTHROUGH:
            if name in source:
                tag, values = source[name]
                forwarded.append((name, tag, list(values)))
        opts, unsupported = attributes.job_to_filter_options(source, self.caps)
        present = {name for name, _t, _v in forwarded}
        for option in opts:
            key, _sep, value = option.partition("=")
            if key and value and key not in present:
                forwarded.append((key, codec.TAG_NAME, [value]))
        return forwarded, unsupported

    # -- the worker path -------------------------------------------
    def process(self, job, data, on_state, boot_timeout=90):
        # 1. bring the engine up; job is honestly "pending" meanwhile
        on_state(3, ["resources-are-not-ready"])
        started = time.time()
        try:
            self.runtime.ensure_up(timeout=boot_timeout)
        except Exception as exc:  # noqa: BLE001
            raise PipelineError("print engine did not come up: %s" % exc,
                                reason="printer-stopped")
        log("job %d: engine ready in %.1fs" % (job["id"], time.time() - started))

        # 2. submit with the client's attributes passed through
        forwarded, unsupported = self.job_attributes(job)
        if unsupported:
            log("job %d: PPD cannot express %s" % (job["id"], unsupported))
        op = job["operation"]
        title = (codec_first(op, "job-name") or codec_first(job["attributes"], "job-name")
                 or "lpbridge")
        user = codec_first(op, "requesting-user-name") or "lpbridge"
        document_format = job.get("format") or "application/pdf"
        if document_format in ("application/octet-stream", None):
            document_format = "application/pdf"
        try:
            engine_job = self._submit(data, forwarded, document_format, title, user)
        except Exception as exc:  # noqa: BLE001
            raise PipelineError("engine refused the job: %s" % exc,
                                reason="job-canceled-at-device")
        log("job %d: engine job %d on queue %s" % (job["id"], engine_job, self.queue))
        on_state(5, ["job-printing"])

        # 3. mirror the engine's job state until it finishes
        outcome = self._wait(job, engine_job, on_state)

        # 4. USB / dry-run: the device stream comes back through the share
        device_bytes = 0
        prn_path = None
        if self.relay:
            prn_path = self._collect(job, engine_job)
            device_bytes = self.transport.send(prn_path, job)
            if not self.keep_spool:
                try:
                    os.unlink(prn_path)
                except OSError:
                    pass
                prn_path = None
        else:
            device_bytes = outcome.get("impressions", 0)

        return {
            "prn": prn_path,
            "device_bytes": device_bytes,
            "impressions": _count_pages(data),
            "unsupported": unsupported,
        }

    def _submit(self, data, forwarded, document_format, title, user):
        try:
            return self.engine.print_job(self.queue, data, forwarded,
                                         document_format=document_format,
                                         title=str(title), user=str(user))
        except IppError as exc:
            # the engine restarted (or its cupsd crashed) and lost the queue:
            # put it back exactly as it was and try once more
            if exc.status != codec.ERR_NOT_FOUND or not self.reprovision:
                raise
            log("queue %s is gone from the engine; re-creating it" % self.queue)
            self.reprovision()
            return self.engine.print_job(self.queue, data, forwarded,
                                         document_format=document_format,
                                         title=str(title), user=str(user))

    def _wait(self, job, engine_job, on_state):
        deadline = time.time() + self.job_timeout
        last_message = None
        interval = 0.4
        while True:
            try:
                state = self.engine.job_state(engine_job)
            except Exception as exc:  # noqa: BLE001
                raise PipelineError("lost contact with the engine: %s" % exc,
                                    reason="printer-stopped")
            message = state.get("message") or ""
            if message and message != last_message:
                log("job %d: engine: %s" % (job["id"], message))
                last_message = message
            if state["state"] == codec.JOB_COMPLETED:
                return state
            if state["state"] in (codec.JOB_CANCELED, codec.JOB_ABORTED):
                reasons = state.get("reasons") or []
                reason = "job-canceled-at-device"
                for candidate in reasons:
                    if candidate in _ABORT_REASONS:
                        reason = _ABORT_REASONS[candidate]
                        break
                raise PipelineError("engine %s the job: %s" % (
                    "canceled" if state["state"] == codec.JOB_CANCELED else "aborted",
                    message or ", ".join(reasons) or "no details"), reason=reason)
            if time.time() > deadline:
                self.engine.cancel_job(engine_job)
                raise PipelineError("printer did not take the job within %d minutes: %s"
                                    % (self.job_timeout // 60, message or "no response"),
                                    reason="printer-stopped")
            try:
                # keeps the client's view current; raises _Canceled if the
                # user canceled the job on the Mac
                on_state(codec.JOB_PROCESSING, self._reasons(state))
            except _Canceled:
                self.engine.cancel_job(engine_job)
                raise
            time.sleep(interval)
            interval = min(2.0, interval * 1.5)

    @staticmethod
    def _reasons(state):
        reasons = ["job-printing"]
        for reason in state.get("printer_reasons") or []:
            base = re.sub(r"-(report|warning|error)$", "", reason)
            if base in ("connecting-to-device", "media-empty", "media-jam", "media-needed",
                        "toner-empty", "toner-low", "marker-supply-empty", "marker-supply-low",
                        "cover-open", "door-open", "input-tray-missing", "offline"):
                reasons.append(base)
        return reasons

    def _collect(self, job, engine_job, timeout=60):
        directory = self.runtime.outbound_dir(self.profile["id"])
        path = os.path.join(directory, "%d.prn" % engine_job)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return path
            time.sleep(0.2)
        raise PipelineError("the engine finished the job but produced no device stream",
                            reason="document-format-error")
