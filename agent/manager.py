#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Serve many printers from one agent process.

One HTTP server, one runtime (one VM / podman machine), one lifecycle — and N
printers, each with its own profile, PPD, capabilities, transport and job
queue, addressed as ``/ipp/print/<id>``.

The manager is what the watch loop and the web UI both talk to:

    manager.add(profile)         provision + register + create the macOS queue
    manager.remove(id)           unregister (optionally drop the queue)
    manager.sync(devices)        add newly-attached printers, flag missing ones
    manager.status()             a JSON-able snapshot for the UI
"""

import os
import threading
import time
from urllib.parse import urlsplit

from . import discovery, macos, provision, shell
from .engine import Engine
from .ipp.server import VirtualPrinter, advertise, log
from .pipeline import Pipeline
from .transport.dryrun import DryRunTransport
from .transport.engine import EngineTransport
from .transport.rawqueue import RawQueueTransport


RETRY_AFTER = 300   # seconds before an auto-add that failed is tried again


class PrinterManager(object):
    def __init__(self, runtime, lifecycle, registry, options):
        self.runtime = runtime
        self.engine = Engine(runtime)
        self.lifecycle = lifecycle
        self.shells = shell.ShellHolds(runtime, lifecycle)
        self.registry = registry
        self.options = options          # the parsed argparse Namespace
        self._entries = {}              # id -> dict(profile, caps, transport, ...)
        self._lock = threading.Lock()
        self.selections = None
        self._mutation_lock = threading.RLock()
        self._failed = {}               # id -> time of the last failed auto add
        self._user_drivers_restored = False

    # -- queries ----------------------------------------------------
    def ids(self):
        with self._lock:
            return sorted(self._entries)

    def entry(self, printer_id):
        with self._lock:
            return self._entries.get(printer_id)

    def status(self):
        out = []
        for printer_id in self.ids():
            entry = self.entry(printer_id)
            if not entry:
                continue
            vp = entry["vprinter"]
            with vp.lock:
                jobs = list(vp.jobs.values())
                offline, fault = vp.offline, vp.fault
            out.append({
                "id": printer_id,
                "name": entry["profile"]["name"],
                "driver": entry["recommendation"].get("driver"),
                "ppd_name": entry["recommendation"].get("ppd_name"),
                "candidates": entry["recommendation"].get("candidates") or [],
                "device_uri": entry.get("device_uri"),
                "make_and_model": entry["caps"].nickname,
                "transport": entry["transport"].describe(),
                "endpoint": entry["endpoint"],
                "auto": entry["profile"].get("auto", False),
                "offline": offline,
                "fault": list(fault) if fault else None,
                "queued": sum(1 for j in jobs if j["state"] < 7),
                "jobs": [{
                    "id": j["id"],
                    "name": (j["operation"].get("job-name") or (None, [None]))[1][0]
                    if j["operation"].get("job-name") else "Untitled",
                    "state": j["state"],
                    "reasons": list(j["reasons"]),
                    "created": int(j["created"]),
                    "error": j["error"],
                } for j in sorted(jobs, key=lambda j: -j["id"])[:10]],
            })
        return out

    # -- add / remove ---------------------------------------------
    def add(self, profile, create_queue=True, ppd_name=None):
        """Provision a printer and start serving it. Returns (ok, message).
        ``ppd_name`` picks one of the resolver's candidates (or any CUPS
        ppd-name) instead of the recommended one."""
        with self._mutation_lock:
            self.lifecycle.job_started(setup=True)
            try:
                return self._add(profile, create_queue, ppd_name)
            finally:
                self.lifecycle.job_finished()

    def _add(self, profile, create_queue=True, ppd_name=None):
        printer_id = profile["id"]
        if self.entry(printer_id):
            return True, "already serving %s" % printer_id
        self.restore_user_drivers()

        opts = self.options
        try:
            recommendation = provision.resolve_driver(self.engine, profile,
                                                      refresh=opts.refresh)
            if ppd_name and ppd_name != recommendation.get("ppd_name"):
                # the user picked another candidate: honour it, remember it
                recommendation = dict(recommendation, ppd_name=ppd_name,
                                      driver=_candidate_label(recommendation, ppd_name),
                                      source="user", note="chosen by you")
                profile["driver"]["ppd"] = ppd_name
            device_uri = self._device_uri(profile)
            ppd_path = provision.ensure_queue(self.engine, profile, recommendation,
                                              device_uri, refresh=opts.refresh,
                                              ipp_uri=self._ipp_uri(profile))
            caps = provision.capabilities(self.runtime, profile, ppd_path,
                                          refresh=opts.refresh)
        except Exception as exc:  # noqa: BLE001
            log("could not set up '%s': %s" % (profile["name"], exc))
            return False, str(exc)

        try:
            transport = self._build_transport(profile)
            if create_queue and not opts.no_auto_queue and isinstance(transport, RawQueueTransport):
                transport._ensure_queue()
        except Exception as exc:
            return False, str(exc)
        pipeline = Pipeline(profile, caps, self.runtime, transport,
                            engine=self.engine, queue=printer_id,
                            relay=not isinstance(transport, EngineTransport),
                            keep_spool=opts.keep_spool, ppd_path=ppd_path,
                            reprovision=lambda: provision.ensure_queue(
                                self.engine, profile, recommendation, device_uri,
                                ipp_uri=self._ipp_uri(profile)))
        vprinter = VirtualPrinter(profile, caps, pipeline,
                                  boot_timeout=opts.boot_timeout,
                                  lifecycle=self.lifecycle)
        endpoint = "ipp://localhost:%d/ipp/print/%s" % (opts.port, printer_id)

        self.registry.register(printer_id, vprinter)
        advertiser = advertise(profile, opts.port) if opts.advertise else None
        with self._lock:
            self._entries[printer_id] = {
                "profile": profile, "caps": caps, "transport": transport,
                "pipeline": pipeline, "vprinter": vprinter,
                "recommendation": recommendation, "endpoint": endpoint,
                "device_uri": device_uri,
                "advertiser": advertiser, "added": time.time(),
                "missing_since": 0,
            }

        if create_queue and not opts.no_auto_queue:
            ready = macos.ensure_everywhere_queue(
                printer_id, endpoint,
                description="%s (lpbridge)" % profile["name"],
                location=profile.get("location") or "")
            if not ready:
                self.remove(printer_id, drop_queue=False)
                return False, "macOS could not add the print queue. Check your printer administration permissions in System Settings, then try again."
        if self.selections:
            self.selections.update(printer_id, profile)
        log("serving '%s' at %s (driver %s)"
            % (profile["name"], endpoint, caps.nickname))
        return True, "ok"

    def remove(self, printer_id, drop_queue=True):
        entry = None
        with self._lock:
            entry = self._entries.pop(printer_id, None)
        self.registry.unregister(printer_id)
        if not entry:
            return False, "not serving %s" % printer_id
        if entry.get("advertiser"):
            entry["advertiser"].terminate()
        if drop_queue:
            macos.remove_queue(printer_id)
            macos.remove_queue("_lpbridge_%s" % printer_id)
            if self.selections:
                self.selections.update(printer_id)
            try:
                self.engine.remove_queue(printer_id)
            except Exception as exc:  # noqa: BLE001
                log("engine queue %s not removed: %s" % (printer_id, exc))
            provision.clear_cache(printer_id)
        log("stopped serving '%s'" % entry["profile"]["name"])
        return True, "ok"

    def shutdown(self):
        for printer_id in self.ids():
            self.remove(printer_id, drop_queue=False)

    # -- presence -------------------------------------------------
    def sync(self, devices, device_to_profile, auto_add=True):
        """Reconcile against what discovery currently sees."""
        seen = set()
        for device in devices:
            profile = device_to_profile(device)
            seen.add(profile["id"])
            if not self.entry(profile["id"]) and auto_add:
                # a printer that could not be set up is retried, but not on
                # every scan: the reason is in the log and the app
                if time.time() - self._failed.get(profile["id"], 0) < RETRY_AFTER:
                    continue
                ok, _message = self.add(profile)
                self._failed[profile["id"]] = 0 if ok else time.time()

        for printer_id in self.ids():
            entry = self.entry(printer_id)
            if not entry:
                continue
            if entry["profile"].get("transport", {}).get("kind") != "usb-raw":
                continue          # network / dryrun printers are not hot-plugged
            present = printer_id in seen
            entry["vprinter"].set_offline(not present)
            entry["missing_since"] = 0 if present else (
                entry["missing_since"] or time.time())

    def restore_user_drivers(self):
        """Files the user dropped in are re-installed once per agent run."""
        if self._user_drivers_restored:
            return
        try:
            provision.restore_user_drivers(self.engine)
            self._user_drivers_restored = True
        except Exception as exc:  # noqa: BLE001
            log("could not restore user drivers: %s" % exc)

    def add_user_driver(self, name, data):
        """Install a driver file the user supplied and forget cached rankings."""
        with self._mutation_lock:
            self.lifecycle.job_started(setup=True)
            try:
                self.restore_user_drivers()
                kind, detail = provision.add_user_driver(self.engine, name, data)
            finally:
                self.lifecycle.job_finished()
        for name_ in os.listdir(os.path.join(provision.SUPPORT, "cache")) if os.path.isdir(
                os.path.join(provision.SUPPORT, "cache")) else []:
            if name_.endswith(".resolve.json"):
                provision._drop(os.path.join(provision.SUPPORT, "cache", name_))
        return kind, detail

    # -- helpers ------------------------------------------------
    def _device_uri(self, profile):
        """The engine-side device URI: the printer's own network address for
        cupsd's backends, or the lpbridge relay for USB and dry runs."""
        kind = self.options.transport or profile["transport"]["kind"]
        cfg = profile["transport"]
        if kind in ("usb-raw", "dryrun"):
            return "lpbridge:///%s" % profile["id"]
        host = cfg.get("host")
        address = discovery.resolve_host(host) or host
        if not address:
            raise provision.EngineError("no network address for %s" % profile["name"])
        if kind == "ipp":
            parts = urlsplit(cfg.get("uri") or "ipp://%s/ipp/print" % host)
            return "ipp://%s:%d%s" % (address, parts.port or 631, parts.path or "/ipp/print")
        if kind == "lpd":
            parts = urlsplit(cfg.get("uri") or "lpd://%s/" % host)
            return "lpd://%s%s" % (address, parts.path or "/")
        # jetdirect / AppSocket -- the distro default for driver-based queues.
        # Driverless queues need the IPP endpoint instead.
        if profile["driver"].get("ppd") == "everywhere" and cfg.get("ipp_uri"):
            parts = urlsplit(cfg["ipp_uri"])
            return "ipp://%s:%d%s" % (address, parts.port or 631, parts.path or "/ipp/print")
        return "socket://%s:%d" % (address, cfg.get("port") or 9100)

    def _ipp_uri(self, profile):
        """The printer's IPP endpoint by address, for driverless setup."""
        cfg = profile["transport"]
        uri = cfg.get("ipp_uri") or (cfg.get("uri") if cfg.get("kind") == "ipp" else None)
        if not uri:
            return None
        parts = urlsplit(uri)
        address = discovery.resolve_host(parts.hostname) or parts.hostname
        return "ipp://%s:%d%s" % (address, parts.port or 631, parts.path or "/ipp/print")

    def _build_transport(self, profile):
        opts = self.options
        kind = opts.transport or profile["transport"]["kind"]
        cfg = profile["transport"]
        if kind == "dryrun":
            return DryRunTransport(opts.output_dir or _default_out())
        if kind in ("jetdirect", "ipp", "lpd"):
            return EngineTransport(cfg.get("uri") or cfg.get("host"))
        return RawQueueTransport(
            profile, uri=cfg.get("uri"),
            match={"serial": cfg.get("serial"), "model": profile["name"],
                   "device_id": profile["match"].get("device_id")})


def _candidate_label(recommendation, ppd_name):
    for candidate in recommendation.get("candidates") or []:
        if candidate.get("ppd_name") == ppd_name:
            return candidate.get("driver") or ppd_name
    return ppd_name


def _default_out():
    import os
    path = os.path.expanduser("~/Library/Application Support/lpbridge/out")
    os.makedirs(path, exist_ok=True)
    return path
