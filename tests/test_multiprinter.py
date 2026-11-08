#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""One process, many printers: path routing and the control API.

No podman, no VM -- fake printers in the registry and a real HTTP server on an
ephemeral port.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from agent.ipp import codec
from agent.ipp.attributes import Capabilities
from agent.ipp.server import IppServer, PrinterRegistry, VirtualPrinter
from agent.profiles import schema

CAPS = Capabilities({"ppd": "/x.ppd", "nickname": "Fake",
                     "options": {"PageSize": {"default": "Letter",
                                              "choices": {"Letter": "L"}}}})


class NullPipeline(object):
    def assemble(self, job):
        return b"%PDF-1.4\n"

    def process(self, job, data, on_state, boot_timeout=90):
        return {"prn": None, "device_bytes": 1, "impressions": 1,
                "unsupported": []}


def _printer(pid):
    profile = schema.auto_profile(pid, pid.upper(), make="ACME", model=pid)
    return VirtualPrinter(profile, CAPS, NullPipeline())


def _get_attrs_request():
    return codec.build_response(codec.OP_GET_PRINTER_ATTRIBUTES, 1, [
        (codec.TAG_OPERATION, [
            ("attributes-charset", codec.TAG_CHARSET, ["utf-8"]),
            ("attributes-natural-language", codec.TAG_LANGUAGE, ["en"]),
        ])])


class FakeManager(object):
    def __init__(self, registry):
        self.registry = registry
        self.runtime = type("R", (), {"name": "fake",
                                      "resolve": lambda *a, **k: {}})()
        self.removed = []

    def ids(self):
        return self.registry.ids()

    def entry(self, pid):
        vp = self.registry.get(pid)
        return {"profile": vp.profile} if vp else None

    def status(self):
        return [{"id": i, "name": i.upper(), "make_and_model": "Fake",
                 "transport": "dryrun", "endpoint": "", "auto": True,
                 "offline": False, "fault": None, "queued": 0, "jobs": [],
                 "driver": "d", "ppd_spec": "s"} for i in self.ids()]

    def add(self, profile, create_queue=True):
        return True, "ok"

    def remove(self, pid, drop_queue=True):
        self.removed.append(pid)
        self.registry.unregister(pid)
        return True, "ok"


class FakeDevices(object):
    def devices(self):
        return []

    def snapshot(self):
        return [], 0.0, False

    def request_refresh(self):
        pass


class MultiPrinter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from agent.control import ControlAPI
        cls.registry = PrinterRegistry()
        for pid in ("alpha", "beta"):
            cls.registry.register(pid, _printer(pid))
        cls.manager = FakeManager(cls.registry)
        control = ControlAPI(cls.manager, lambda d: None, FakeDevices())
        cls.server = IppServer(("127.0.0.1", 0), cls.registry, control)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def _ipp(self, path):
        req = urllib.request.Request(
            self._url(path), data=_get_attrs_request(),
            headers={"Content-Type": "application/ipp"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()

    # -- routing ---------------------------------------------------
    def test_each_printer_answers_on_its_own_path(self):
        for pid in ("alpha", "beta"):
            status, body = self._ipp("/ipp/print/%s" % pid)
            self.assertEqual(status, 200)
            _, _, _, groups, _ = codec.parse_request(body)
            printer = codec.group_dict(groups, codec.TAG_PRINTER)
            self.assertEqual(codec.first(printer, "printer-name"), pid.upper())
            self.assertIn("/ipp/print/%s" % pid,
                          codec.first(printer, "printer-uri-supported"))

    def test_unknown_printer_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._ipp("/ipp/print/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_bare_path_is_ambiguous_with_two_printers(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._ipp("/ipp/print")
        self.assertEqual(ctx.exception.code, 404)

    def test_bare_path_works_when_only_one_printer(self):
        removed = self.registry.unregister("beta")
        try:
            status, body = self._ipp("/ipp/print")
            self.assertEqual(status, 200)
            _, _, _, groups, _ = codec.parse_request(body)
            printer = codec.group_dict(groups, codec.TAG_PRINTER)
            self.assertEqual(codec.first(printer, "printer-name"), "ALPHA")
        finally:
            self.registry.register("beta", removed)

    # -- control API ----------------------------------------------
    def test_api(self):
        with urllib.request.urlopen(self._url("/"), timeout=5) as resp:
            # no HTML on this port; a browser gets a pointer, not a page
            self.assertEqual(resp.headers.get_content_type(), "text/plain")
            self.assertIn(b"lpbridge app", resp.read())
        with urllib.request.urlopen(self._url("/api/printers"), timeout=5) as r:
            ids = [p["id"] for p in json.loads(r.read())["printers"]]
        self.assertEqual(ids, ["alpha", "beta"])
        with urllib.request.urlopen(self._url("/api/health"), timeout=5) as r:
            self.assertEqual(json.loads(r.read())["printers"], 2)

    def test_mutating_call_requires_the_header(self):
        req = urllib.request.Request(self._url("/api/printers/alpha"),
                                     method="DELETE")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 403)
        self.assertNotIn("alpha", self.manager.removed)
        self.assertIsNotNone(self.registry.get("alpha"))

    def test_delete_with_header_removes(self):
        self.registry.register("gamma", _printer("gamma"))
        req = urllib.request.Request(self._url("/api/printers/gamma"),
                                     method="DELETE",
                                     headers={"X-lpbridge": "1"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        self.assertIn("gamma", self.manager.removed)
        self.assertIsNone(self.registry.get("gamma"))


class SpoolIsolation(unittest.TestCase):
    def test_job_spool_dirs_are_printer_scoped(self):
        """Two printers both number jobs from 1 -- their spool dirs must not
        collide (they did, and the jobs clobbered each other)."""
        import os
        import shutil
        import tempfile
        from agent.pipeline import Pipeline

        tmp = tempfile.mkdtemp(prefix="lpbridge-spool-")
        seen = []

        class Recorder(object):
            name = "rec"

            def ensure_up(self, timeout=90):
                pass

            def outbound_dir(self, printer_id):
                path = os.path.join(tmp, "outbound", printer_id)
                os.makedirs(path, exist_ok=True)
                return path

        runtime = Recorder()

        class Sink(object):
            def send(self, prn_path, job):
                seen.append(prn_path)
                return 1

        class Cups(object):
            # both queues get engine job-id 1: the outbound path must still differ
            def print_job(self, queue, document, job_attributes=None, **kw):
                with open(os.path.join(runtime.outbound_dir(queue), "1.prn"), "wb") as h:
                    h.write(b"x")
                return 1

            def job_state(self, job_id):
                return {"state": 9, "reasons": ["job-completed-successfully"], "message": ""}

        try:
            for pid in ("alpha", "beta"):
                profile = schema.auto_profile(pid, pid)
                pipe = Pipeline(profile, CAPS, runtime, Sink(), engine=Cups(), queue=pid,
                                keep_spool=True)
                job = {"id": 1, "operation": {}, "attributes": {},
                       "chunks": [b"%PDF-1.4\n"]}
                pipe.process(job, b"%PDF-1.4\n", lambda *a: None)
            self.assertEqual(len(seen), 2)
            self.assertNotEqual(seen[0], seen[1])
            self.assertIn("/alpha/", seen[0])
            self.assertIn("/beta/", seen[1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
