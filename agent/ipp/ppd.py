#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Read what the agent needs out of a PPD, on the Mac.

cupsd installed the PPD for the queue and the agent read it back
(``~/Library/Application Support/lpbridge/ppd/<id>.ppd``); this turns it
into the dict ``attributes.Capabilities`` works from::

    {"version": 2, "ppd": "...", "nickname": "...", "color_device": True,
     "options": {"PageSize": {"default": "Letter",
                              "choices": {"Letter": "Letter", ...}}, ...},
     "paper": {"Letter": {"width": 612.0, "height": 792.0,
                          "imageable": [12.0, 12.0, 600.0, 780.0]}, ...},
     "custom_page_size": {"min": [72.0, 72.0], "max": [842.0, 1440.0]}}

Every ``*OpenUI`` group is kept, not just the seven the IPP map names:
vendor PPDs put colour and resolution behind their own option names
(Brother ``BRMonoColor`` / ``BRResolution``, HP ``HPColorMode``, Canon
``CNColorMode`` ...) and ``Capabilities`` finds those by their choices.
Dimensions are PostScript points, as the PPD states them.

The engine's ``--dump-capabilities`` did this same job with awk inside the
guest; parsing a text file the Mac already has needs no VM.
"""

import re

VERSION = 2

_OPENUI = re.compile(r'^\*(?:JCL)?OpenUI\s+\*([A-Za-z0-9_]+)(?:/([^:]*))?:\s*(\S+)')
_CLOSEUI = re.compile(r'^\*(?:JCL)?CloseUI')
_DEFAULT = re.compile(r'^\*Default([A-Za-z0-9_]+):\s*(\S+)')
_CHOICE = re.compile(r'^\*([A-Za-z0-9_]+)\s+([^/:\s]+)(?:/([^:]*))?:')
_QUOTED = re.compile(r'"([^"]*)"')
_PAPER = re.compile(r'^\*(PaperDimension|ImageableArea)\s+([^/:\s]+)(?:/[^:]*)?:\s*"([^"]*)"')
_CUSTOM = re.compile(r'^\*ParamCustomPageSize\s+(Width|Height):\s*\S+\s+points\s+([0-9.]+)\s+([0-9.]+)')


def _floats(text):
    out = []
    for part in text.split():
        try:
            out.append(float(part))
        except ValueError:
            return None
    return out


def parse(text, path=None):
    """Parse PPD ``text``; never raises on a malformed line, just skips it."""
    options = {}
    defaults = {}
    paper = {}
    custom = {}
    nickname = model = None
    color_device = False
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("*") or line.startswith("*%"):
            continue
        if line.startswith("*NickName:"):
            m = _QUOTED.search(line)
            if m:
                nickname = m.group(1).strip()
            continue
        if line.startswith("*ModelName:"):
            m = _QUOTED.search(line)
            if m:
                model = m.group(1).strip()
            continue
        if line.startswith("*ColorDevice:"):
            color_device = line.split(":", 1)[1].strip().lower().startswith("true")
            continue
        m = _PAPER.match(line)
        if m:
            kind, choice, values = m.groups()
            dims = _floats(values)
            if kind == "PaperDimension" and dims and len(dims) == 2:
                paper.setdefault(choice, {})["width"], paper[choice]["height"] = dims
            elif kind == "ImageableArea" and dims and len(dims) == 4:
                paper.setdefault(choice, {})["imageable"] = dims
            continue
        m = _CUSTOM.match(line)
        if m:
            which, low, high = m.groups()
            custom[which] = (float(low), float(high))
            continue
        m = _OPENUI.match(line)
        if m:
            current = m.group(1)
            options.setdefault(current, {"default": None, "choices": {}, "label": (m.group(2) or current).strip()})
            continue
        if _CLOSEUI.match(line):
            current = None
            continue
        m = _DEFAULT.match(line)
        if m:
            defaults[m.group(1)] = m.group(2)
            continue
        if current:
            m = _CHOICE.match(line)
            if m and m.group(1) == current:
                choice, label = m.group(2), (m.group(3) or m.group(2)).strip()
                options[current]["choices"].setdefault(choice, label)

    for name, entry in options.items():
        default = defaults.get(name)
        if default in entry["choices"]:
            entry["default"] = default
        elif entry["choices"]:
            entry["default"] = next(iter(entry["choices"]))
    # PageSize choices that only appear as PaperDimension (some PPDs list
    # sizes without an *OpenUI block) are still real sizes
    if "PageSize" in options:
        for choice in paper:
            if "width" in paper[choice] and choice not in options["PageSize"]["choices"]:
                options["PageSize"]["choices"][choice] = choice
    paper = {c: p for c, p in paper.items() if "width" in p and "height" in p}

    custom_page_size = None
    if "Width" in custom and "Height" in custom and options.get("PageSize", {}).get("choices"):
        custom_page_size = {"min": [custom["Width"][0], custom["Height"][0]],
                            "max": [custom["Width"][1], custom["Height"][1]]}

    return {"version": VERSION, "ppd": path, "nickname": nickname or model or "Generic Printer",
            "color_device": color_device, "options": options, "paper": paper,
            "custom_page_size": custom_page_size}


def load(path):
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:2] == b"\x1f\x8b":
        import gzip
        data = gzip.decompress(data)
    return parse(data.decode("utf-8", "replace"), path=path)
