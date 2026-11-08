#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""macOS CUPS queue management -- so the user never opens a terminal.

The agent owns two queues per printer:

* the **visible** IPP Everywhere queue the user prints to, pointed at our
  loopback IPP endpoint. Created with ``lpadmin -m everywhere`` once the agent
  is listening.
* the **hidden** raw queue that moves the finished bytes to the device via
  Apple's own ``usb://`` / ``socket://`` backend. Name prefixed ``_`` and
  ``printer-is-shared=false`` so it stays out of the Print dialog.

Everything here is idempotent: safe to run on every agent start.
"""

import re
import subprocess
import time

from .ipp.server import log

LPADMIN = "/usr/sbin/lpadmin"
LPINFO = "/usr/sbin/lpinfo"
LPSTAT = "/usr/bin/lpstat"


def _run(args, timeout=30):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        class _R(object):
            returncode = 1
            stdout = ""
            stderr = str(exc)
        return _R()


def queue_exists(name):
    return _run([LPSTAT, "-p", name]).returncode == 0


def queue_device_uri(name):
    out = _run([LPSTAT, "-v", name]).stdout
    m = re.search(r"device for %s: (\S+)" % re.escape(name), out)
    return m.group(1) if m else None


def ensure_everywhere_queue(name, endpoint, description=None, location=None):
    """Create/point the visible driverless queue at our IPP endpoint.

    Returns True if the queue is present and pointed where we want.
    """
    want_uri = endpoint
    if queue_exists(name) and queue_device_uri(name) == want_uri:
        _run([LPADMIN, "-p", name, "-E"])  # ensure enabled/accepting
        return True

    args = [LPADMIN, "-p", name, "-E", "-v", want_uri, "-m", "everywhere"]
    if description:
        args += ["-D", description]
    if location is not None:
        args += ["-L", location]
    result = _run(args, timeout=45)
    if result.returncode != 0:
        # older macOS: driverless model may be 'everywhere' unavailable until
        # the server has been probed once; retry after a beat
        time.sleep(2)
        result = _run(args, timeout=45)
    if result.returncode != 0:
        log("could not create queue %s: %s" % (name, result.stderr.strip()))
        return False
    _run([LPADMIN, "-p", name, "-o", "printer-is-shared=false"])
    log("queue '%s' is in the Print dialog (-> %s)" % (name, want_uri))
    return True


def remove_queue(name):
    if queue_exists(name):
        _run([LPADMIN, "-x", name])
        log("removed queue '%s'" % name)


#: backends that make `lpinfo -v` take ~15s. We do our own Bonjour scan with
#: dns-sd, so losing CUPS's dnssd probe here costs nothing. (macOS ignores
#: --include-schemes but honours --exclude-schemes: 15s -> 1.2s.)
SLOW_SCHEMES = "smb,lpd,snmp,dnssd,bluetooth,mdns"


def usb_socket_uris():
    """Every candidate device URI Apple's backends can see, with device-id."""
    result = _run([LPINFO, "-l", "-v", "--exclude-schemes", SLOW_SCHEMES],
                  timeout=30)
    if result.returncode != 0:
        result = _run([LPINFO, "-l", "-v"], timeout=30)
    entries = []
    current = {}
    for line in result.stdout.splitlines():
        if line.startswith("Device: uri = "):
            if current.get("uri"):
                entries.append(current)
            current = {"uri": line.split("= ", 1)[1].strip()}
        elif "=" in line and current:
            key, _, value = line.strip().partition(" = ")
            current[key.strip()] = value.strip()
    if current.get("uri"):
        entries.append(current)
    return [e for e in entries
            if e["uri"].startswith(("usb://", "socket://", "dnssd://"))]


def resolve_device_uri(match=None):
    """Pick the device URI for a printer, by serial / model / device-id."""
    match = match or {}
    wanted_serial = str(match.get("serial") or "").lower()
    wanted_model = str(match.get("model") or "").lower()
    wanted_devid = str(match.get("device_id") or "").lower()
    candidates = usb_socket_uris()
    for entry in candidates:
        blob = (entry.get("uri", "") + " " + entry.get("device-id", "")).lower()
        if wanted_serial and wanted_serial in blob:
            return entry["uri"]
        if wanted_devid and _devid_model(wanted_devid) and \
                _devid_model(wanted_devid) in blob:
            return entry["uri"]
        if wanted_model and wanted_model.replace(" ", "") in blob.replace(" ", ""):
            return entry["uri"]
    usb_only = [e["uri"] for e in candidates if e["uri"].startswith("usb://")]
    if len(usb_only) == 1:
        return usb_only[0]
    return None


def _devid_model(devid):
    m = re.search(r"mdl:([^;]+)", devid)
    return m.group(1).strip().lower() if m else None


def ensure_raw_queue(queue_name, match=None, uri=None, raw_ppd=None):
    """Create the hidden raw transport queue. Returns its name or raises."""
    if queue_exists(queue_name):
        return queue_name
    device_uri = uri or resolve_device_uri(match)
    if not device_uri:
        raise RuntimeError("could not find the printer on USB or the network")
    log("creating hidden raw queue %s -> %s" % (queue_name, device_uri))
    result = _run([LPADMIN, "-p", queue_name, "-E", "-v", device_uri, "-m", "raw"])
    if result.returncode != 0 and raw_ppd:
        result = _run([LPADMIN, "-p", queue_name, "-E", "-v", device_uri,
                       "-P", raw_ppd])
    if result.returncode != 0:
        raise RuntimeError("lpadmin failed: %s" % result.stderr.strip())
    _run([LPADMIN, "-p", queue_name, "-o", "printer-is-shared=false"])
    return queue_name
