#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Dry-run transport: write the device blob to disk instead of a printer.

Used by the golden tests and by ``--transport dryrun`` for developing without
hardware. Writes, next to each other:

    <timestamp>_job<id>_<title>.prn     the device stream
    <timestamp>_job<id>_<title>.json    the job settings (schema shared with
                                        the CUPS-backend prototype)
"""

import json
import os
import re
import time

from .base import Transport, TransportError
from ..ipp import attributes as ipp_attributes
from ..ipp import codec
from ..ipp.attributes import ORIENTATION_NAMES, QUALITY_NAMES
from ..ipp.server import log

UNSAFE_NAME_RE = re.compile(r'[\x00-\x1f/\\:*?"<>|]+')


def _safe_name(title):
    title = UNSAFE_NAME_RE.sub("_", title or "").strip(" ._")
    return (title or "Untitled")[:80]


def _unique_base(directory, base):
    candidate, counter = base, 1
    while any(os.path.exists(os.path.join(directory, candidate + ext))
             for ext in (".prn", ".json", ".pdf")):
        candidate = "%s-%d" % (base, counter)
        counter += 1
    return candidate


def _record(job, prn_path, byte_count, device_uri):
    op = job["operation"]
    ja = job["attributes"]
    sides = codec.first(ja, "sides", "one-sided")
    resolution = codec.first(ja, "printer-resolution")
    quality = codec.first(ja, "print-quality")
    orientation = codec.first(ja, "orientation-requested")
    media_size, media_type, media_source = ipp_attributes._describe_media(ja)
    return {
        "job": {
            "id": job["id"],
            "uuid": job["uuid"],
            "title": codec.first(op, "job-name")
            or codec.first(ja, "job-name") or "Untitled",
            "user": codec.first(op, "requesting-user-name"),
            "device_uri": device_uri,
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
        "settings": {
            "duplex": sides,
            "duplex_is_two_sided": bool(sides and sides.startswith("two-sided")),
            "color_mode": codec.first(ja, "print-color-mode", "color"),
            "media_size": media_size,
            "media_type": media_type,
            "media_source": media_source,
            "copies": codec.first(ja, "copies", 1),
            "page_ranges": _fmt_ranges(ja),
            "pages_per_sheet": codec.first(ja, "number-up", 1),
            "resolution": codec.format_resolution(resolution) if resolution
            else None,
            "orientation": ORIENTATION_NAMES.get(orientation, orientation),
            "print_quality": QUALITY_NAMES.get(quality, quality),
            "print_scaling": codec.first(ja, "print-scaling"),
        },
        "document": {"path": prn_path, "bytes": byte_count},
        "raw": {
            "transport": "dryrun",
            "job_attributes": codec.json_safe(ja),
            "operation_attributes": codec.json_safe(op),
        },
    }


def _fmt_ranges(ja):
    entry = ja.get("page-ranges")
    if not entry:
        return None
    parts = []
    for value in entry[1]:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            parts.append("%d-%d" % (value[0], value[1]))
        else:
            parts.append(str(value))
    return ",".join(parts) or None


class DryRunTransport(Transport):
    kind = "dryrun"

    def __init__(self, output_dir):
        self.output_dir = os.path.abspath(os.path.expanduser(output_dir))

    def describe(self):
        return "dryrun -> %s" % self.output_dir

    def send(self, prn_path, job):
        if not os.path.exists(prn_path):
            raise TransportError("no .prn to deliver")
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            with open(prn_path, "rb") as handle:
                data = handle.read()
            title = codec.first(job["operation"], "job-name") or "Untitled"
            base = _unique_base(self.output_dir, "%s_job%d_%s" % (
                time.strftime("%Y%m%d-%H%M%S"), job["id"], _safe_name(title)))
            out = os.path.join(self.output_dir, base + ".prn")
            with open(out, "wb") as handle:
                handle.write(data)
            with open(os.path.join(self.output_dir, base + ".json"), "w",
                      encoding="utf-8") as handle:
                json.dump(_record(job, out, len(data), self.describe()),
                          handle, indent=2, ensure_ascii=False)
                handle.write("\n")
        except OSError as exc:
            raise TransportError("dryrun write failed: %s" % exc)
        log("dryrun: wrote %s (%d bytes)" % (os.path.basename(out), len(data)))
        return len(data)
