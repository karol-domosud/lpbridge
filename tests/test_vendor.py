# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Vendor catalog: matching, archive members, architecture policy."""
import io
import json
import os
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent import vendor


def _catalog():
    return [
        {"id": "acme-native", "match": {"make": "^acme", "model": "^Laser"},
         "packages": [{"url": "https://x/acme.tar.gz", "sha256": "0" * 64,
                       "members": {"arm64": "*/arm64/*.deb", "amd64": "*/amd64/*.deb"}}]},
        {"id": "acme-x86", "match": {"make": "^acme", "model": "^Ink"}, "arch": "amd64",
         "packages": [{"url": "https://x/ink.deb", "sha256": "1" * 64}]},
        {"id": "old-32", "match": {"make": "^acme", "model": "^Dot"}, "arch": "i386",
         "packages": [{"url": "https://x/dot.deb", "sha256": "2" * 64}]},
    ]


class Vendor(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch("agent.vendor.SUPPORT", self.temp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_match_by_make_model_and_profile_override(self):
        entries = _catalog()
        self.assertEqual(vendor.match({"make": "ACME Inc", "model": "Laser 9"}, entries=entries)["id"], "acme-native")
        self.assertIsNone(vendor.match({"make": "Other", "model": "Laser 9"}, entries=entries))
        forced = vendor.match({"make": "Other", "model": "x"}, {"driver": {"vendor": "acme-x86"}}, entries=entries)
        self.assertEqual(forced["id"], "acme-x86")
        with self.assertRaises(vendor.VendorError):
            vendor.match({}, {"driver": {"vendor": "nope"}}, entries=entries)

    def test_profile_packages_win(self):
        entry = vendor.match({}, {"id": "p", "driver": {"packages": [{"url": "https://x/a.deb", "sha256": "a" * 64}]}},
                             entries=_catalog())
        self.assertEqual(entry["id"], "profile:p")

    def test_archive_prefers_native_arm64_member(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for path in ("drv/arm64/acme_1_arm64.deb", "drv/amd64/acme_1_amd64.deb"):
                info = tarfile.TarInfo(path)
                info.size = 3
                tar.addfile(info, io.BytesIO(b"deb"))
        blob = buffer.getvalue()
        import hashlib
        entry = dict(_catalog()[0])
        entry["packages"] = [dict(entry["packages"][0], sha256=hashlib.sha256(blob).hexdigest())]

        def fake_download(url, dest, sha256):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as handle:
                handle.write(blob)
        with patch("agent.vendor._download", side_effect=fake_download):
            files, arch = vendor.fetch(entry)
            self.assertEqual(arch, "arm64")
            self.assertTrue(files[0].endswith("acme_1_arm64.deb"))
            files, arch = vendor.fetch(entry, native_ok=False)
            self.assertEqual(arch, "amd64")

    def test_i386_and_missing_x86_64_are_refused_before_download(self):
        runtime = Mock()
        runtime.x86_64_available.return_value = False
        runtime.i386_available.return_value = False
        with patch("agent.vendor._download") as download:
            with self.assertRaisesRegex(vendor.VendorError, "qemu-i386"):
                vendor.install(runtime, _catalog()[2])
            download.assert_not_called()
        runtime.i386_available.return_value = True
        with patch("agent.vendor.fetch", return_value=(["/dot.deb"], "i386")):
            vendor.install(runtime, _catalog()[2])
            runtime.install_packages.assert_called_once_with(["/dot.deb"], timeout=1200)
        runtime.install_packages.reset_mock()
        with patch("agent.vendor.fetch", return_value=(["/x.deb"], "amd64")):
            with self.assertRaisesRegex(vendor.VendorError, "x86-64 interpreter"):
                vendor.install(runtime, _catalog()[1])
            runtime.install_packages.assert_not_called()

    def test_shipped_catalog_is_well_formed(self):
        for entry in vendor.catalog():
            self.assertTrue(entry.get("id") and entry.get("match"))
            for package in entry.get("packages", []):
                self.assertRegex(package["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(package["url"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
