#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""End-to-end job flow with a fake engine -- no VM, no cupsd, no printer.

The point under test is invariant 2: a job is ``completed`` only after the
transport has the bytes (USB relay) or the engine reports it done (network),
it sits ``pending`` with ``resources-are-not-ready`` while the engine boots,
and the engine's verdicts (aborted, canceled, printer-state messages) are
mirrored rather than guessed.
"""

import os
import shutil
import tempfile
import threading
import time
import unittest

from agent.ipp import codec
from agent.ipp.attributes import Capabilities
from agent.ipp.server import VirtualPrinter
from agent.pipeline import Pipeline
from agent.transport.base import Transport, TransportError

CAPS = Capabilities({"ppd": "/x.ppd", "nickname": "Fake",
                     "options": {"PageSize": {"default": "Letter",
                                              "choices": {"Letter": "L",
                                                          "A4": "A4"}}}})
PROFILE = {"id": "fake", "name": "Fake Printer", "uuid": "urn:uuid:fake",
           "manufacturer": "ACME", "location": "",
           "driver": {"ppd": "/x.ppd", "mode": "cupsfilter", "arch": "linux/arm64"},
           "transport": {"kind": "dryrun"}}

PDF = b"%PDF-1.4\n1 0 obj<< >>endobj\ntrailer<< >>\n%%EOF"


class FakeRuntime(object):
    def __init__(self, share, boot_delay=0.2):
        self.share = share
        self.boot_delay = boot_delay
        self.up = False
        self.events = []

    def ensure_up(self, timeout=90):
        if not self.up:
            self.events.append(("boot-start", time.time()))
            time.sleep(self.boot_delay)
            self.up = True
            self.events.append(("boot-done", time.time()))

    def outbound_dir(self, printer_id):
        path = os.path.join(self.share, "outbound", printer_id)
        os.makedirs(path, exist_ok=True)
        return path

    def shutdown(self):
        self.up = False


class FakeEngine(object):
    """Behaves like cupsd seen through agent.engine: accepts a job, runs it
    on a thread, drops a device stream on the share (relay queues), and
    answers job_state honestly."""

    def __init__(self, runtime, outcome="completed", delay=0.15, relay=True):
        self.runtime = runtime
        self.outcome = outcome
        self.delay = delay
        self.relay = relay
        self.jobs = {}
        self.submitted = []
        self.canceled = []
        self._next = 100

    def print_job(self, queue, document, job_attributes=None, document_format="application/pdf",
                  title="lpbridge", user="lpbridge"):
        job_id = self._next
        self._next += 1
        self.submitted.append((queue, document, list(job_attributes or []), title, user))
        self.jobs[job_id] = {"state": codec.JOB_PENDING, "reasons": ["none"], "message": "", "printer_reasons": []}
        threading.Thread(target=self._run, args=(queue, job_id), daemon=True).start()
        return job_id

    def _run(self, queue, job_id):
        job = self.jobs[job_id]
        job["state"] = codec.JOB_PROCESSING
        job["message"] = "Connecting to printer..."
        job["printer_reasons"] = ["connecting-to-device"]
        time.sleep(self.delay)
        if job_id in self.canceled:
            job.update(state=codec.JOB_CANCELED, reasons=["job-canceled-by-user"])
            return
        if self.outcome == "aborted":
            job.update(state=codec.JOB_ABORTED, reasons=["aborted-by-system"],
                       message="Filter failed", printer_reasons=[])
            return
        if self.relay:
            directory = self.runtime.outbound_dir(queue)
            with open(os.path.join(directory, "%d.prn" % job_id), "wb") as handle:
                handle.write(b"\x1b%-12345X@PJL\nFAKE-DEVICE-STREAM\n")
        job.update(state=codec.JOB_COMPLETED, reasons=["job-completed-successfully"],
                   message="", printer_reasons=[], impressions=1)

    def job_state(self, job_id):
        return dict(self.jobs[job_id])

    def cancel_job(self, job_id):
        self.canceled.append(job_id)


class RecordingTransport(Transport):
    kind = "recording"

    def __init__(self):
        self.sent_at = None
        self.bytes = None

    def send(self, prn_path, job):
        self.sent_at = time.time()
        self.bytes = os.path.getsize(prn_path)
        return self.bytes


def _print_job_request(pdf, job_group=None):
    groups = [
        (codec.TAG_OPERATION, [
            ("attributes-charset", codec.TAG_CHARSET, ["utf-8"]),
            ("attributes-natural-language", codec.TAG_LANGUAGE, ["en"]),
            ("printer-uri", codec.TAG_URI, ["ipp://localhost/ipp/print"]),
            ("requesting-user-name", codec.TAG_NAME, ["tester"]),
            ("job-name", codec.TAG_NAME, ["Unit Test Doc"]),
        ]),
    ]
    if job_group:
        groups.append((codec.TAG_JOB, job_group))
    # the document follows the IPP message (after TAG_END)
    return codec.build_response(codec.OP_PRINT_JOB, 1, groups) + pdf


def _get_job_request(job_id):
    return codec.build_response(codec.OP_GET_JOB_ATTRIBUTES, 2, [
        (codec.TAG_OPERATION, [
            ("attributes-charset", codec.TAG_CHARSET, ["utf-8"]),
            ("attributes-natural-language", codec.TAG_LANGUAGE, ["en"]),
            ("printer-uri", codec.TAG_URI, ["ipp://localhost/ipp/print"]),
            ("job-id", codec.TAG_INTEGER, [job_id]),
        ]),
    ])


class JobFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lpbridge-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _drive(self, runtime, transport, engine=None, relay=True, job_group=None):
        engine = engine or FakeEngine(runtime, relay=relay)
        pipeline = Pipeline(PROFILE, CAPS, runtime, transport, engine=engine,
                            queue=PROFILE["id"], relay=relay)
        printer = VirtualPrinter(PROFILE, CAPS, pipeline, boot_timeout=10)
        self.engine = engine
        uri = "ipp://localhost/ipp/print"
        resp = printer.handle(_print_job_request(PDF, job_group), "127.0.0.1", uri)
        _, _, _, groups, _ = codec.parse_request(resp)
        job = codec.group_dict(groups, codec.TAG_JOB)
        job_id = codec.first(job, "job-id")
        return printer, job_id, uri

    def _state(self, printer, job_id, uri):
        resp = printer.handle(_get_job_request(job_id), "127.0.0.1", uri)
        _, _, status, groups, _ = codec.parse_request(resp)
        job = codec.group_dict(groups, codec.TAG_JOB)
        return codec.first(job, "job-state"), codec.first(job, "job-state-reasons")

    def test_pending_while_booting_then_completed(self):
        runtime = FakeRuntime(self.tmp, boot_delay=0.4)
        transport = RecordingTransport()
        printer, job_id, uri = self._drive(runtime, transport)

        # immediately after submit: accepted but not yet completed
        state, reason = self._state(printer, job_id, uri)
        self.assertIn(state, (codec.JOB_PENDING, codec.JOB_PROCESSING))

        # and during the boot window the client is told why it is waiting
        saw_not_ready = (reason == "resources-are-not-ready")
        for _ in range(50):
            state, reason = self._state(printer, job_id, uri)
            if reason == "resources-are-not-ready":
                saw_not_ready = True
            if state in (codec.JOB_COMPLETED, codec.JOB_ABORTED):
                break
            time.sleep(0.02)
        self.assertTrue(saw_not_ready, "never reported resources-are-not-ready")

        deadline = time.time() + 5
        while time.time() < deadline:
            state, _ = self._state(printer, job_id, uri)
            if state in (codec.JOB_COMPLETED, codec.JOB_ABORTED):
                break
            time.sleep(0.05)
        self.assertEqual(state, codec.JOB_COMPLETED)
        self.assertIsNotNone(transport.sent_at)
        self.assertGreater(transport.bytes, 0)

    def test_job_not_completed_before_transport(self):
        runtime = FakeRuntime(self.tmp, boot_delay=0.1)
        transport = RecordingTransport()
        printer, job_id, uri = self._drive(runtime, transport)

        completed_at = None
        deadline = time.time() + 5
        while time.time() < deadline:
            state, _ = self._state(printer, job_id, uri)
            if state == codec.JOB_COMPLETED:
                completed_at = time.time()
                break
            time.sleep(0.02)
        self.assertIsNotNone(completed_at)
        # the client saw "completed" only after send() had run
        self.assertLessEqual(transport.sent_at, completed_at + 0.001)

    def test_engine_abort_aborts_job_and_skips_transport(self):
        runtime = FakeRuntime(self.tmp, boot_delay=0)
        transport = RecordingTransport()
        printer, job_id, uri = self._drive(runtime, transport,
                                           engine=FakeEngine(runtime, outcome="aborted"))
        deadline = time.time() + 5
        state = None
        while time.time() < deadline:
            state, reason = self._state(printer, job_id, uri)
            if state in (codec.JOB_COMPLETED, codec.JOB_ABORTED):
                break
            time.sleep(0.02)
        self.assertEqual(state, codec.JOB_ABORTED)
        self.assertEqual(reason, "job-canceled-at-device")
        self.assertIsNone(transport.sent_at)

    def test_network_queue_completes_when_engine_says_so(self):
        runtime = FakeRuntime(self.tmp, boot_delay=0)
        transport = RecordingTransport()
        printer, job_id, uri = self._drive(runtime, transport, relay=False)
        deadline = time.time() + 5
        while time.time() < deadline:
            state, _ = self._state(printer, job_id, uri)
            if state in (codec.JOB_COMPLETED, codec.JOB_ABORTED):
                break
            time.sleep(0.02)
        self.assertEqual(state, codec.JOB_COMPLETED)
        self.assertIsNone(transport.sent_at)   # the engine's backend delivered it

    def test_engine_printer_state_is_mirrored_while_printing(self):
        runtime = FakeRuntime(self.tmp, boot_delay=0)
        engine = FakeEngine(runtime, delay=0.6)
        printer, job_id, uri = self._drive(runtime, RecordingTransport(), engine=engine)
        seen = set()
        deadline = time.time() + 5
        while time.time() < deadline:
            resp = printer.handle(_get_job_request(job_id), "127.0.0.1", uri)
            _, _, _, groups, _ = codec.parse_request(resp)
            job = codec.group_dict(groups, codec.TAG_JOB)
            state = codec.first(job, "job-state")
            seen.update(job.get("job-state-reasons", (None, []))[1])
            if state in (codec.JOB_COMPLETED, codec.JOB_ABORTED):
                break
            time.sleep(0.02)
        self.assertIn("connecting-to-device", seen)

    def test_cancel_on_the_mac_cancels_in_the_engine(self):
        runtime = FakeRuntime(self.tmp, boot_delay=0)
        engine = FakeEngine(runtime, delay=1.0)
        printer, job_id, uri = self._drive(runtime, RecordingTransport(), engine=engine)
        time.sleep(0.3)
        cancel = codec.build_response(codec.OP_CANCEL_JOB, 3, [(codec.TAG_OPERATION, [
            ("attributes-charset", codec.TAG_CHARSET, ["utf-8"]),
            ("attributes-natural-language", codec.TAG_LANGUAGE, ["en"]),
            ("printer-uri", codec.TAG_URI, [uri]),
            ("job-id", codec.TAG_INTEGER, [job_id])])])
        printer.handle(cancel, "127.0.0.1", uri)
        time.sleep(1.2)
        self.assertEqual(engine.canceled, [100])
        state, _ = self._state(printer, job_id, uri)
        self.assertEqual(state, codec.JOB_CANCELED)

    def test_client_attributes_and_ppd_options_are_forwarded(self):
        runtime = FakeRuntime(self.tmp, boot_delay=0)
        job_group = [("copies", codec.TAG_INTEGER, [2]),
                     ("media", codec.TAG_KEYWORD, ["iso_a4_210x297mm"]),
                     ("sides", codec.TAG_KEYWORD, ["one-sided"])]
        printer, job_id, uri = self._drive(runtime, RecordingTransport(), job_group=job_group)
        deadline = time.time() + 5
        while time.time() < deadline:
            state, _ = self._state(printer, job_id, uri)
            if state in (codec.JOB_COMPLETED, codec.JOB_ABORTED):
                break
            time.sleep(0.02)
        self.assertEqual(state, codec.JOB_COMPLETED)
        queue, _doc, forwarded, title, user = self.engine.submitted[0]
        self.assertEqual(queue, "fake")
        names = {name: values for name, _tag, values in forwarded}
        self.assertEqual(names["copies"], [2])
        self.assertEqual(names["media"], ["iso_a4_210x297mm"])
        self.assertEqual(names.get("PageSize"), ["A4"])   # mapped from media via the PPD
        self.assertEqual(title, "Unit Test Doc")
        self.assertEqual(user, "tester")

    def test_dryrun_transport_writes_prn_and_sidecar(self):
        from agent.transport.dryrun import DryRunTransport
        out = os.path.join(self.tmp, "out")
        runtime = FakeRuntime(self.tmp)
        printer, job_id, uri = self._drive(runtime, DryRunTransport(out))
        deadline = time.time() + 5
        while time.time() < deadline:
            state, _ = self._state(printer, job_id, uri)
            if state == codec.JOB_COMPLETED:
                break
            time.sleep(0.03)
        self.assertEqual(state, codec.JOB_COMPLETED)
        files = sorted(os.listdir(out))
        self.assertTrue(any(f.endswith(".prn") for f in files), files)
        self.assertTrue(any(f.endswith(".json") for f in files), files)


if __name__ == "__main__":
    unittest.main()
