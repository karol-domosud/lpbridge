# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud
"""The PPD read on the Mac: sizes by dimension, colour and resolution behind
vendor option names, custom sizes, and the job options that come back."""
import os
import unittest

from agent.ipp import attributes, codec, ppd
from agent.ipp.attributes import Capabilities

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "vendor-color.ppd")


class Parsing(unittest.TestCase):
    def setUp(self):
        self.data = ppd.load(FIXTURE)
        self.caps = Capabilities(self.data)

    def test_every_openui_group_is_kept(self):
        self.assertEqual(self.data["version"], ppd.VERSION)
        self.assertTrue(self.data["color_device"])
        self.assertIn("BRMonoColor", self.data["options"])
        self.assertEqual(self.data["options"]["BRMonoColor"]["default"], "Auto")
        self.assertEqual(self.data["options"]["BRMonoColor"]["choices"]["FullColor"], "Color")

    def test_paper_dimensions_and_custom_range(self):
        self.assertEqual(self.data["paper"]["A4"], {"width": 595.0, "height": 842.0,
                                                    "imageable": [12.0, 12.0, 583.0, 830.0]})
        self.assertEqual(self.data["custom_page_size"], {"min": [100.0, 100.0], "max": [842.0, 1440.0]})

    def test_gzipped_ppd_loads(self):
        import gzip, tempfile
        with open(FIXTURE, "rb") as source:
            payload = gzip.compress(source.read())
        with tempfile.NamedTemporaryFile(suffix=".ppd.gz", delete=False) as handle:
            handle.write(payload)
        try:
            self.assertEqual(ppd.load(handle.name)["nickname"], "Test Vendor Color Laser")
        finally:
            os.unlink(handle.name)

    def test_malformed_lines_are_skipped(self):
        data = ppd.parse("*OpenUI *Broken\n*PaperDimension X: \"abc def\"\n*PageSize A4/A4: \"\"\n")
        self.assertEqual(data["paper"], {})
        self.assertEqual(data["nickname"], "Generic Printer")


class Media(unittest.TestCase):
    def setUp(self):
        self.caps = Capabilities(ppd.load(FIXTURE))

    def test_every_ppd_size_is_offered_including_odd_ones(self):
        media = self.caps.media_supported()
        self.assertEqual(media[:4], ["iso_a4_210x297mm", "na_letter_8.5x11in",
                                     "iso_a3_297x420mm", "na_index-3x5_3x5in"])
        # a size no table knows gets an oe_ name carrying its dimensions
        self.assertTrue(media[4].startswith("oe_195x270mm_"))
        self.assertEqual(attributes.parse_pwg_size(media[4]), (19509, 26988))
        receipt = media[5]
        self.assertTrue(receipt.startswith("oe_receipt_"))
        self.assertEqual(attributes.parse_pwg_size(receipt), (5786, 50800))

    def test_media_maps_back_by_keyword_dimensions_and_custom_range(self):
        c = self.caps
        self.assertEqual(c.pagesize_for_media("iso_a3_297x420mm"), "A3")
        # a client naming the same size differently still lands on the right choice
        self.assertEqual(c.pagesize_for_media("custom_210x297mm"), "A4")
        self.assertEqual(c.pagesize_for_media("oe_whatever_195x270mm"), "195x270mm")
        # inside the custom range: cupsd's Custom.WxH in points
        self.assertEqual(c.pagesize_for_media("custom_100x200mm"), "Custom.283x567")
        # outside it: nothing -- the job reports it and prints on the default
        self.assertIsNone(c.pagesize_for_media("iso_a0_841x1189mm"))

    def test_margins_come_from_imageable_area(self):
        self.assertEqual(self.caps.media_margins("iso_a4_210x297mm"), (423, 423, 423, 423))
        self.assertEqual(self.caps.media_margins(self.caps.media_supported()[5]), (0, 0, 0, 0))

    def test_printer_description_carries_sizes_and_custom_range(self):
        attrs = dict((n, (t, v)) for n, t, v in attributes.printer_description(
            {"name": "x", "uuid": "urn:uuid:1"}, self.caps, "ipp://x", 0, 0))
        self.assertEqual(len(attrs["media-col-database"][1]), 6)
        sizes = attrs["media-size-supported"][1]
        self.assertEqual(len(sizes), 7)
        self.assertEqual(sizes[-1]["x-dimension"], (codec.TAG_RANGE, [(3528, 29704)]))
        self.assertIn(423, attrs["media-left-margin-supported"][1])
        self.assertEqual(attrs["print-color-mode-supported"][1], ["color", "monochrome", "auto"])
        self.assertEqual(attrs["color-supported"][1], [True])
        self.assertEqual(attrs["printer-resolution-supported"][1], [(600, 600, 3), (600, 2400, 3)])
        self.assertEqual(attrs["media-source-supported"][1], ["auto", "tray-1", "manual"])


