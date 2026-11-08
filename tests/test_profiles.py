#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Profile lint + the tiny YAML reader.

"Schema validation, PPD existence, digest pinning, fetch URL reachability"
PPD existence is checked in the container by the
golden test, not here.
"""

import os
import unittest

from agent.profiles import loader, schema

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES = os.path.join(ROOT, "profiles")
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "profiles")


class YamlSubset(unittest.TestCase):
    def test_block_mapping_and_sequence(self):
        data = loader.load(
            "id: x\n"
            "aliases:\n"
            "  - One\n"
            "  - Two\n"
            "driver:\n"
            "  mode: cupsfilter\n"
            "  packages: [a, b]\n")
        self.assertEqual(data["id"], "x")
        self.assertEqual(data["aliases"], ["One", "Two"])
        self.assertEqual(data["driver"]["mode"], "cupsfilter")
        self.assertEqual(data["driver"]["packages"], ["a", "b"])

    def test_flow_mapping_in_sequence(self):
        data = loader.load(
            "match:\n"
            "  usb:\n"
            '    - {vendor: "0x04e8", product: "0x3441"}\n')
        self.assertEqual(data["match"]["usb"][0],
                         {"vendor": "0x04e8", "product": "0x3441"})

    def test_comments_and_scalars(self):
        data = loader.load(
            "a: 1   # trailing\n"
            "# whole line\n"
            "b: true\n"
            "c: null\n"
            'd: "hash # inside string"\n')
        self.assertEqual(data["a"], 1)
        self.assertIs(data["b"], True)
        self.assertIsNone(data["c"])
        self.assertEqual(data["d"], "hash # inside string")

    def test_tab_indent_rejected(self):
        with self.assertRaises(loader.YamlError):
            loader.load("a:\n\tb: 1\n")


class Lint(unittest.TestCase):
    def test_shipped_profiles_dir_is_empty_or_valid(self):
        # profiles/ ships empty on purpose (README only); anything a user drops
        # there must still validate.
        for pid, profile in schema.load_dir(PROFILES).items():
            self.assertEqual(profile["id"], pid)

    def test_example_fixtures_are_valid(self):
        found = schema.load_dir(FIXTURES)
        self.assertEqual(set(found),
                         {"pinned-foo2zjs", "pinned-gdi", "jetdirect-foomatic", "minimal"})
        for pid, profile in found.items():
            self.assertEqual(profile["id"], pid)
            self.assertTrue(profile["uuid"].startswith("urn:uuid:"))
            self.assertIn(profile["transport"]["kind"],
                          ("usb-raw", "jetdirect", "ipp", "lpd", "dryrun"))
            ppd = profile["driver"].get("ppd")
            if ppd is not None:
                self.assertTrue(ppd == "everywhere" or ppd.endswith(".ppd") or "://" in ppd, ppd)
        self.assertEqual(found["jetdirect-foomatic"]["transport"]["host"],
                         "192.0.2.50")
        self.assertIsNone(found["minimal"]["driver"]["ppd"])

    def test_minimal_profile_is_valid(self):
        p = schema.normalise({"id": "x", "match": {
            "usb": [{"vendor": "0x04e8", "product": "0x3441"}]}})
        self.assertEqual(p["id"], "x")
        self.assertIsNone(p["driver"]["ppd"])   # resolved at run time

    def test_stable_uuid_for_id(self):
        a = schema.normalise({"id": "demo", "driver": {"make": "A"}})
        b = schema.normalise({"id": "demo", "driver": {"make": "B"}})
        self.assertEqual(a["uuid"], b["uuid"])

    def test_bad_ppd_spec_rejected(self):
        with self.assertRaises(schema.ProfileError):
            schema.normalise({"id": "x", "driver": {"ppd_spec": "magic:foo"}})

    def test_bad_transport_rejected(self):
        with self.assertRaises(schema.ProfileError):
            schema.normalise({"id": "x", "transport": {"kind": "carrier-pigeon"}})

    def test_auto_profile(self):
        p = schema.auto_profile("hp-lj-1018", "HP LaserJet 1018",
                                make="HP", model="LaserJet 1018",
                                device_id="MFG:HP;MDL:LaserJet 1018;")
        self.assertTrue(p["auto"])
        self.assertEqual(p["driver"]["model"], "LaserJet 1018")

    def test_vendor_packages_need_url_and_sha256(self):
        with self.assertRaises(schema.ProfileError):
            schema.normalise({"id": "blob", "driver": {"packages": [
                {"url": "https://x/y.deb"}]}})
        ok = schema.normalise({"id": "blob", "driver": {"packages": [
            {"url": "https://x/y.deb", "sha256": "a" * 64}], "ppd": "everywhere"}})
        self.assertEqual(ok["driver"]["packages"][0]["url"], "https://x/y.deb")
        self.assertEqual(ok["driver"]["ppd"], "everywhere")

    def test_legacy_ppd_specs_map_to_cups_names(self):
        self.assertEqual(schema.legacy_ppd_name("foomatic:Samsung-ML-1670/foo2qpdl"),
                         "foomatic:Samsung-ML-1670-foo2qpdl.ppd")
        self.assertEqual(schema.legacy_ppd_name("cupsdriver:foo2zjs:foo2zjs:0/ppd/x.ppd"),
                         "foo2zjs:0/ppd/x.ppd")
        self.assertEqual(schema.legacy_ppd_name("driverless"), "everywhere")
        with self.assertRaises(schema.ProfileError):
            schema.normalise({"id": "x", "driver": {"ppd_spec": "/usr/share/ppd/old.ppd"}})


class RuntimeFactory(unittest.TestCase):
    def test_auto_falls_back_to_podman_without_vm_image(self):
        from agent.runtime import factory, vfkit
        if vfkit.VfkitRuntime.assets_present():
            self.skipTest("VM image is installed on this machine")
        rt = factory.make_runtime("auto")
        self.assertEqual(rt.name, "podman")

    def test_unknown_runtime_rejected(self):
        from agent.runtime import factory
        from agent.runtime.base import RuntimeError_
        with self.assertRaises(RuntimeError_):
            factory.make_runtime("qemu")


if __name__ == "__main__":
    unittest.main()
