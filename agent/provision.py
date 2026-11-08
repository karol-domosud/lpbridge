#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Turn a profile (or a bare detected printer) into a queue in the engine.

    recommendation = resolve_driver(engine, profile)      # what CUPS would pick
    ppd_path       = ensure_queue(engine, profile, recommendation)
    caps           = capabilities(runtime, profile, ppd_path)

The engine's cupsd does the real work (cups-driverd matching, ``lpadmin``,
driverless PPD generation). Results are cached under
``~/Library/Application Support/lpbridge/`` so this is instant after the first
setup; ``--refresh`` redoes it.

A recommendation never says "leave this printer to macOS": whoever runs
lpbridge has already decided the printer goes through the bridge.
"""

import json
import os

from . import vendor
from .engine import EngineError
from .ipp import ppd
from .ipp.attributes import Capabilities
from .ipp.server import log

SUPPORT = os.path.expanduser("~/Library/Application Support/lpbridge")
RESOLVER_VERSION = 3


def _cache(*parts):
    path = os.path.join(SUPPORT, "cache", *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _ppd_path(profile_id):
    path = os.path.join(SUPPORT, "ppd", "%s.ppd" % profile_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def device_from_profile(profile):
    """What the engine's ranking needs to know about a printer."""
    driver = profile.get("driver", {})
    match = profile.get("match", {})
    make = driver.get("make") or profile.get("manufacturer")
    model = driver.get("model")
    if not model and profile.get("name"):
        parts = profile["name"].split(None, 1)
        if not make and parts:
            make = parts[0]
        if len(parts) > 1:
            model = parts[1]
    return {
        "make": make, "model": model,
        "device_id": match.get("device_id"),
        "pdl": match.get("pdl"),
        "transport": profile.get("transport", {}).get("kind"),
        "name": profile.get("name"),
    }


def resolve_driver(engine, profile, refresh=False):
    """The ranked driver candidates for a printer, best first (cached)."""
    driver = profile.get("driver", {})
    if driver.get("ppd"):
        return {
            "ppd_name": driver["ppd"], "driver": "(profile)",
            "make_and_model": driver["ppd"], "source": "profile",
            "candidates": [], "available": True,
            "note": "driver pinned by profile",
        }

    cache = _cache("%s.resolve.json" % profile["id"])
    if os.path.exists(cache) and not refresh:
        try:
            with open(cache) as handle:
                cached = json.load(handle)
            if cached.get("available") and cached.get("_resolver_version") == RESOLVER_VERSION:
                return cached
        except (OSError, ValueError):
            pass

    device = device_from_profile(profile)
    log("asking the engine which driver %s / %s needs ..."
        % (device["make"] or "?", device["model"] or "?"))
    rec = lookup_driver(engine, device, profile=profile)
    rec["_resolver_version"] = RESOLVER_VERSION
    with open(cache, "w") as handle:
        json.dump(rec, handle, indent=2)
    _log_recommendation(profile, rec)
    return rec


def lookup_driver(engine, device, profile=None, install_vendor=True):
    """Rank drivers; when a vendor package is known for the model and the
    engine has none of its own, install it and rank again."""
    candidates = engine.rank_drivers(device)
    vendor_entry = vendor.match(device, profile)
    if vendor_entry and install_vendor:
        specific = [c for c in candidates if c.get("source") == "device-id"]
        if not specific or vendor_entry.get("prefer"):
            try:
                vendor.install(engine.runtime, vendor_entry)
                candidates = engine.rank_drivers(device)
            except vendor.VendorError as exc:
                log("vendor driver for %s not installed: %s" % (device.get("model"), exc))
    if not candidates:
        return {
            "ppd_name": None, "driver": None, "make_and_model": None, "source": None,
            "candidates": [], "available": False,
            "note": "no driver found for this printer" + (
                "; a vendor package exists but could not be installed" if vendor_entry else ""),
        }
    best = candidates[0]
    return {
        "ppd_name": best["ppd_name"], "driver": best.get("driver"),
        "make_and_model": best.get("make_and_model"), "source": best.get("source"),
        "candidates": [
            {k: c.get(k) for k in ("ppd_name", "driver", "make_and_model", "source", "generic")}
            for c in candidates],
        "available": True,
        "note": _note(best),
    }


def _note(candidate):
    source = candidate.get("source")
    if source == "device-id":
        return "exact match for this model"
    if source == "driverless":
        return "the printer's own IPP Everywhere support, through lpbridge"
    if source == "make-and-model":
        return "closest model the engine knows"
    if source == "generic":
        return "generic driver for a language the printer reports"
    return ""


def _log_recommendation(profile, rec):
    if rec.get("available"):
        log("%s: driver '%s' (%s) -- %s" % (profile["id"], rec.get("driver"),
                                             rec.get("ppd_name"), rec.get("note")))
    else:
        log("%s: no usable driver -- %s" % (profile["id"], rec.get("note")))


