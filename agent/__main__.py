#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""lpbridge agent -- macOS-side entry point.

    python3 -m agent                       # serve every printer we can find
    python3 -m agent --discover            # what's attached + which driver
    python3 -m agent --profile my-printer  # serve one pinned profile
    python3 -m agent --uninstall           # remove the macOS queues

One process serves any number of printers, each at
``ipp://localhost:8631/ipp/print/<id>``, and answers the control API the
setup app uses on the same port. The agent creates and removes the macOS print
queues itself and keeps watching for printers to come and go -- the user never
touches a terminal (invariant 6: LaunchAgent in the login session).
"""

import argparse
import hashlib
import fcntl
import signal
import os
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

from . import discovery, macos, provision
from .ipp.server import IppServer, PrinterRegistry, advertise, log
from .manager import PrinterManager
from .profiles import schema
from .runtime.factory import make_runtime
from .runtime.lifecycle import Lifecycle
from .control import ControlAPI
from .selections import Selections

SUPPORT = os.path.expanduser("~/Library/Application Support/lpbridge")
WATCH_INTERVAL = 20


def _support(*parts):
    path = os.path.join(SUPPORT, *parts)
    os.makedirs(path, exist_ok=True)
    return path


def _repo(*parts):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, *parts)


def _slug(*parts):
    text = "-".join(p for p in parts if p)
    out = "".join(c if c.isalnum() else "-" for c in text.lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "printer"


def _transport_for_device(device):
    kind = device.get("transport")
    if kind in ("jetdirect", "ipp", "lpd", "network-raw"):
        parts = urlsplit(device["uri"])
        return {"kind": "lpd" if kind == "network-raw" else kind,
                "host": parts.hostname, "port": parts.port,
                "uri": device.get("uri"), "ipp_uri": device.get("ipp_uri")}
    return {"kind": "usb-raw", "uri": device.get("uri")}


def device_to_profile(device):
    return schema.auto_profile(
        _slug(device.get("make"), device.get("model")) + "-" +
        hashlib.sha256(str(device.get("uri") or device.get("name") or "")
                       .encode()).hexdigest()[:8],
        device.get("name") or _slug(device.get("make"), device.get("model")),
        make=device.get("make"), model=device.get("model"),
        device_id=device.get("device_id"), pdl=device.get("pdl"),
        transport=_transport_for_device(device))


def _filtered(devices, args):
    if args.auto and args.auto is not True:
        t = args.auto.lower()
        devices = [d for d in devices
                   if t in (d.get("name") or "").lower()
                   or t in (d.get("model") or "").lower()
                   or t in d.get("uri", "").lower()]
    return devices


def _find_devices(args):
    """One-shot scan (used by --uninstall, which has no cache running)."""
    return _filtered(discovery.detect(network=not args.no_network), args)


def _wait_listening(host, port, timeout=10):
    deadline = time.time() + timeout
    probe = "127.0.0.1" if host in ("0.0.0.0", "") else host
    while time.time() < deadline:
        try:
            with socket.create_connection((probe, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="agent", description="lpbridge agent")
    parser.add_argument("--profile", help="serve only this profile id")
    parser.add_argument("--managed", action="store_true",
                        help="restore only printers explicitly selected in the app")
    parser.add_argument("--profiles-dir", default=_repo("profiles"))
    parser.add_argument("--auto", nargs="?", const=True, metavar="NAME",
                        help="only serve detected printers matching NAME")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8631)
    parser.add_argument("--transport", choices=("usb-raw", "jetdirect", "ipp", "lpd", "dryrun"),
                        help="override the detected transport (dryrun keeps output on the Mac)")
    parser.add_argument("--output-dir", help="dryrun transport output directory")
    parser.add_argument("--advertise", action="store_true")
    parser.add_argument("--no-auto-queue", action="store_true",
                        help="do not create the macOS print queues")
    parser.add_argument("--no-watch", action="store_true",
                        help="do not keep watching for printers to appear/leave")
    parser.add_argument("--refresh", action="store_true",
                        help="re-run driver resolution / PPD / capabilities")
    parser.add_argument("--build", action="store_true",
                        help="force an engine image rebuild (podman runtime)")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove the macOS queues and exit")
    parser.add_argument("--keep-spool", action="store_true")
    parser.add_argument("--grace", type=int, default=120)
    parser.add_argument("--boot-timeout", type=int, default=120)
    parser.add_argument("--ignore-battery", action="store_true",
                        help="keep the VM up even on battery")
    parser.add_argument("--machine-provider", default="applehv")
    parser.add_argument("--runtime", choices=("auto", "podman", "vfkit"),
                        default="auto")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    profiles = schema.load_dir(args.profiles_dir)

    if args.discover:
        runtime = make_runtime(args.runtime,
                               machine_provider=args.machine_provider)
        rt = None
        try:
            runtime.ensure_up(timeout=args.boot_timeout)
            rt = runtime
        except Exception as exc:  # noqa: BLE001
            log("(driver hints unavailable: %s)" % exc)
        print(discovery.report(profiles, runtime=rt, network=not args.no_network))
        return 0

    # what are we asked to serve? -----------------------------------
    pinned = []
    if args.profile:
        if args.profile not in profiles:
            print("unknown profile '%s' (have: %s)"
                  % (args.profile, ", ".join(sorted(profiles)) or "none"),
                  file=sys.stderr)
            return 2
        pinned = [profiles[args.profile]]
    elif not args.managed and not args.auto and profiles:
        pinned = list(profiles.values())

    if args.uninstall:
        targets = pinned or [device_to_profile(d) for d in _find_devices(args)]
        if not targets:
            log("nothing to uninstall")
        for prof in targets:
            macos.remove_queue(prof["id"])
            macos.remove_queue("_lpbridge_%s" % prof["id"])
            provision.clear_cache(prof["id"])
            log("uninstalled '%s'" % prof["name"])
        return 0

    # A second app/LaunchAgent must never boot against the same disk or spool.
    lease = open(os.path.join(_support(), "agent.lock"), "a")
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another lpbridge print service is already running")
        return 1
    runtime = make_runtime(args.runtime, machine_provider=args.machine_provider)
    try:
        if args.build:
            runtime.build(force=True)
        runtime.ensure_up(timeout=args.boot_timeout)
    except Exception as exc:  # noqa: BLE001
        log("runtime unavailable: %s" % exc)
        return 1

    lifecycle = Lifecycle(runtime, grace=args.grace,
                          respect_battery=not args.ignore_battery)
    lifecycle.job_finished()  # Booting for setup also starts the idle countdown.
    registry = PrinterRegistry()
    manager = PrinterManager(runtime, lifecycle, registry, args)
    if args.managed:
        manager.selections = Selections(os.path.join(SUPPORT, "printers.json"))
        # stored profiles may predate the current schema; normalise them
        pinned = []
        for stored in manager.selections.read().values():
            try:
                pinned.append(schema.normalise(stored, source="<selection>"))
            except schema.ProfileError as exc:
                log("ignoring a stored printer selection: %s" % exc)
    # a scan costs seconds (lpinfo alone can take 15s), so it runs behind
    # everything; the UI and the watch loop read the last known list
    devices = discovery.DeviceCache(network=not args.no_network,
                                    interval=WATCH_INTERVAL).start()
    control = ControlAPI(manager, device_to_profile, devices,
                  network=not args.no_network)

    try:
        server = IppServer((args.host, args.port), registry, control)
    except OSError as exc:
        log("cannot bind %s:%d: %s" % (args.host, args.port, exc))
        lifecycle.stop()
        return 1
    threading.Thread(target=server.serve_forever, name="ipp-http",
                     daemon=True).start()
    _wait_listening(args.host, args.port)
    log("control API: http://localhost:%d/api/ (the lpbridge app talks to this)" % args.port)

    for profile in pinned:
        manager.add(profile)
    if not pinned and not args.managed:
        for device in _filtered(devices.devices(), args):
            manager.add(device_to_profile(device))

    if manager.ids():
        log("ready. %d printer(s) in the Print dialog." % len(manager.ids()))
    else:
        log("no printers yet -- watching. Open http://localhost:%d/ to add one."
            % args.port)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    watch = not args.no_watch and (args.managed or not pinned)
    try:
        while not stop.wait(WATCH_INTERVAL):
            if watch:
                try:
                    manager.sync(_filtered(devices.devices(), args),
                                 device_to_profile, auto_add=not args.managed)
                except Exception as exc:  # noqa: BLE001
                    log("watch pass failed: %s" % exc)
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        devices.stop()
        manager.shutdown()
        server.shutdown()
        lifecycle.stop()
        runtime.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