class VendorOptions(unittest.TestCase):
    def setUp(self):
        self.caps = Capabilities(ppd.load(FIXTURE))

    def test_roles_resolve_to_vendor_names(self):
        c = self.caps
        self.assertEqual(c.option_for("ColorModel"), "BRMonoColor")
        self.assertEqual(c.option_for("Resolution"), "BRResolution")
        self.assertEqual(c.option_for("InputSlot"), "BRInputSlot")
        self.assertEqual(c.option_for("MediaType"), "BRMediaType")
        self.assertIsNone(c.option_for("OutputBin"))

    def test_job_options_use_the_vendor_names(self):
        job = {"print-color-mode": (codec.TAG_KEYWORD, ["monochrome"]),
               "media-col": (codec.TAG_BEG_COLLECTION, [{
                   "media-size": (codec.TAG_BEG_COLLECTION, [{
                       "x-dimension": (codec.TAG_INTEGER, [29700]),
                       "y-dimension": (codec.TAG_INTEGER, [42000])}]),
                   "media-source": (codec.TAG_KEYWORD, ["tray-1"]),
                   "media-type": (codec.TAG_KEYWORD, ["cardstock"])}]),
               "printer-resolution": (codec.TAG_RESOLUTION, [(600, 2400, 3)]),
               "sides": (codec.TAG_KEYWORD, ["two-sided-short-edge"])}
        opts, unsupported = attributes.job_to_filter_options(job, self.caps)
        self.assertEqual(unsupported, [])
        self.assertEqual(sorted(opts), sorted([
            "BRMonoColor=Mono", "PageSize=A3", "BRInputSlot=Tray1", "BRMediaType=Thick",
            "BRResolution=600x2400dpi", "Duplex=DuplexTumble"]))

    def test_color_and_auto_pick_the_right_choices(self):
        for mode, choice in (("color", "FullColor"), ("auto", "Auto"), ("monochrome", "Mono")):
            opts, _ = attributes.job_to_filter_options(
                {"print-color-mode": (codec.TAG_KEYWORD, [mode])}, self.caps)
            self.assertIn("BRMonoColor=%s" % choice, opts)

    def test_anisotropic_resolution_never_matches_the_square_one(self):
        self.assertIsNone(attributes._dpi_tuple_to_ppd((600, 2400, 3), ["600dpi", "1200dpi"]))
        self.assertEqual(attributes._dpi_tuple_to_ppd((600, 600, 3), ["600dpi", "600x2400dpi"]), "600dpi")

    def test_color_device_without_any_colour_option_still_offers_colour(self):
        caps = Capabilities({"nickname": "x", "color_device": True,
                             "options": {"PageSize": {"default": "A4", "choices": {"A4": "A4"}}}})
        self.assertEqual(caps.color_modes(), ["color", "monochrome", "auto"])
        opts, unsupported = attributes.job_to_filter_options(
            {"print-color-mode": (codec.TAG_KEYWORD, ["monochrome"])}, caps)
        self.assertEqual((opts, unsupported), ([], []))    # print-color-mode passes through to cupsd

    def test_mono_device_asked_for_colour_is_reported(self):
        caps = Capabilities({"nickname": "x", "options": {
            "PageSize": {"default": "A4", "choices": {"A4": "A4"}}}})
        self.assertEqual(caps.color_modes(), ["monochrome"])
        _, unsupported = attributes.job_to_filter_options(
            {"print-color-mode": (codec.TAG_KEYWORD, ["color"])}, caps)
        self.assertEqual(unsupported, ["print-color-mode"])