def ensure_queue(engine, profile, recommendation, device_uri, refresh=False, ipp_uri=None):
    """Create/refresh the engine queue for this profile; return the Mac-side
    path of the PPD cupsd installed for it (used for the PPD -> IPP map)."""
    ppd_name = recommendation.get("ppd_name")
    if not ppd_name:
        raise EngineError(recommendation.get("note") or
                          "no driver is available for %s" % profile["name"])
    path = _ppd_path(profile["id"])
    log("queue %s: %s -> %s" % (profile["id"], ppd_name, device_uri))
    engine.add_queue(profile["id"], device_uri, ppd_name, info=profile["name"],
                     location=profile.get("location") or "lpbridge", ipp_uri=ipp_uri)
    try:
        data = engine.queue_ppd(profile["id"])
    except Exception as exc:  # noqa: BLE001
        data = b""
        log("no PPD for queue %s: %s" % (profile["id"], exc))
    if not data.startswith(b"*PPD-Adobe"):
        # `lpadmin -m everywhere` exits 0 even when the printer never
        # answered; without a PPD the queue is useless, so say why.
        engine.remove_queue(profile["id"])
        if ppd_name == "everywhere":
            raise EngineError("could not read the printer's IPP capabilities at %s -- "
                              "make sure it is switched on and reachable, then try again"
                              % device_uri)
        raise EngineError("the engine installed no PPD for %s (%s)" % (profile["id"], ppd_name))
    if os.path.exists(path):
        with open(path, "rb") as handle:
            if handle.read() != data:
                _drop(_cache("%s.caps.json" % profile["id"]))
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def capabilities(runtime, profile, ppd_path, refresh=False):
    cache = _cache("%s.caps.json" % profile["id"])
    if os.path.exists(cache) and not refresh:
        try:
            with open(cache) as handle:
                data = json.load(handle)
            # an older dump (the awk one) knows no paper sizes or colour flag
            if data.get("version") == ppd.VERSION:
                return Capabilities(data)
        except (OSError, ValueError):
            pass
    log("reading printer capabilities from the PPD ...")
    data = runtime.capabilities(ppd_path, profile=profile)
    with open(cache, "w") as handle:
        json.dump(data, handle, indent=2)
    return Capabilities(data)


# -- drivers the user dropped in ------------------------------------------
# Kept on the Mac so they survive an engine image update; re-installed into
# the engine on every start (a PPD copy is instant, a .deb is skipped once
# the engine remembers its checksum).
USER_DRIVERS = os.path.join(SUPPORT, "user-drivers")


def add_user_driver(engine, name, data):
    """Install a .ppd / .ppd.gz / .deb the user supplied; returns (kind, detail)."""
    import gzip
    import hashlib
    import re
    base = os.path.basename(name or "driver")
    lowered = base.lower()
    if lowered.endswith(".ppd.gz"):
        data, base = gzip.decompress(data), base[:-3]
        lowered = base.lower()
    if lowered.endswith(".ppd"):
        ppd_name = engine.install_ppd(base, data)
        kind = "ppd"
    elif lowered.endswith(".deb"):
        kind = "deb"
    else:
        raise EngineError("%s: only .ppd, .ppd.gz and .deb files can be installed" % base)
    os.makedirs(USER_DRIVERS, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9_.+-]+", "_", base)
    path = os.path.join(USER_DRIVERS, "%s-%s" % (digest, safe))
    with open(path + ".tmp", "wb") as handle:
        handle.write(data)
    os.replace(path + ".tmp", path)
    if kind == "deb":
        _install_user_deb(engine, path)
        return kind, "installed %s into the engine" % base
    return kind, ppd_name


def _install_user_deb(engine, path):
    marker = "/var/lib/lpbridge/installed/%s" % os.path.basename(path)
    check = engine.exec(["test", "-e", marker])
    if check["exit_code"] == 0:
        return
    engine.install_deb(path)
    engine.exec(["sh", "-c", "mkdir -p /var/lib/lpbridge/installed && touch %s" % marker])


def restore_user_drivers(engine):
    """Re-install everything under USER_DRIVERS (engine start / image update)."""
    if not os.path.isdir(USER_DRIVERS):
        return 0
    count = 0
    for name in sorted(os.listdir(USER_DRIVERS)):
        path = os.path.join(USER_DRIVERS, name)
        if name.endswith(".tmp") or not os.path.isfile(path):
            continue
        original = name.split("-", 1)[1] if "-" in name else name
        try:
            if name.lower().endswith(".ppd"):
                with open(path, "rb") as handle:
                    engine.install_ppd(original, handle.read())
            elif name.lower().endswith(".deb"):
                _install_user_deb(engine, path)
            else:
                continue
            count += 1
        except Exception as exc:  # noqa: BLE001
            log("user driver %s not restored: %s" % (original, exc))
    if count:
        log("restored %d user-supplied driver file(s) into the engine" % count)
    return count


def _drop(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def clear_cache(profile_id):
    for suffix in (".resolve.json", ".caps.json"):
        _drop(_cache("%s%s" % (profile_id, suffix)))
    _drop(_ppd_path(profile_id))
