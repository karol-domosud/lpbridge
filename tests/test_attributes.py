#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""PPD <-> IPP attribute mapping, both directions.

"Catches the most common class of bug: an option that appears in the Print
dialog but never reaches the filter".
"""

import unittest

from agent.ipp import attributes, codec
from agent.ipp.attributes import Capabilities

# A trimmed copy of the real ``ppdcaps.awk`` output for the ML-1670 PPD.
CAPS = Capabilities({
    "ppd": "/usr/share/ppd/lpbridge/model.ppd",
    "nickname": "Samsung ML-1670 Foomatic/foo2qpdl (recommended)",
    "options": {
        "PageSize": {"default": "Letter", "choices": {
            "Letter": "Letter", "A4": "A4", "A5": "A5", "Legal": "Legal",
            "Executive": "Executive"}},
        "Resolution": {"default": "1200x600dpi", "choices": {
            "600x600dpi": "600x600 dpi", "1200x600dpi": "1200x600 dpi",
            "1200x1200dpi": "1200x1200 dpi"}},
        "InputSlot": {"default": "Auto", "choices": {
            "Auto": "Auto Select", "Tray1": "Tray 1", "Tray2": "Tray 2",
            "Manual": "Manual"}},
        "MediaType": {"default": "plain", "choices": {
            "plain": "Plain Paper", "thick": "Thick Stock",
            "transparency": "Transparency", "envelope": "Envelope"}},
    },
})

# A colour duplex printer, to exercise the branches the ML-1670 lacks.
CAPS_COLOR = Capabilities({
    "ppd": "/x.ppd", "nickname": "Test Color",
    "options": {
        "PageSize": {"default": "A4", "choices": {"A4": "A4", "Letter": "US"}},
        "Duplex": {"default": "None", "choices": {
            "None": "Off", "DuplexNoTumble": "Long Edge",
            "DuplexTumble": "Short Edge"}},
        "ColorModel": {"default": "RGB", "choices": {
            "RGB": "Color", "Gray": "Grayscale"}},
        "Resolution": {"default": "600dpi", "choices": {"600dpi": "600"}},
    },
})

PROFILE = {"name": "Samsung ML-1670", "uuid": "urn:uuid:x", "location": "",
           "manufacturer": "Samsung"}


class Forward(unittest.TestCase):
    def setUp(self):
        self.attrs = dict(
            (n, (t, v)) for n, t, v in attributes.printer_description(
                PROFILE, CAPS, "ipp://h/ipp/print", 0, 0))

    def test_media_supported_is_pwg_keywords(self):
        media = self.attrs["media-supported"][1]
        self.assertIn("iso_a4_210x297mm", media)
        self.assertIn("na_letter_8.5x11in", media)
        self.assertIn("na_legal_8.5x14in", media)

    def test_mono_only_printer_reports_monochrome(self):
        self.assertEqual(self.attrs["print-color-mode-supported"][1],
                         ["monochrome"])
        self.assertEqual(self.attrs["color-supported"][1], [False])

    def test_no_duplex_option_means_one_sided_only(self):
        self.assertEqual(self.attrs["sides-supported"][1], ["one-sided"])

    def test_resolutions_are_parsed(self):
        res = self.attrs["printer-resolution-supported"][1]
        self.assertIn((600, 600, 3), res)
        self.assertIn((1200, 600, 3), res)
        self.assertEqual(self.attrs["printer-resolution-default"][1],
                         [(1200, 600, 3)])

    def test_color_printer_exposes_sides_and_modes(self):
        a = dict((n, (t, v)) for n, t, v in attributes.printer_description(
            PROFILE, CAPS_COLOR, "ipp://h/ipp/print", 0, 0))
        self.assertEqual(set(a["print-color-mode-supported"][1]),
                         {"color", "monochrome", "auto"})
        self.assertIn("two-sided-long-edge", a["sides-supported"][1])


class Reverse(unittest.TestCase):
    def _job(self, **kw):
        out = {}
        for name, value in kw.items():
            name = name.replace("_", "-")
            if name == "printer-resolution":
                out[name] = (codec.TAG_RESOLUTION, [value])
            elif name == "copies" or name == "number-up":
                out[name] = (codec.TAG_INTEGER, [value])
            else:
                out[name] = (codec.TAG_KEYWORD, [value])
        return out

    def test_media_maps_to_ppd_pagesize(self):
        opts, unsup = attributes.job_to_filter_options(
            self._job(media="iso_a4_210x297mm"), CAPS)
        self.assertIn("PageSize=A4", opts)
        self.assertEqual(unsup, [])

    def test_resolution_matches_ppd_choice(self):
        opts, _ = attributes.job_to_filter_options(
            self._job(printer_resolution=(600, 600, 3)), CAPS)
        self.assertIn("Resolution=600x600dpi", opts)

    def test_duplex_request_unsupported_on_simplex_printer(self):
        opts, unsup = attributes.job_to_filter_options(
            self._job(sides="two-sided-long-edge"), CAPS)
        self.assertIn("sides", unsup)
        self.assertFalse([o for o in opts if o.startswith("Duplex=")])

    def test_duplex_maps_on_capable_printer(self):
        opts, unsup = attributes.job_to_filter_options(
            self._job(sides="two-sided-short-edge"), CAPS_COLOR)
        self.assertIn("Duplex=DuplexTumble", opts)
        self.assertEqual(unsup, [])

    def test_color_mode_picks_gray_choice(self):
        opts, _ = attributes.job_to_filter_options(
            self._job(print_color_mode="monochrome"), CAPS_COLOR)
        self.assertIn("ColorModel=Gray", opts)

    def test_copies_and_number_up_pass_through(self):
        opts, _ = attributes.job_to_filter_options(
            self._job(copies=3, number_up=2), CAPS)
        self.assertIn("copies=3", opts)
        self.assertIn("number-up=2", opts)

    def test_every_emitted_pagesize_round_trips(self):
        """PPD -> IPP -> -o flag must land back on a real PPD choice."""
        pd = dict((n, (t, v)) for n, t, v in attributes.printer_description(
            PROFILE, CAPS, "ipp://h/ipp/print", 0, 0))
        for pwg in pd["media-supported"][1]:
            opts, unsup = attributes.job_to_filter_options(
                self._job(media=pwg), CAPS)
            picked = [o.split("=", 1)[1] for o in opts if o.startswith("PageSize=")]
            self.assertTrue(picked, "no PageSize for %s" % pwg)
            self.assertIn(picked[0], CAPS.choices("PageSize"))


if __name__ == "__main__":
    unittest.main()
