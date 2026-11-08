#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""PPD <-> IPP attribute mapping.

Two directions that must stay symmetric:

* forward  -- ``ppd.load`` reads the queue's PPD into option groups, paper
  dimensions and the colour flag; :func:`printer_description` turns that
  into the IPP attributes a driverless macOS client needs to build a queue
  with the printer's real sizes, trays, colour modes and resolutions.
  Sizes come from ``*PaperDimension``, so an A3 press or a 58 mm receipt
  printer is offered exactly what its PPD lists; colour and resolution are
  found under vendor option names too (Brother ``BRMonoColor``, HP
  ``HPColorMode`` ...), by looking at the choices rather than the name.

* reverse  -- :func:`job_to_filter_options` maps the IPP job attributes macOS
  sends back to ``cupsfilter -o name=value`` flags for that same PPD.

``tests/test_attributes.py`` round-trips the pair.
"""

import time

from . import codec
from .codec import (
    TAG_BEG_COLLECTION, TAG_BOOLEAN, TAG_CHARSET, TAG_DATETIME, TAG_ENUM,
    TAG_INTEGER, TAG_KEYWORD, TAG_LANGUAGE, TAG_MIME, TAG_NAME, TAG_RANGE,
    TAG_RESOLUTION, TAG_TEXT, TAG_URI,
)

# ---------------------------------------------------------------------------
# PWG media keyword <-> PPD PageSize choice
# ---------------------------------------------------------------------------
# hundredths of a millimetre, PWG 5101.1 self-describing names. Used to give
# a PPD size its standard keyword when the dimensions match (so macOS shows
# "A3" rather than "297 by 420 mm"); anything else gets an ``oe_`` name the
# way cupsd's own PPD cache does.
MEDIA_SIZES = {
    "iso_a0_841x1189mm": (84100, 118900), "iso_a1_594x841mm": (59400, 84100),
    "iso_a2_420x594mm": (42000, 59400), "iso_a3_297x420mm": (29700, 42000),
    "iso_a4_210x297mm": (21000, 29700), "iso_a5_148x210mm": (14800, 21000),
    "iso_a6_105x148mm": (10500, 14800), "iso_a7_74x105mm": (7400, 10500),
    "iso_b4_250x353mm": (25000, 35300), "iso_b5_176x250mm": (17600, 25000),
    "iso_b6_125x176mm": (12500, 17600),
    "iso_c5_162x229mm": (16200, 22900), "iso_c6_114x162mm": (11400, 16200),
    "iso_dl_110x220mm": (11000, 22000),
    "jis_b4_257x364mm": (25700, 36400), "jis_b5_182x257mm": (18200, 25700),
    "jis_b6_128x182mm": (12800, 18200),
    "jpn_hagaki_100x148mm": (10000, 14800), "jpn_oufuku_148x200mm": (14800, 20000),
    "jpn_chou3_120x235mm": (12000, 23500), "jpn_you4_105x235mm": (10500, 23500),
    "na_letter_8.5x11in": (21590, 27940), "na_legal_8.5x14in": (21590, 35560),
    "na_ledger_11x17in": (27940, 43180), "na_executive_7.25x10.5in": (18415, 26670),
    "na_invoice_5.5x8.5in": (13970, 21590), "na_foolscap_8.5x13in": (21590, 33020),
    "na_index-3x5_3x5in": (7620, 12700), "na_index-4x6_4x6in": (10160, 15240),
    "na_5x7_5x7in": (12700, 17780), "na_govt-letter_8x10in": (20320, 25400),
    "na_arch-a_9x12in": (22860, 30480), "na_arch-b_12x18in": (30480, 45720),
    "na_c_17x22in": (43180, 55880), "na_d_22x34in": (55880, 86360),
    "na_number-10_4.125x9.5in": (10478, 24130), "na_monarch_3.875x7.5in": (9843, 19050),
    "na_number-9_3.875x8.875in": (9843, 22543),
    "prc_16k_146x215mm": (14600, 21500), "roc_16k_7.75x10.75in": (19685, 27305),
    "om_folio_210x330mm": (21000, 33000), "om_folio-sp_215x315mm": (21500, 31500),
    "oe_photo-l_3.5x5in": (8890, 12700),
}
SIZE_TO_MEDIA = {v: k for k, v in MEDIA_SIZES.items()}

# PPD *PageSize choice name -> PWG keyword, for capability dumps that carry
# no *PaperDimension (older caches, hand-written test fixtures)
PPD_PAGESIZE_TO_PWG = {
    "Letter": "na_letter_8.5x11in", "Legal": "na_legal_8.5x14in",
    "Executive": "na_executive_7.25x10.5in", "Tabloid": "na_ledger_11x17in",
    "Ledger": "na_ledger_11x17in", "Statement": "na_invoice_5.5x8.5in",
    "A3": "iso_a3_297x420mm", "A4": "iso_a4_210x297mm", "A5": "iso_a5_148x210mm",
    "A6": "iso_a6_105x148mm", "B5": "jis_b5_182x257mm", "ISOB5": "iso_b5_176x250mm",
    "JISB5": "jis_b5_182x257mm", "EnvDL": "iso_dl_110x220mm", "EnvC5": "iso_c5_162x229mm",
    "Env10": "na_number-10_4.125x9.5in", "EnvMonarch": "na_monarch_3.875x7.5in",
    "Postcard": "jpn_hagaki_100x148mm",
}
PWG_TO_PPD_PAGESIZE = {}
for _ppd, _pwg in PPD_PAGESIZE_TO_PWG.items():
    PWG_TO_PPD_PAGESIZE.setdefault(_pwg, _ppd)


def points_to_hmm(points):
    """PostScript points -> hundredths of a millimetre, as IPP wants."""
    return int(round(points * 2540.0 / 72.0))


def pwg_size_name(width_hmm, height_hmm, ppd_choice=None):
    """The PWG keyword for a size: a standard one within a millimetre, else
    ``oe_<choice>_<w>x<h>mm`` as cupsd's PPD cache names it."""
    for name, (w, h) in MEDIA_SIZES.items():
        if abs(w - width_hmm) <= 100 and abs(h - height_hmm) <= 100:
            return name
    if width_hmm % 2540 == 0 and height_hmm % 2540 == 0:
        dims = "%dx%din" % (width_hmm // 2540, height_hmm // 2540)
    else:
        dims = "%sx%smm" % (("%.2f" % (width_hmm / 100.0)).rstrip("0").rstrip("."),
                            ("%.2f" % (height_hmm / 100.0)).rstrip("0").rstrip("."))
    slug = "".join(c if c.isalnum() or c in "-." else "-" for c in (ppd_choice or "custom").lower()).strip("-")
    return "oe_%s_%s" % (slug or "custom", dims)


def parse_pwg_size(keyword):
    """``iso_a4_210x297mm`` / ``oe_x_195x270mm`` / ``custom_8.5x14in`` -> (w, h) in hundredths mm, or None."""
    if keyword in MEDIA_SIZES:
        return MEDIA_SIZES[keyword]
    tail = keyword.rsplit("_", 1)[-1]
    for unit, factor in (("mm", 100.0), ("in", 2540.0)):
        if tail.endswith(unit) and "x" in tail:
            a, b = tail[:-len(unit)].split("x", 1)
            try:
                return (int(round(float(a) * factor)), int(round(float(b) * factor)))
            except ValueError:
                return None
    return None


# PPD *InputSlot / *MediaType choice  ->  PWG keyword (best effort, lower-cased)
INPUTSLOT_TO_PWG = {
    "Auto": "auto", "AutoSelect": "auto", "Default": "auto",
    "Tray1": "tray-1", "Tray2": "tray-2", "Tray3": "tray-3",
    "Upper": "tray-1", "Lower": "tray-2", "MP": "by-pass-tray",
    "Manual": "manual", "ManualFeed": "manual", "Multipurpose": "by-pass-tray",
}
MEDIATYPE_TO_PWG = {
    "Plain": "stationery", "PLAIN": "stationery", "Normal": "stationery",
    "Auto": "auto", "Thick": "cardstock", "Thin": "stationery-lightweight",
    "Transparency": "transparency", "Labels": "labels", "Envelope": "envelope",
    "Photo": "photographic", "Bond": "stationery", "Recycled": "stationery",
}

ORIENTATION_NAMES = {3: "portrait", 4: "landscape",
                     5: "reverse-landscape", 6: "reverse-portrait"}
QUALITY_NAMES = {3: "draft", 4: "normal", 5: "high"}

# IPP sides keyword  <->  PPD *Duplex choice
SIDES_TO_DUPLEX = {
    "one-sided": "None",
    "two-sided-long-edge": "DuplexNoTumble",
    "two-sided-short-edge": "DuplexTumble",
}
DUPLEX_TO_SIDES = {v: k for k, v in SIDES_TO_DUPLEX.items()}
DUPLEX_TO_SIDES["Off"] = "one-sided"

# IPP print-color-mode  ->  candidate PPD *ColorModel choices, best first
COLORMODE_TO_COLORMODEL = {
    "monochrome": ("Gray", "Grayscale", "Mono", "Black", "KGray"),
    "color": ("RGB", "CMYK", "Color", "AutoColor"),
    "auto": ("Auto", "AutoColor"),
}

# Vendor PPDs keep the standard option behind their own names; these are the
# roles the agent needs and the standard name for each. A vendor option is
# recognised by its name ending in the standard one (``BRInputSlot``,
# ``HPMediaType``) or, for colour and resolution, by what its choices are.
ROLE_OPTIONS = {
    "PageSize": ("PageSize",),
    "Duplex": ("Duplex",),
    "InputSlot": ("InputSlot", "MediaSource", "PaperSource"),
    "MediaType": ("MediaType",),
    "OutputBin": ("OutputBin",),
    "Resolution": ("Resolution",),
    "ColorModel": ("ColorModel", "ColorMode", "MonoColor", "ColorType", "OutputMode"),
}
_MONO_WORDS = ("gray", "grey", "mono", "black", "bw", "b&w")
_COLOR_WORDS = ("color", "colour", "rgb", "cmyk", "full")


def classify_color_choice(choice, label=""):
    """'Mono' -> 'monochrome', 'FullColor' -> 'color', 'Auto' -> 'auto', else None."""
    text = ("%s %s" % (choice, label)).lower()
    if "auto" in text and not any(w in text for w in _MONO_WORDS):
        return "auto"
    if any(w in text for w in _MONO_WORDS):
        return "monochrome"
    if any(w in text for w in _COLOR_WORDS):
        return "color"
    return None


def _pwg_to_dpi_tuple(text):
    """'600dpi' or '600x600dpi' -> (600, 600, 3)."""
    text = text.strip().lower()
    units = 3 if text.endswith("dpi") else 4
    text = text.rstrip("dpi").rstrip("dpc").rstrip("m")
    if "x" in text:
        a, b = text.split("x", 1)
    else:
        a = b = text
    try:
        return (int(a), int(b), units)
    except ValueError:
        return (300, 300, 3)


def _dpi_tuple_to_ppd(value, choices):
    """(600,600,3) -> the PPD *Resolution choice whose leading number matches."""
    want = "%dx%ddpi" % (value[0], value[1])
    exact = (want, "%ddpi" % value[0]) if value[0] == value[1] else (want,)
    for choice in choices:
        if choice.lower() in exact:
            return choice
    if value[0] != value[1]:
        return None            # 600x2400 asked for: "600dpi" is not it
    for choice in choices:
        digits = "".join(c for c in choice if c.isdigit())
        if digits[:len(str(value[0]))] == str(value[0]):
            return choice
    return None


# ---------------------------------------------------------------------------
# Capabilities: the JSON the container dumps from the PPD
# ---------------------------------------------------------------------------
class Capabilities(object):
    """What the PPD says the printer can do, as ``ppd.load`` reports it.

    ``data`` looks like::

        {"ppd": "...", "nickname": "...", "color_device": True,
         "options": {"PageSize": {"default": "Letter",
                                  "choices": {"Letter": "US Letter", ...}},
                     ...},
         "paper": {"Letter": {"width": 612, "height": 792,
                              "imageable": [12, 12, 600, 780]}, ...},
         "custom_page_size": {"min": [w, h], "max": [w, h]} | None}

    Older dumps (the awk one) lack ``paper`` and ``color_device``; the
    fixed tables above cover those.
    """

    def __init__(self, data):
        self.data = data or {}
        self.options = self.data.get("options", {})
        self.paper = self.data.get("paper") or {}
        self.custom_page_size = self.data.get("custom_page_size")
        self.ppd = self.data.get("ppd")
        self.nickname = self.data.get("nickname") or "Generic Printer"
        self.color_device = bool(self.data.get("color_device"))
        self._roles = {}

    def choices(self, option):
        entry = self.options.get(option) or {}
        return list((entry.get("choices") or {}).keys())

    def labels(self, option):
        entry = self.options.get(option) or {}
        return dict(entry.get("choices") or {})

    def default(self, option):
        entry = self.options.get(option) or {}
        return entry.get("default")

    def has(self, option):
        return bool(self.choices(option))

    # -- which PPD option plays which role --------------------------------
    def option_for(self, role):
        """The PPD option name filling ``role`` (``"ColorModel"`` ...), or None."""
        if role in self._roles:
            return self._roles[role]
        found = None
        for name in ROLE_OPTIONS.get(role, (role,)):
            if self.has(name):
                found = name
                break
        if not found:
            suffixes = tuple(x.lower() for x in ROLE_OPTIONS.get(role, (role,)))
            for name in self.options:
                if name.lower().endswith(suffixes) and self.has(name):
                    found = name
                    break
        if not found and role == "ColorModel":
            found = self._color_option_by_choices()
        if not found and role == "Resolution":
            found = self._resolution_option_by_choices()
        self._roles[role] = found
        return found

    def _color_option_by_choices(self):
        for name in self.options:
            kinds = {classify_color_choice(c, l) for c, l in self.labels(name).items()}
            if "monochrome" in kinds and "color" in kinds:
                return name
        return None

    def _resolution_option_by_choices(self):
        for name in self.options:
            choices = self.choices(name)
            if choices and all(_looks_like_dpi(c) for c in choices):
                return name
        return None

    def color_choice(self, mode):
        """The choice of the colour option meaning IPP ``mode``, or None."""
        option = self.option_for("ColorModel")
        if not option:
            return None
        if option == "ColorModel":
            for candidate in COLORMODE_TO_COLORMODEL.get(mode, ()):
                if candidate in self.choices(option):
                    return candidate
        for choice, label in self.labels(option).items():
            if classify_color_choice(choice, label) == mode:
                return choice
        return None

    # -- media ---------------------------------------------------------
    def _size_hmm(self, choice):
        entry = self.paper.get(choice)
        if entry and entry.get("width") and entry.get("height"):
            return (points_to_hmm(entry["width"]), points_to_hmm(entry["height"]))
        pwg = PPD_PAGESIZE_TO_PWG.get(choice)
        return MEDIA_SIZES.get(pwg) if pwg else None

    def media_keyword(self, choice):
        """The PWG keyword lpbridge advertises for a PageSize choice, or None."""
        dims = self._size_hmm(choice)
        if dims:
            return pwg_size_name(dims[0], dims[1], choice)
        return PPD_PAGESIZE_TO_PWG.get(choice)

    def media_supported(self):
        out = []
        for choice in self.choices("PageSize"):
            if choice.lower().startswith("custom"):
                continue
            pwg = self.media_keyword(choice)
            if pwg and pwg not in out:
                out.append(pwg)
        return out or ["na_letter_8.5x11in", "iso_a4_210x297mm"]

    def media_default(self):
        default = self.default("PageSize")
        pwg = self.media_keyword(default) if default else None
        supported = self.media_supported()
        return pwg if pwg in supported else supported[0]

    def media_margins(self, pwg):
        """(left, bottom, right, top) in hundredths mm from *ImageableArea; zeros if unknown."""
        choice = self.pagesize_for_media(pwg)
        entry = self.paper.get(choice) if choice else None
        if not entry or not entry.get("imageable"):
            return (0, 0, 0, 0)
        llx, lly, urx, ury = entry["imageable"]
        return tuple(max(0, points_to_hmm(v)) for v in
                     (llx, lly, entry["width"] - urx, entry["height"] - ury))

    def pagesize_for_media(self, pwg, dims=None):
        """The PPD PageSize choice for a PWG keyword or (w, h) in hundredths mm.

        Matches by keyword first, then by dimensions within a millimetre;
        a size the PPD does not list but its custom range allows becomes
        ``Custom.WxH`` (points), which cupsd understands. None otherwise.
        """
        for choice in self.choices("PageSize"):
            if self.media_keyword(choice) == pwg:
                return choice
        dims = dims or (parse_pwg_size(pwg) if pwg else None)
        if not dims:
            return None
        for choice in self.choices("PageSize"):
            size = self._size_hmm(choice)
            if size and abs(size[0] - dims[0]) <= 100 and abs(size[1] - dims[1]) <= 100:
                return choice
        if self.custom_page_size:
            w_pt, h_pt = dims[0] * 72.0 / 2540.0, dims[1] * 72.0 / 2540.0
            (min_w, min_h), (max_w, max_h) = self.custom_page_size["min"], self.custom_page_size["max"]
            if min_w <= w_pt <= max_w and min_h <= h_pt <= max_h:
                return "Custom.%dx%d" % (round(w_pt), round(h_pt))
        return None

    def custom_size_range(self):
        """((min_w, min_h), (max_w, max_h)) in hundredths mm, or None."""
        c = self.custom_page_size
        if not c:
            return None
        return ((points_to_hmm(c["min"][0]), points_to_hmm(c["min"][1])),
                (points_to_hmm(c["max"][0]), points_to_hmm(c["max"][1])))

    # -- other derived IPP vocabularies --------------------------------
    def sides_supported(self):
        option = self.option_for("Duplex")
        if not option:
            return ["one-sided"]
        out = ["one-sided"]
        for choice in self.choices(option):
            side = DUPLEX_TO_SIDES.get(choice)
            if side and side not in out:
                out.append(side)
        return out

    def color_modes(self):
        option = self.option_for("ColorModel")
        modes = set()
        if option:
            for choice, label in self.labels(option).items():
                kind = classify_color_choice(choice, label)
                if kind:
                    modes.add(kind)
                elif option == "ColorModel":
                    modes.add("color")     # RGB, CMYK, ...: anything not grey is colour
        if self.color_device:
            # *ColorDevice: True is the PPD's word that this prints colour,
            # whatever its option set looks like
            modes.update({"color", "monochrome"})
        if not modes:
            return ["monochrome"]
        if "color" in modes:
            modes.add("auto")
        order = ["color", "monochrome", "auto"]
        return [m for m in order if m in modes]

    def resolutions(self):
        option = self.option_for("Resolution")
        out = []
        for choice in (self.choices(option) if option else ()):
            out.append(_pwg_to_dpi_tuple(choice))
        return out or [(300, 300, 3)]

    def resolution_default(self):
        option = self.option_for("Resolution")
        d = self.default(option) if option else None
        return _pwg_to_dpi_tuple(d) if d else self.resolutions()[0]

    def media_sources(self):
        option = self.option_for("InputSlot")
        out = []
        for choice in (self.choices(option) if option else ()):
            out.append(INPUTSLOT_TO_PWG.get(choice, choice.lower()))
        return out or ["auto"]

    def media_types(self):
        option = self.option_for("MediaType")
        out = []
        for choice in (self.choices(option) if option else ()):
            out.append(MEDIATYPE_TO_PWG.get(choice, choice.lower()))
        return out or ["stationery"]

    def output_bins(self):
        option = self.option_for("OutputBin")
        out = [b.lower() for b in (self.choices(option) if option else ())]
        return out or ["face-down"]


def _looks_like_dpi(choice):
    low = choice.lower()
    if not low.endswith("dpi"):
        return False
    body = low[:-3]
    return all(part.isdigit() for part in body.split("x")) and body != ""


# ---------------------------------------------------------------------------
# forward: capabilities -> IPP printer description
# ---------------------------------------------------------------------------
def _media_col(pwg_keyword, source="auto", media_type="stationery", caps=None):
    width, height = parse_pwg_size(pwg_keyword) or MEDIA_SIZES["na_letter_8.5x11in"]
    left, bottom, right, top = caps.media_margins(pwg_keyword) if caps else (0, 0, 0, 0)
    return {
        "media-size": (TAG_BEG_COLLECTION, [{
            "x-dimension": (TAG_INTEGER, [width]),
            "y-dimension": (TAG_INTEGER, [height]),
        }]),
        "media-bottom-margin": (TAG_INTEGER, [bottom]),
        "media-left-margin": (TAG_INTEGER, [left]),
        "media-right-margin": (TAG_INTEGER, [right]),
        "media-top-margin": (TAG_INTEGER, [top]),
        "media-source": (TAG_KEYWORD, [source]),
        "media-type": (TAG_KEYWORD, [media_type]),
    }


def _media_size_supported(caps, media):
    sizes = []
    for pwg in media:
        dims = parse_pwg_size(pwg)
        if dims:
            sizes.append({"x-dimension": (TAG_INTEGER, [dims[0]]),
                          "y-dimension": (TAG_INTEGER, [dims[1]])})
    custom = caps.custom_size_range()
    if custom:
        (min_w, min_h), (max_w, max_h) = custom
        sizes.append({"x-dimension": (TAG_RANGE, [(min_w, max_w)]),
                      "y-dimension": (TAG_RANGE, [(min_h, max_h)])})
    return sizes


def printer_description(profile, caps, printer_uri, start_time, queued,
                        state=3, state_reasons=("none",),
                        accepting=True, state_message=None):
    """Everything a driverless client needs to build a queue with no PPD."""
    uptime = max(1, int(time.time() - start_time))
    name = profile.get("name") or "lpbridge printer"
    media = caps.media_supported()
    sides = caps.sides_supported()
    colors = caps.color_modes()
    resolutions = caps.resolutions()
    sources = caps.media_sources()
    types = caps.media_types()
    bins = caps.output_bins()

    attrs = [
        ("printer-uri-supported", TAG_URI, [printer_uri]),
        ("uri-security-supported", TAG_KEYWORD, ["none"]),
        ("uri-authentication-supported", TAG_KEYWORD, ["requesting-user-name"]),
        ("printer-name", TAG_NAME, [name]),
        ("printer-info", TAG_TEXT, [name]),
        ("printer-dns-sd-name", TAG_NAME, [name]),
        ("printer-location", TAG_TEXT, [profile.get("location") or ""]),
        ("printer-make-and-model", TAG_TEXT, [caps.nickname]),
        ("printer-device-id", TAG_TEXT, [
            "MFG:%s;MDL:%s;CMD:PDF;CLS:PRINTER;"
            % (profile.get("manufacturer") or "lpbridge", name)]),
        ("printer-uuid", TAG_URI, [profile["uuid"]]),
        ("printer-state", TAG_ENUM, [state]),
        ("printer-state-reasons", TAG_KEYWORD, list(state_reasons)),
        ("printer-state-message", TAG_TEXT, [
            state_message or ("Ready" if state == 3
                              else "Processing" if state == 4 else "Stopped")]),
        ("printer-is-accepting-jobs", TAG_BOOLEAN, [bool(accepting)]),
        ("printer-up-time", TAG_INTEGER, [uptime]),
        ("printer-current-time", TAG_DATETIME, [codec.ipp_datetime()]),
        ("queued-job-count", TAG_INTEGER, [queued]),
        ("charset-configured", TAG_CHARSET, ["utf-8"]),
        ("charset-supported", TAG_CHARSET, ["utf-8"]),
        ("natural-language-configured", TAG_LANGUAGE, ["en"]),
        ("generated-natural-language-supported", TAG_LANGUAGE, ["en"]),
        ("ipp-versions-supported", TAG_KEYWORD, ["1.0", "1.1", "2.0"]),
        ("ipp-features-supported", TAG_KEYWORD, ["ipp-everywhere"]),
        ("operations-supported", TAG_ENUM, [
            codec.OP_PRINT_JOB, codec.OP_VALIDATE_JOB, codec.OP_CREATE_JOB,
            codec.OP_SEND_DOCUMENT, codec.OP_CANCEL_JOB,
            codec.OP_GET_JOB_ATTRIBUTES, codec.OP_GET_JOBS,
            codec.OP_GET_PRINTER_ATTRIBUTES, codec.OP_CLOSE_JOB,
            codec.OP_IDENTIFY_PRINTER]),
        ("multiple-document-jobs-supported", TAG_BOOLEAN, [True]),
        ("multiple-operation-time-out", TAG_INTEGER, [120]),
        ("multiple-operation-time-out-action", TAG_KEYWORD, ["process-job"]),
        ("pdl-override-supported", TAG_KEYWORD, ["attempted"]),
        ("compression-supported", TAG_KEYWORD, ["none", "gzip"]),
        ("document-format-supported", TAG_MIME,
         ["application/pdf", "application/octet-stream"]),
        ("document-format-default", TAG_MIME, ["application/pdf"]),
        ("document-format-preferred", TAG_MIME, ["application/pdf"]),
        ("color-supported", TAG_BOOLEAN, ["monochrome" not in colors
                                          or "color" in colors]),
        ("print-color-mode-supported", TAG_KEYWORD, colors),
        ("print-color-mode-default", TAG_KEYWORD, [colors[0]]),
        ("sides-supported", TAG_KEYWORD, sides),
        ("sides-default", TAG_KEYWORD, [
            DUPLEX_TO_SIDES.get(caps.default(caps.option_for("Duplex") or "Duplex"), "one-sided")]),
        ("media-supported", TAG_KEYWORD, media),
        ("media-default", TAG_KEYWORD, [caps.media_default()]),
        ("media-ready", TAG_KEYWORD, [caps.media_default()]),
        ("media-type-supported", TAG_KEYWORD, types),
        ("media-source-supported", TAG_KEYWORD, sources),
        ("media-source-default", TAG_KEYWORD, [
            INPUTSLOT_TO_PWG.get(caps.default(caps.option_for("InputSlot") or "InputSlot"), sources[0])]),
        ("media-type-default", TAG_KEYWORD, [
            MEDIATYPE_TO_PWG.get(caps.default(caps.option_for("MediaType") or "MediaType"), types[0])]),
        ("media-col-default", TAG_BEG_COLLECTION, [
            _media_col(caps.media_default(), sources[0], types[0], caps)]),
        ("media-col-database", TAG_BEG_COLLECTION, [
            _media_col(m, sources[0], types[0], caps) for m in media]),
        ("media-size-supported", TAG_BEG_COLLECTION, _media_size_supported(caps, media)),
        ("media-col-supported", TAG_KEYWORD, [
            "media-size", "media-top-margin", "media-bottom-margin",
            "media-left-margin", "media-right-margin", "media-source",
            "media-type"]),
        ("media-bottom-margin-supported", TAG_INTEGER, sorted({caps.media_margins(m)[1] for m in media})),
        ("media-left-margin-supported", TAG_INTEGER, sorted({caps.media_margins(m)[0] for m in media})),
        ("media-right-margin-supported", TAG_INTEGER, sorted({caps.media_margins(m)[2] for m in media})),
        ("media-top-margin-supported", TAG_INTEGER, sorted({caps.media_margins(m)[3] for m in media})),
        ("printer-resolution-supported", TAG_RESOLUTION, resolutions),
        ("printer-resolution-default", TAG_RESOLUTION, [
            caps.resolution_default()]),
        ("pwg-raster-document-resolution-supported", TAG_RESOLUTION, resolutions),
        ("print-quality-supported", TAG_ENUM, [3, 4, 5]),
        ("print-quality-default", TAG_ENUM, [4]),
        ("print-scaling-supported", TAG_KEYWORD, [
            "auto", "auto-fit", "fill", "fit", "none"]),
        ("print-scaling-default", TAG_KEYWORD, ["auto"]),
        ("copies-supported", TAG_RANGE, [(1, 999)]),
        ("copies-default", TAG_INTEGER, [1]),
        ("orientation-requested-supported", TAG_ENUM, [3, 4, 5, 6]),
        ("orientation-requested-default", TAG_ENUM, [3]),
        ("number-up-supported", TAG_INTEGER, [1, 2, 4, 6, 9, 16]),
        ("number-up-default", TAG_INTEGER, [1]),
        ("page-ranges-supported", TAG_BOOLEAN, [True]),
        ("finishings-supported", TAG_ENUM, [3]),
        ("finishings-default", TAG_ENUM, [3]),
        ("output-bin-supported", TAG_KEYWORD, bins),
        ("output-bin-default", TAG_KEYWORD, [
            (caps.default(caps.option_for("OutputBin") or "OutputBin") or bins[0]).lower()]),
        ("identify-actions-supported", TAG_KEYWORD, ["display"]),
        ("identify-actions-default", TAG_KEYWORD, ["display"]),
        ("which-jobs-supported", TAG_KEYWORD, [
            "completed", "not-completed", "all"]),
        ("job-creation-attributes-supported", TAG_KEYWORD, [
            "copies", "finishings", "media", "media-col", "number-up",
            "orientation-requested", "output-bin", "page-ranges",
            "print-color-mode", "print-quality", "print-scaling",
            "printer-resolution", "sides", "job-name",
            "multiple-document-handling", "requesting-user-name"]),
        ("printer-get-attributes-supported", TAG_KEYWORD, ["document-format"]),
        ("document-password-supported", TAG_INTEGER, [0]),
    ]
    return attrs


# ---------------------------------------------------------------------------
# reverse: IPP job attributes -> cupsfilter -o flags
# ---------------------------------------------------------------------------
def _describe_media(job):
    """Return (pwg_keyword, media_type, media_source) from media/media-col."""
    keyword = codec.first(job, "media")
    if keyword:
        return keyword, None, None
    col = codec.first(job, "media-col")
    if not isinstance(col, dict):
        return None, None, None
    size = codec.first(col, "media-size")
    name = None
    if isinstance(size, dict):
        dims = (codec.first(size, "x-dimension"), codec.first(size, "y-dimension"))
        if all(isinstance(d, int) for d in dims):
            name = SIZE_TO_MEDIA.get(dims) or pwg_size_name(dims[0], dims[1])
    return name, codec.first(col, "media-type"), codec.first(col, "media-source")


def _page_ranges(job):
    entry = job.get("page-ranges")
    if not entry:
        return None
    parts = []
    for value in entry[1]:
        if isinstance(value, (tuple, list)) and len(value) == 2:
            parts.append("%d-%d" % (value[0], value[1]) if value[0] != value[1]
                         else str(value[0]))
        else:
            parts.append(str(value))
    return ",".join(parts) or None


def _match_pwg_source(pwg, caps):
    option = caps.option_for("InputSlot")
    for choice in (caps.choices(option) if option else ()):
        if INPUTSLOT_TO_PWG.get(choice, choice.lower()) == pwg:
            return choice
    return None


def _match_pwg_type(pwg, caps):
    option = caps.option_for("MediaType")
    for choice in (caps.choices(option) if option else ()):
        if MEDIATYPE_TO_PWG.get(choice, choice.lower()) == pwg:
            return choice
    return None


def job_to_filter_options(job_attributes, caps):
    """Return ``(opts, unsupported)``.

    ``opts`` is a list of ``"Name=Value"`` strings to hand to
    ``cupsfilter -o``; ``unsupported`` lists IPP attribute names that the PPD
    cannot express (surfaced to the client as ``job-completed-with-errors``
    per the container contract, exit 12).
    """
    opts = []
    unsupported = []
    job = job_attributes

    # -- sides / duplex ------------------------------------------------
    sides = codec.first(job, "sides")
    duplex = caps.option_for("Duplex")
    if sides:
        ppd_choice = SIDES_TO_DUPLEX.get(sides)
        if duplex and ppd_choice and ppd_choice in caps.choices(duplex):
            opts.append("%s=%s" % (duplex, ppd_choice))
        elif duplex and ppd_choice == "None":
            opts.append("%s=None" % duplex)
        elif sides != "one-sided":
            unsupported.append("sides")

    # -- colour -------------------------------------------------------
    # The colour option may be *ColorModel or a vendor's own (BRMonoColor,
    # HPColorMode ...). A colour device with no such option still gets the
    # request as print-color-mode, which cups-filters honour on raster
    # paths; a mono device asked for colour simply prints mono.
    color_mode = codec.first(job, "print-color-mode")
    color_option = caps.option_for("ColorModel")
    if color_mode and color_option:
        picked = caps.color_choice(color_mode)
        if not picked and color_mode == "auto":
            picked = caps.color_choice("color")
        if picked:
            opts.append("%s=%s" % (color_option, picked))
        elif color_mode == "color" and "color" not in caps.color_modes():
            unsupported.append("print-color-mode")
    elif color_mode == "color" and "color" not in caps.color_modes():
        unsupported.append("print-color-mode")

    # -- media size -------------------------------------------------
    # Matched by keyword, then by dimensions, then the PPD's custom range;
    # a size the printer cannot take is reported and cupsd prints on its
    # default size rather than failing the job.
    pwg_media, mtype, msource = _describe_media(job)
    if pwg_media:
        ppd_size = caps.pagesize_for_media(pwg_media)
        if ppd_size:
            opts.append("PageSize=%s" % ppd_size)
        elif caps.has("PageSize"):
            unsupported.append("media")

    # -- media source / type --------------------------------------
    if msource:
        choice = _match_pwg_source(msource, caps)
        if choice:
            opts.append("%s=%s" % (caps.option_for("InputSlot"), choice))
        elif caps.option_for("InputSlot") and msource != "auto":
            unsupported.append("media-source")
    if mtype:
        choice = _match_pwg_type(mtype, caps)
        if choice:
            opts.append("%s=%s" % (caps.option_for("MediaType"), choice))
        elif caps.option_for("MediaType"):
            unsupported.append("media-type")

    # -- resolution ------------------------------------------------
    resolution = codec.first(job, "printer-resolution")
    res_option = caps.option_for("Resolution")
    if resolution and res_option:
        choice = _dpi_tuple_to_ppd(resolution, caps.choices(res_option))
        if choice:
            opts.append("%s=%s" % (res_option, choice))
        else:
            unsupported.append("printer-resolution")

    # -- attributes cupsfilter understands directly ----------------
    copies = codec.first(job, "copies")
    if copies and int(copies) != 1:
        opts.append("copies=%d" % int(copies))
    number_up = codec.first(job, "number-up")
    if number_up and int(number_up) != 1:
        opts.append("number-up=%d" % int(number_up))
    ranges = _page_ranges(job)
    if ranges:
        opts.append("page-ranges=%s" % ranges)
    orientation = codec.first(job, "orientation-requested")
    if orientation and orientation != 3:
        opts.append("orientation-requested=%d" % int(orientation))
    scaling = codec.first(job, "print-scaling")
    if scaling and scaling != "auto":
        opts.append("print-scaling=%s" % scaling)

    return opts, unsupported
