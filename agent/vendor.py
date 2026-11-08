#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Vendor driver packages: the ``.deb`` a Linux user would download by hand.

Invariant 7: never vendor these into the repo. ``agent/data/vendor-drivers.json``
is a catalog of *where* they are (URL + sha256 + which models) -- harvested
from the same places a Linux user looks (vendor download pages, the AUR
PKGBUILDs that wrap them). The agent downloads one on first use, verifies
it, and installs it into the engine with ``dpkg``; the engine resolves the
package's Ubuntu dependencies through its own network.

A package is either a ``.deb`` or an archive (``.tar.gz``) that contains
``.deb`` files per architecture (``members``): a native arm64 build is used
when the vendor ships one, else the x86-64 build runs under FEX (or
qemu-x86_64 where FEX cannot), else the 32-bit i386 build runs under
qemu-i386. Emulation is slower than native, but a page of raster is a
bounded job.

A profile can force an entry with ``driver.vendor: <id>`` or name its own
packages with ``driver.packages: [{url, sha256}]``.
"""

import hashlib
import json
import os
import fnmatch
import re
import tarfile
import tempfile
import urllib.parse
import urllib.request

from .ipp.server import log

SUPPORT = os.path.expanduser("~/Library/Application Support/lpbridge")
CATALOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "vendor-drivers.json")
MAX_BYTES = 256 * 1024 * 1024


class VendorError(Exception):
    pass


def catalog(path=None):
    try:
        with open(path or CATALOG) as handle:
            return json.load(handle).get("drivers", [])
    except (OSError, ValueError) as exc:
        log("vendor catalog unreadable: %s" % exc)
        return []


def match(device, profile=None, entries=None):
    """The catalog entry for a printer, or None."""
    entries = catalog() if entries is None else entries
    driver = (profile or {}).get("driver", {})
    if driver.get("packages"):
        return {"id": "profile:%s" % profile["id"], "packages": driver["packages"],
                "arch": driver.get("arch", "amd64"), "prefer": True}
    if driver.get("vendor"):
        for entry in entries:
            if entry.get("id") == driver["vendor"]:
                return entry
        raise VendorError("profile names unknown vendor driver '%s'" % driver["vendor"])
    make = (device.get("make") or "").lower()
    model = device.get("model") or ""
    for entry in entries:
        rule = entry.get("match") or {}
        if rule.get("make") and not re.search(rule["make"], make, re.I):
            continue
        if rule.get("model") and not re.search(rule["model"], model, re.I):
            continue
        if rule.get("device_id") and not re.search(rule["device_id"], device.get("device_id") or "", re.I):
            continue
        return entry
    return None


def _download(url, dest, sha256):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise VendorError("vendor downloads must use https: %s" % url)
    log("fetching vendor driver: %s" % url)
    request = urllib.request.Request(url, headers={"User-Agent": "lpbridge"})
    digest = hashlib.sha256()
    size = 0
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    temporary = None
    try:
        with urllib.request.urlopen(request, timeout=60) as response, \
                tempfile.NamedTemporaryFile(dir=os.path.dirname(dest), delete=False) as output:
            temporary = output.name
            while True:
                block = response.read(1 << 16)
                if not block:
                    break
                size += len(block)
                if size > MAX_BYTES:
                    raise VendorError("vendor download exceeds %d MB" % (MAX_BYTES >> 20))
                digest.update(block)
                output.write(block)
        if digest.hexdigest() != sha256.lower():
            raise VendorError("checksum mismatch for %s (got %s)" % (url, digest.hexdigest()))
        os.replace(temporary, dest)
        temporary = None
    except (OSError, urllib.error.URLError) as exc:
        raise VendorError("download failed for %s: %s" % (url, exc))
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _check_arch(runtime, entry_id, arch):
    if arch == "i386" and not runtime.i386_available():
        raise VendorError("%s is a 32-bit x86 build and the engine has no qemu-i386 "
                          "(update the engine image)" % entry_id)
    if arch == "amd64" and not runtime.x86_64_available():
        raise VendorError("%s is an x86-64 build and the engine has no x86-64 interpreter "
                          "(update the engine image)" % entry_id)


def fetch(entry, native_ok=True):
    """Download (once) every package of an entry; return ``(deb paths, arch)``.

    ``arch`` is the architecture of the binaries selected: ``arm64`` when the
    vendor ships a native build, else the entry's own (``amd64`` / ``i386``).
    """
    files = []
    arch = entry.get("arch", "amd64")
    directory = os.path.join(SUPPORT, "vendor", re.sub(r"[^A-Za-z0-9_.-]", "_", entry["id"]))
    for package in entry.get("packages", []):
        url, sha256 = package["url"], package["sha256"].lower()
        name = os.path.basename(urllib.parse.urlsplit(url).path) or "package"
        dest = os.path.join(directory, sha256[:16] + "-" + name)
        if not os.path.exists(dest) or _sha256(dest) != sha256:
            _download(url, dest, sha256)
        members = package.get("members")
        if not members:
            files.append(dest)
            continue
        # an archive with per-architecture .deb files inside
        chosen = None
        for candidate in (("arm64",) if native_ok else ()) + ("amd64", "i386"):
            if members.get(candidate):
                chosen = candidate
                break
        if not chosen:
            raise VendorError("%s lists no usable architecture" % entry["id"])
        arch = chosen
        files.extend(_extract(dest, members[chosen], directory))
    return files, arch


def _extract(archive, pattern, directory):
    out = []
    target = os.path.join(directory, "extracted")
    try:
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                if not member.isfile() or not fnmatch.fnmatch(member.name, pattern):
                    continue
                if ".." in member.name.split("/") or member.name.startswith("/"):
                    raise VendorError("unsafe path in %s: %s" % (archive, member.name))
                path = os.path.join(target, os.path.basename(member.name))
                if not os.path.exists(path):
                    os.makedirs(target, exist_ok=True)
                    with tar.extractfile(member) as source, open(path + ".tmp", "wb") as sink:
                        for block in iter(lambda: source.read(1 << 16), b""):
                            sink.write(block)
                    os.replace(path + ".tmp", path)
                out.append(path)
    except tarfile.TarError as exc:
        raise VendorError("cannot read %s: %s" % (archive, exc))
    if not out:
        raise VendorError("no file matching %s in %s" % (pattern, os.path.basename(archive)))
    return out


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def install(runtime, entry):
    """Download + verify + ``dpkg -i`` into the engine. Raises VendorError."""
    declared = entry.get("arch", "amd64")
    archives = any(p.get("members") for p in entry.get("packages", []))
    if not archives:
        # plain .deb entries: refuse before spending the download
        _check_arch(runtime, entry["id"], declared)
    files, arch = fetch(entry)
    _check_arch(runtime, entry["id"], arch)
    if not files:
        raise VendorError("%s lists no packages" % entry["id"])
    log("installing vendor driver %s (%s) into the engine (%d package%s)"
        % (entry["id"], arch, len(files), "" if len(files) == 1 else "s"))
    try:
        runtime.install_packages(files, timeout=1200)
    except Exception as exc:  # noqa: BLE001
        raise VendorError(str(exc))
    return files
