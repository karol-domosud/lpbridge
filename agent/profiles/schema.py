#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Profile schema: validate, normalise, derive defaults.

A profile is now mostly optional. The common path is: the agent detects a
printer, asks the engine's CUPS which driver it needs, and serves it -- no
YAML at all. A profile file exists to pin a known-good choice (``driver.ppd``
is a CUPS ``ppd-name`` as ``lpinfo -m`` lists it, or ``everywhere``), name a
vendor ``.deb`` to install into the engine, or set the transport for a device
that cannot be auto-detected (a plain socket printer, say).

Minimum valid profile:

    id: my-printer
    match:
      usb: [{vendor: "0x04e8", product: "0x3441"}]

Everything else has a default or is resolved at run time.
"""

import hashlib
import os
import uuid as _uuid

from . import loader

VALID_TRANSPORTS = ("usb-raw", "jetdirect", "ipp", "lpd", "dryrun")

_NS = _uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


class ProfileError(Exception):
    pass


def _require(cond, message):
    if not cond:
        raise ProfileError(message)


def normalise(data, source=None):
    _require(isinstance(data, dict), "profile is not a mapping")
    label = source or data.get("id") or "<profile>"

    pid = data.get("id")
    _require(isinstance(pid, str) and pid, "%s: missing 'id'" % label)
    _require(pid.replace("-", "").replace("_", "").isalnum(),
             "%s: id must be alphanumeric/dash/underscore" % label)

    name = data.get("name") or pid
    aliases = data.get("aliases") or []
    _require(isinstance(aliases, list), "%s: aliases must be a list" % label)

    match = data.get("match") or {}
    _require(isinstance(match, dict), "%s: match must be a mapping" % label)
    usb = match.get("usb") or []
    for entry in usb:
        _require(isinstance(entry, dict) and "vendor" in entry
                 and "product" in entry,
                 "%s: match.usb entries need vendor+product" % label)
    mdns = match.get("mdns") or []

    driver = data.get("driver") or {}
    _require(isinstance(driver, dict), "%s: driver must be a mapping" % label)
    ppd = driver.get("ppd") or driver.get("ppd_name")
    legacy = driver.get("ppd_spec")
    if ppd is None and legacy:
        ppd = legacy_ppd_name(legacy)
        _require(ppd, "%s: driver.ppd_spec '%s' has no CUPS equivalent; use driver.ppd "
                 "with a name from `lpinfo -m`" % (label, legacy))
    _require(ppd is None or (isinstance(ppd, str) and ppd),
             "%s: driver.ppd must be a CUPS ppd-name or 'everywhere'" % label)

    packages = driver.get("packages") or []
    _require(isinstance(packages, list), "%s: driver.packages must be a list" % label)
    for entry in packages:
        _require(isinstance(entry, dict) and entry.get("url") and entry.get("sha256"),
                 "%s: each driver.packages entry needs url + sha256" % label)

    transport = data.get("transport") or {"kind": "dryrun"}
    _require(isinstance(transport, dict), "%s: transport must be a mapping" % label)
    kind = transport.get("kind", "dryrun")
    _require(kind in VALID_TRANSPORTS,
             "%s: transport.kind must be one of %s" % (label, VALID_TRANSPORTS))
    if kind in ("jetdirect", "ipp", "lpd"):
        _require(transport.get("host") or transport.get("uri"),
                 "%s: %s transport needs host (or uri)" % (label, kind))

    return {
        "id": pid,
        "name": name,
        "aliases": aliases,
        "manufacturer": data.get("manufacturer") or driver.get("make")
        or name.split()[0],
        "location": data.get("location"),
        "uuid": "urn:uuid:%s" % _uuid.uuid5(_NS, pid),
        "match": {"usb": usb, "mdns": mdns, "device_id": match.get("device_id"),
                  "pdl": match.get("pdl")},
        "driver": {
            "ppd": ppd,
            "make": driver.get("make"),
            "model": driver.get("model"),
            "packages": packages,
            "vendor": driver.get("vendor"),
        },
        "transport": dict(transport, kind=kind),
        "capabilities": data.get("capabilities") or {},
        "auto": False,
        "source": source,
    }


def auto_profile(pid, name, make=None, model=None, device_id=None,
                 transport=None, ppd=None, pdl=None):
    """Build an in-memory profile for a detected printer (no YAML file)."""
    match = {}
    if device_id:
        match["device_id"] = device_id
    if pdl:
        match["pdl"] = pdl
    prof = normalise({
        "id": pid,
        "name": name,
        "manufacturer": make,
        "match": match,
        "driver": {"make": make, "model": model, "ppd": ppd},
        "transport": transport or {"kind": "dryrun"},
    }, source="<auto>")
    prof["auto"] = True
    return prof


def legacy_ppd_name(spec):
    """Older profiles named PPDs by lpbridge's own scheme; map what can be
    mapped onto CUPS ppd-names."""
    if spec == "driverless":
        return "everywhere"
    if spec.startswith("foomatic:") and "/" in spec:
        printer_id, driver = spec[len("foomatic:"):].split("/", 1)
        return "foomatic:%s-%s.ppd" % (printer_id, driver)
    if spec.startswith("cupsdriver:"):
        rest = spec[len("cupsdriver:"):]
        return rest.split(":", 1)[1] if ":" in rest else None
    return None


def load_file(path):
    return normalise(loader.load_path(path), source=os.path.basename(path))


def load_dir(directory):
    profiles = {}
    if not os.path.isdir(directory):
        return profiles
    for name in sorted(os.listdir(directory)):
        if not name.endswith((".yaml", ".yml")):
            continue
        profile = load_file(os.path.join(directory, name))
        _require(profile["id"] not in profiles,
                 "duplicate profile id: %s" % profile["id"])
        profiles[profile["id"]] = profile
    return profiles


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
