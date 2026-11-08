#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Golden byte-output test -- the contract under test is the device stream.

Each ``tests/golden/<name>/`` holds:
    input.pdf      the document
    options.json   {"make","model"[,"ppd"],"opts":[...],"job_meta":{...}}
    expected.prn   the device stream, captured once

The test does what the agent does: asks the engine's cupsd for the driver
(unless ``ppd`` pins a CUPS ppd-name), adds an ``lpbridge:///`` relay queue,
prints through it with the options as IPP job attributes, and compares the
relayed device stream after the normalizer zeroes the volatile PJL header
fields. Runs the real engine container; skipped when podman can't.
"""

import json
import os
import re
import shutil
import tempfile
import time
import unittest

from agent.engine import Engine
from agent.ipp import codec
from agent.runtime.podman import PodmanRuntime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(ROOT, "tests", "golden")


def normalize_prn(data):
    """Zero the ASCII PJL preamble fields that legitimately vary per job.
    QPDL / ZjStream / SPL all begin with a plain-text PJL header; the raster
    body after ``@PJL ENTER LANGUAGE`` is the deterministic part."""
    marker = b"@PJL ENTER LANGUAGE"
    idx = data.find(marker)
    head = data if idx < 0 else data[:idx]
    body = b"" if idx < 0 else data[idx:]
    head = re.sub(rb"SERVICEDATE=\d+", b"SERVICEDATE=0", head)
    head = re.sub(rb'USERNAME="[^"]*"', b'USERNAME="x"', head)
    head = re.sub(rb'JOBNAME="[^"]*"', b'JOBNAME="x"', head)
    head = re.sub(rb'(DATE|TIME)\s*=\s*"?[^"\r\n]*"?', rb"\1=0", head)
    return head + body


class Golden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = PodmanRuntime(machine_provider="applehv")
        try:
            cls.runtime.ensure_up(timeout=120)
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest("podman/engine not available: %s" % exc)
        cls.engine = Engine(cls.runtime)

    def _cases(self):
        if not os.path.isdir(GOLDEN):
            return
        for name in sorted(os.listdir(GOLDEN)):
            case = os.path.join(GOLDEN, name)
            if os.path.exists(os.path.join(case, "expected.prn")):
                yield name, case

    def test_golden(self):
        cases = list(self._cases())
        self.assertTrue(cases, "no golden fixtures under tests/golden/")
        for name, case in cases:
            with self.subTest(fixture=name):
                self._run_case(name, case)

    def _run_case(self, name, case):
        with open(os.path.join(case, "options.json")) as handle:
            spec = json.load(handle)

        device = {"make": spec.get("make"), "model": spec.get("model"),
                  "device_id": spec.get("device_id"), "transport": "usb-raw"}
        ppd_name = spec.get("ppd")
        if not ppd_name:
            candidates = self.engine.rank_drivers(device)
            self.assertTrue(candidates, "%s: the engine found no driver" % name)
            ppd_name = candidates[0]["ppd_name"]
            self.assertNotEqual(ppd_name, "everywhere", "%s ranked driverless first" % name)

        queue = "golden-%s" % name
        tmp = tempfile.mkdtemp(prefix="lpbridge-golden-")
        try:
            self.engine.add_queue(queue, "lpbridge:///%s" % queue, ppd_name, info=name)
            attributes = []
            for option in spec.get("opts", []):
                key, _sep, value = option.partition("=")
                attributes.append((key, codec.TAG_NAME, [value]))
            with open(os.path.join(case, "input.pdf"), "rb") as handle:
                pdf = handle.read()
            meta = spec.get("job_meta") or {}
            job_id = self.engine.print_job(queue, pdf, attributes,
                                           title=meta.get("title", "golden"),
                                           user=meta.get("user", "golden"))
            deadline = time.time() + 120
            state = None
            while time.time() < deadline:
                state = self.engine.job_state(job_id)
                if state["state"] >= codec.JOB_CANCELED:
                    break
                time.sleep(0.3)
            self.assertEqual(state and state["state"], codec.JOB_COMPLETED,
                             "%s: engine job ended %r" % (name, state))
            prn = os.path.join(self.runtime.outbound_dir(queue), "%d.prn" % job_id)
            self.assertTrue(os.path.exists(prn), "%s: no relayed device stream" % name)

            actual = normalize_prn(open(prn, "rb").read())
            expected = normalize_prn(
                open(os.path.join(case, "expected.prn"), "rb").read())
            if actual != expected:
                for label, blob in (("actual", actual), ("expected", expected)):
                    with open(os.path.join(tmp, label + ".norm"), "wb") as h:
                        h.write(blob)
                print("golden mismatch for %s; normalized copies in %s"
                      % (name, tmp))
            self.assertEqual(len(actual), len(expected),
                             "%s: device stream length changed" % name)
            self.assertEqual(actual, expected,
                             "%s: device stream differs from golden" % name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            self.engine.remove_queue(queue)
            shutil.rmtree(self.runtime.outbound_dir(queue), ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
