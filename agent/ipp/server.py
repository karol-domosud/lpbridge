#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""IPP Everywhere front end: HTTP transport, operation dispatch, job registry.

The prototype answered every operation by writing the PDF to disk and calling
the job ``completed`` immediately. That is the bug invariant 2 calls out. Here
a job is only ``completed`` once :mod:`agent.pipeline` reports that the device
bytes reached the transport. While the container VM is booting the job stays
``pending`` with ``job-state-reason = resources-are-not-ready``; macOS shows
that honestly in the print queue.
"""

import collections
import os
import queue
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import attributes, codec
from .codec import (
    JOB_ABORTED, JOB_CANCELED, JOB_COMPLETED, JOB_PENDING, JOB_PROCESSING,
    TAG_CHARSET, TAG_ENUM, TAG_INTEGER, TAG_JOB, TAG_KEYWORD, TAG_LANGUAGE,
    TAG_NAME, TAG_OPERATION, TAG_PRINTER, TAG_TEXT, TAG_URI, first, group_dict,
)

MAX_JOB_BYTES = 512 * 1024 * 1024
_log_lock = threading.Lock()
#: recent log lines, so the menubar app / web UI can show what happened
LOG_RING = collections.deque(maxlen=500)


def log(message):
    line = "%s  %s" % (time.strftime("%H:%M:%S"), message)
    with _log_lock:
        import sys
        LOG_RING.append(line)
        sys.stderr.write(line + "\n")
        sys.stderr.flush()


def recent_log(limit=200):
    with _log_lock:
        return list(LOG_RING)[-limit:]


# ---------------------------------------------------------------------------
# Job registry + worker
# ---------------------------------------------------------------------------
class VirtualPrinter(object):
    """IPP printer state + a single-slot job worker.

    v0.1 is synchronous in spirit: one job processed at a time, in order.
    """

    def __init__(self, profile, caps, pipeline, boot_timeout=90, lifecycle=None):
        self.profile = profile
        self.caps = caps
        self.pipeline = pipeline
        self.boot_timeout = boot_timeout
        self.lifecycle = lifecycle
        self.start_time = time.time()
        self.lock = threading.Lock()
        self.next_job_id = 1
        self.jobs = {}
        self.offline = False           # device not currently reachable
        self.fault = None              # (reason, message) after repeated failures
        self._consecutive_failures = 0
        self._queue = queue.Queue()
        self._worker = threading.Thread(target=self._run, name="pipeline",
                                        daemon=True)
        self._worker.start()

    # -- job bookkeeping ----------------------------------------------
    def _new_job(self, operation_attributes, job_attributes):
        with self.lock:
            job_id = self.next_job_id
            self.next_job_id += 1
            job = {
                "id": job_id,
                "uuid": "urn:uuid:%s" % uuid.uuid4(),
                "operation": operation_attributes,
                "attributes": job_attributes,
                "chunks": [],
                "state": JOB_PENDING,
                "reasons": ["none"],
                "created": time.time(),
                "processing_at": 0,
                "completed_at": 0,
                "impressions": 0,
                "format": first(operation_attributes, "document-format"),
                "compression": first(operation_attributes, "compression"),
                "error": None,
                "prn": None,
            }
            self.jobs[job_id] = job
            return job

    def set_offline(self, offline):
        with self.lock:
            if bool(offline) != self.offline:
                self.offline = bool(offline)
                log("printer %s is now %s" % (
                    self.profile["id"], "offline" if offline else "back online"))

    def _note_job_result(self, ok, reason=None, message=None):
        with self.lock:
            if ok:
                self._consecutive_failures = 0
                self.fault = None
            elif reason in ("printer-stopped", "job-canceled-at-device"):
                self._consecutive_failures += 1
                if self._consecutive_failures >= 2:
                    self.fault = (reason, message or "printing failed repeatedly")

    def _set_state(self, job, state, reasons):
        with self.lock:
            job["state"] = state
            job["reasons"] = list(reasons) if isinstance(reasons, (list, tuple)) \
                else [reasons]
            if state == JOB_PROCESSING and not job["processing_at"]:
                job["processing_at"] = time.time()
            if state in (JOB_COMPLETED, JOB_ABORTED, JOB_CANCELED):
                job["completed_at"] = time.time()

    def job_group(self, job, printer_uri):
        with self.lock:
            state = job["state"]
            reasons = list(job["reasons"])
            created = int(job["created"])
            processing = int(job["processing_at"])
            completed = int(job["completed_at"])
            impressions = job["impressions"]
        return [
            ("job-uri", TAG_URI, ["%s/%d" % (printer_uri, job["id"])]),
            ("job-id", TAG_INTEGER, [job["id"]]),
            ("job-uuid", TAG_URI, [job["uuid"]]),
            ("job-printer-uri", TAG_URI, [printer_uri]),
            ("job-state", TAG_ENUM, [state]),
            ("job-state-reasons", TAG_KEYWORD, reasons),
            ("job-name", TAG_NAME,
             [first(job["operation"], "job-name")
              or first(job["attributes"], "job-name") or "Untitled"]),
            ("job-originating-user-name", TAG_NAME,
             [first(job["operation"], "requesting-user-name") or "anonymous"]),
            ("job-impressions-completed", TAG_INTEGER, [impressions]),
            ("time-at-creation", TAG_INTEGER, [created]),
            ("time-at-processing", TAG_INTEGER, [processing or 0]),
            ("time-at-completed", TAG_INTEGER, [completed or 0]),
            ("job-printer-up-time", TAG_INTEGER,
             [max(1, int(time.time() - self.start_time))]),
        ]

    # -- worker ------------------------------------------------------
    def _enqueue(self, job):
        self._queue.put(job["id"])

    def _run(self):
        while True:
            job_id = self._queue.get()
            job = self.jobs.get(job_id)
            if not job or job["state"] == JOB_CANCELED:
                self._queue.task_done()
                continue
            if self.lifecycle:
                self.lifecycle.job_started()
            try:
                self._process(job)
            finally:
                if self.lifecycle:
                    self.lifecycle.job_finished()
            self._queue.task_done()

    def _process(self, job):
        data = self.pipeline.assemble(job)
        if not data:
            self._set_state(job, JOB_ABORTED, ["job-canceled-at-device"])
            job["error"] = "no document data"
            return

        def on_state(state, reasons):
            if job["state"] == JOB_CANCELED:
                raise _Canceled()
            self._set_state(job, state, reasons)

        try:
            self._set_state(job, JOB_PENDING, ["job-incoming"])
            result = self.pipeline.process(job, data, on_state,
                                           boot_timeout=self.boot_timeout)
        except _Canceled:
            log("job %d canceled during processing" % job["id"])
            return
        except PipelineError as exc:
            log("job %d failed: %s" % (job["id"], exc))
            self._set_state(job, JOB_ABORTED, [exc.reason])
            job["error"] = str(exc)
            self._note_job_result(False, exc.reason, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - last-resort guard
            log("job %d crashed: %r" % (job["id"], exc))
            self._set_state(job, JOB_ABORTED, ["processing-to-stop-point"])
            job["error"] = repr(exc)
            self._note_job_result(False, "printer-stopped", repr(exc))
            return

        with self.lock:
            job["impressions"] = result.get("impressions", 0)
            job["prn"] = result.get("prn")
        reasons = ["job-completed-successfully"]
        if result.get("unsupported"):
            reasons = ["job-completed-with-warnings"]
        self._set_state(job, JOB_COMPLETED, reasons)
        self._note_job_result(True)
        log("job %d completed: %s (%d bytes -> device)"
            % (job["id"], os.path.basename(result.get("prn") or "?"),
               result.get("device_bytes", 0)))

    # -- dispatch --------------------------------------------------
    def handle(self, data, client_host, printer_uri):
        try:
            version, operation, request_id, groups, offset = \
                codec.parse_request(data)
        except (codec.IppParseError, Exception) as exc:  # noqa: BLE001
            log("bad IPP request from %s: %s" % (client_host, exc))
            return codec.build_response(codec.ERR_BAD_REQUEST, 0, [
                (TAG_OPERATION, [
                    ("attributes-charset", TAG_CHARSET, ["utf-8"]),
                    ("attributes-natural-language", TAG_LANGUAGE, ["en"]),
                    ("status-message", TAG_TEXT, ["Malformed IPP request"]),
                ])])

        operation_attributes = group_dict(groups, TAG_OPERATION)
        job_attributes = group_dict(groups, TAG_JOB)
        document = data[offset:]
        header = [
            ("attributes-charset", TAG_CHARSET, ["utf-8"]),
            ("attributes-natural-language", TAG_LANGUAGE, ["en"]),
        ]

        def respond(status, extra_groups=(), message=None):
            group = list(header)
            if message:
                group.append(("status-message", TAG_TEXT, [message]))
            return codec.build_response(
                status, request_id,
                [(TAG_OPERATION, group)] + list(extra_groups), version=(1, 1))

        log("op=0x%04x from %s (%d bytes)" % (operation, client_host, len(data)))

        if operation == codec.OP_GET_PRINTER_ATTRIBUTES:
            with self.lock:
                queued = sum(1 for j in self.jobs.values()
                             if j["state"] < JOB_CANCELED)
                busy = any(j["state"] == JOB_PROCESSING
                           for j in self.jobs.values())
                offline = self.offline
                fault = self.fault
            state, reasons, message = 3, ["none"], "Ready"
            if busy:
                state = 4
            if offline:
                reasons = ["offline-report", "connecting-to-device"]
                message = "Printer not connected"
            if fault:
                state = 5  # stopped -> macOS shows "Paused"
                reasons = [fault[0]]
                message = fault[1]
            return respond(codec.OK, [(TAG_PRINTER, attributes.printer_description(
                self.profile, self.caps, printer_uri, self.start_time, queued,
                state=state, state_reasons=tuple(reasons),
                accepting=True, state_message=message))])

        if operation == codec.OP_IDENTIFY_PRINTER:
            log("Identify-Printer requested")
            return respond(codec.OK)

        if operation == codec.OP_VALIDATE_JOB:
            fmt = first(operation_attributes, "document-format")
            if fmt and fmt not in ("application/pdf", "application/octet-stream"):
                return respond(codec.ERR_FORMAT_NOT_SUPPORTED,
                               message="Only application/pdf is supported")
            return respond(codec.OK)

        if operation == codec.OP_PRINT_JOB:
            job = self._new_job(operation_attributes, job_attributes)
            job["chunks"].append(document)
            self._enqueue(job)
            return respond(codec.OK,
                           [(TAG_JOB, self.job_group(job, printer_uri))])

        if operation == codec.OP_CREATE_JOB:
            job = self._new_job(operation_attributes, job_attributes)
            return respond(codec.OK,
                           [(TAG_JOB, self.job_group(job, printer_uri))])

        if operation == codec.OP_SEND_DOCUMENT:
            job_id = first(operation_attributes, "job-id")
            job = self.jobs.get(job_id)
            if not job:
                return respond(codec.ERR_NOT_FOUND, message="Unknown job-id")
            job["chunks"].append(document)
            if first(operation_attributes, "compression"):
                job["compression"] = first(operation_attributes, "compression")
            if first(operation_attributes, "document-format"):
                job["format"] = first(operation_attributes, "document-format")
            if first(operation_attributes, "last-document", True):
                self._enqueue(job)
            return respond(codec.OK,
                           [(TAG_JOB, self.job_group(job, printer_uri))])

        if operation == codec.OP_CLOSE_JOB:
            job_id = first(operation_attributes, "job-id")
            job = self.jobs.get(job_id)
            if not job:
                return respond(codec.ERR_NOT_FOUND, message="Unknown job-id")
            return respond(codec.OK,
                           [(TAG_JOB, self.job_group(job, printer_uri))])

        if operation in (codec.OP_GET_JOB_ATTRIBUTES, codec.OP_CANCEL_JOB):
            job_id = first(operation_attributes, "job-id")
            if job_id is None:
                uri = first(operation_attributes, "job-uri") or ""
                try:
                    job_id = int(uri.rstrip("/").rsplit("/", 1)[-1])
                except (ValueError, IndexError):
                    job_id = None
            job = self.jobs.get(job_id)
            if not job:
                return respond(codec.ERR_NOT_FOUND, message="Unknown job-id")
            if operation == codec.OP_CANCEL_JOB:
                if job["state"] in (JOB_COMPLETED, JOB_ABORTED):
                    return respond(codec.ERR_NOT_POSSIBLE,
                                   message="Job already finished")
                self._set_state(job, JOB_CANCELED, ["job-canceled-by-user"])
            return respond(codec.OK,
                           [(TAG_JOB, self.job_group(job, printer_uri))])

        if operation == codec.OP_GET_JOBS:
            jobs = sorted(self.jobs.values(), key=lambda j: j["id"])
            which = first(operation_attributes, "which-jobs", "not-completed")
            if which == "not-completed":
                jobs = [j for j in jobs if j["state"] < JOB_CANCELED]
            elif which == "completed":
                jobs = [j for j in jobs if j["state"] >= JOB_CANCELED]
            return respond(codec.OK, [(TAG_JOB, self.job_group(j, printer_uri))
                                      for j in jobs])

        return respond(codec.ERR_OP_NOT_SUPPORTED,
                       message="Operation 0x%04x not supported" % operation)


class _Canceled(Exception):
    pass


class PipelineError(Exception):
    def __init__(self, message, reason="job-canceled-at-device"):
        Exception.__init__(self, message)
        self.reason = reason


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------
class IppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lpbridge-ipp/0.1"

    def log_message(self, fmt, *args):
        pass

    def _read_body(self):
        encoding = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in encoding:
            chunks, total = [], 0
            while True:
                line = self.rfile.readline(65536).strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    raise codec.IppParseError("bad chunk header")
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(65536)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                total += size
                if total > MAX_JOB_BYTES:
                    raise codec.IppParseError("job exceeds %d bytes" % MAX_JOB_BYTES)
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_JOB_BYTES:
            raise codec.IppParseError("job exceeds %d bytes" % MAX_JOB_BYTES)
        return self.rfile.read(length)

    # -- routing -------------------------------------------------
    def _split_path(self):
        path = (self.path or "/").split("?", 1)[0].rstrip("/")
        return path or "/"

    def _resolve_printer(self):
        """``/ipp/print/<id>`` picks a printer; bare ``/ipp/print`` means the
        only one (back-compat with single-printer installs)."""
        path = self._split_path()
        registry = self.server.registry
        if path.startswith("/ipp/print/"):
            return registry.get(path[len("/ipp/print/"):])
        if path == "/ipp/print":
            ids = registry.ids()
            return registry.get(ids[0]) if len(ids) == 1 else None
        return None

    def _printer_uri(self, printer_id=None):
        host = self.headers.get("Host") or "%s:%d" % self.server.server_address
        if printer_id:
            return "ipp://%s/ipp/print/%s" % (host, printer_id)
        return "ipp://%s%s" % (host, self._split_path())

    def do_POST(self):
        if self._split_path().startswith("/api/"):
            self.server.control.handle_api(self, "POST")
            return
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip()
        if content_type != "application/ipp":
            self.send_error(400, "Expected application/ipp")
            return
        printer = self._resolve_printer()
        if printer is None:
            self.send_error(404, "No such printer")
            return
        try:
            body = self._read_body()
        except (codec.IppParseError, OSError) as exc:
            log("read failed: %s" % exc)
            self.send_error(400, "Bad request body")
            return
        response = printer.handle(
            body, self.client_address[0],
            self._printer_uri(printer.profile["id"]))
        self.send_response(200)
        self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self):
        self.server.control.handle_get(self)

    def do_DELETE(self):
        self.server.control.handle_api(self, "DELETE")


class PrinterRegistry(object):
    """Thread-safe id -> VirtualPrinter map shared by the HTTP server."""

    def __init__(self):
        self._lock = threading.Lock()
        self._printers = {}

    def register(self, printer_id, printer):
        with self._lock:
            self._printers[printer_id] = printer

    def unregister(self, printer_id):
        with self._lock:
            return self._printers.pop(printer_id, None)

    def get(self, printer_id):
        with self._lock:
            return self._printers.get(printer_id)

    def ids(self):
        with self._lock:
            return sorted(self._printers)

    def items(self):
        with self._lock:
            return sorted(self._printers.items())

    def __len__(self):
        with self._lock:
            return len(self._printers)


class IppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, registry, control=None):
        ThreadingHTTPServer.__init__(self, address, IppHandler)
        self.registry = registry
        self.control = control


# ---------------------------------------------------------------------------
# Bonjour advertisement via the dns-sd tool that ships with macOS
# ---------------------------------------------------------------------------
def advertise(profile, port):
    name = profile.get("name") or "lpbridge printer"
    uuid_bare = profile["uuid"].replace("urn:uuid:", "")
    txt = [
        "txtvers=1", "qtotal=1", "rp=ipp/print/%s" % profile["id"],
        "ty=%s" % name, "product=(%s)" % name,
        "note=lpbridge", "pdl=application/pdf",
        "Color=T", "Duplex=T", "Scan=F", "Fax=F",
        "PaperMax=legal-A4", "kind=document", "priority=0",
        "UUID=%s" % uuid_bare,
        "adminurl=http://localhost:%d/" % port,
        "TLS=", "air=none", "URF=none", "mopria-certified=",
    ]
    command = ["/usr/bin/dns-sd", "-R", name, "_ipp._tcp,_universal", ".",
               str(port)] + txt
    if not os.path.exists(command[0]):
        log("dns-sd not found; skipping Bonjour "
            "(add the printer with: lpadmin -m everywhere)")
        return None
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    log("advertising over Bonjour as '%s' (_ipp._tcp port %d)" % (name, port))
    return process
