#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""vfkit runtime -- boots the bundled VM and drives its cupsd.

Skipped unless the VM image is installed at
``~/Library/Application Support/lpbridge/vm/lpbridge-vm-<arch>.img``
(``make vm-install``; ``LPBRIDGE_VM_DIR`` overrides). Boots the VM once for
the whole class.

The last test asserts *parity*: the golden fixture printed through the VM's
cupsd must be byte-identical (after the PJL-header normalizer) to the podman
golden -- same image, same filters, so the device stream must match.
"""

import json
import os
import shutil
import time
import unittest

from agent.engine import Engine
from agent.ipp import codec
from agent.runtime.vfkit import VfkitRuntime
from tests.test_golden import GOLDEN, normalize_prn


@unittest.skipUnless(VfkitRuntime.assets_present(),
                     "VM image not installed (make vm-install)")
class Vfkit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rt = VfkitRuntime(boot_timeout=60)
        try:
            cls.rt.ensure_up()
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest("VM did not boot: %s" % exc)
        cls.engine = Engine(cls.rt)

    @classmethod
    def tearDownClass(cls):
        cls.rt.shutdown()

    def test_engine_answers_over_nat(self):
        self.assertTrue(self.rt.engine_url().startswith("http://192.168."))
        self.assertGreaterEqual(len(self.engine.list_queues()), 0)

    def test_rank_in_vm(self):
        ranked = self.engine.rank_drivers({"make": "Samsung", "model": "ML-2580N",
                                           "device_id": "MFG:Samsung;MDL:ML-2580N;",
                                           "transport": "jetdirect"})
        self.assertTrue(ranked)
        self.assertIn("splix", ranked[0]["driver"])

    def test_x86_interpreters_are_registered(self):
        # x86-64 always has an interpreter; FEX is the one we expect on Apple silicon
        self.assertTrue(self.rt.x86_64_available())
        self.assertTrue(self.rt.i386_available())
        self.assertEqual(self.rt.x86_64_interpreter(), "FEX-x86_64")
        result = self.rt.exec(["sh", "-c", "ls /proc/sys/fs/binfmt_misc/; dpkg --print-foreign-architectures; "
                               "/lib64/ld-linux-x86-64.so.2 --version | head -1"])
        self.assertIn("FEX-x86_64", result["stdout"])
        self.assertNotIn("rosetta", result["stdout"])
        self.assertIn("qemu-i386", result["stdout"])
        self.assertIn("amd64", result["stdout"])
        self.assertIn("i386", result["stdout"])
        self.assertIn("ld.so", result["stdout"])   # an x86-64 binary ran, through binfmt

    def test_print_matches_podman_golden(self):
        case = os.path.join(GOLDEN, "samsung-ml-2580n")
        if not os.path.exists(os.path.join(case, "expected.prn")):
            self.skipTest("no samsung-ml-2580n golden")
        with open(os.path.join(case, "options.json")) as handle:
            spec = json.load(handle)
        queue = "vmtest-golden"
        try:
            self.engine.add_queue(queue, "lpbridge:///%s" % queue, spec["ppd"], info="vm golden")
            attributes = [(k, codec.TAG_NAME, [v]) for k, _s, v in
                          (o.partition("=") for o in spec.get("opts", []))]
            with open(os.path.join(case, "input.pdf"), "rb") as handle:
                pdf = handle.read()
            job_id = self.engine.print_job(queue, pdf, attributes, title="golden", user="golden")
            deadline = time.time() + 120
            state = None
            while time.time() < deadline:
                state = self.engine.job_state(job_id)
                if state["state"] >= codec.JOB_CANCELED:
                    break
                time.sleep(0.3)
            self.assertEqual(state and state["state"], codec.JOB_COMPLETED, state)
            prn = os.path.join(self.rt.outbound_dir(queue), "%d.prn" % job_id)
            deadline = time.time() + 30
            while not os.path.exists(prn) and time.time() < deadline:
                time.sleep(0.2)
            actual = normalize_prn(open(prn, "rb").read())
            expected = normalize_prn(open(os.path.join(case, "expected.prn"), "rb").read())
            self.assertEqual(len(actual), len(expected), "VM device stream length differs from golden")
            self.assertEqual(actual, expected, "VM device stream differs from golden")
        finally:
            self.engine.remove_queue(queue)
            shutil.rmtree(self.rt.outbound_dir(queue), ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
