#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Find printers the user might want to add -- USB and network, no terminal.

USB / already-plugged devices come from ``lpinfo -l -v`` (Apple's backends
enumerate them and hand back an IEEE-1284 device-id). Network printers come
from Bonjour via ``dns-sd``: ``_ipp._tcp`` (IPP), ``_pdl-datastream._tcp``
(JetDirect 9100), ``_printer._tcp`` (LPD).

Each result is a dict:
    {transport, uri, make, model, device_id, name, serial}
"""

import re
import concurrent.futures
import ipaddress
import os
import select
import subprocess
import threading
import time
from urllib.parse import unquote, quote, urlsplit

from . import macos, legacy_discovery
from .ipp.server import log

DNS_SD = "/usr/bin/dns-sd"


def _devid_parse(devid):
    fields = {}
    for part in (devid or "").split(";"):
        if ":" in part:
            key, _, value = part.partition(":")
            fields[key.strip().upper()] = value.strip()
    make = fields.get("MFG") or fields.get("MANUFACTURER")
    model = fields.get("MDL") or fields.get("MODEL")
    return make, model, fields


def usb_printers():
    out = []
    for entry in macos.usb_socket_uris():
        uri = entry.get("uri", "")
        if not uri.startswith("usb://"):
            continue
        devid = entry.get("device-id") or ""
        make, model, _ = _devid_parse(devid)
        serial = None
        m = re.search(r"serial=([^&]+)", uri)
        if m:
            serial = m.group(1)
        if not make or not model:
            # usb://Make/Model?serial=...
            m = re.match(r"usb://([^/]+)/([^?]+)", uri)
            if m:
                make = make or unquote(m.group(1))
                model = model or unquote(m.group(2))
        out.append({
            "transport": "usb-raw", "uri": uri,
            "make": make, "model": model, "device_id": devid,
            "name": entry.get("info") or ("%s %s" % (make or "", model or "")).strip(),
            "serial": serial,
        })
    return out


def _dns_sd_output(args, timeout):
    """Read dns-sd continuously through a tty so short scans keep buffered output."""
    master, slave = os.openpty()
    proc = None
    chunks = []
    try:
        proc = subprocess.Popen([DNS_SD] + list(args), stdout=slave, stderr=slave,
                                stdin=subprocess.DEVNULL)
        os.close(slave)
        slave = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if not select.select([master], [], [], max(0, remaining))[0]:
                break
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break  # EIO when the child closes its terminal
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        log("Bonjour command failed: %s" % exc)
    finally:
        if slave is not None:
            os.close(slave)
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        os.close(master)
    output = b"".join(chunks).decode("utf-8", errors="replace")
    if "failed" in output.lower():
        log("Bonjour: %s" % output.strip())
    return output


def _dns_sd_browse(service, timeout=3):
    """Return instance names for a service type via a bounded dns-sd -B."""
    out = _dns_sd_output(["-B", service, "local"], timeout)
    names = []
    for line in out.splitlines():
        if " Add " not in line and not line.strip().startswith("Add"):
            continue
        # ... Add <flags> <if> local. <service>. <instance name>
        idx = line.rfind(service)
        if idx < 0:
            continue
        inst = line[idx + len(service):].lstrip(". \t").rstrip()
        if inst:
            names.append(inst)
    return list(dict.fromkeys(names))


def _dns_sd_resolve(instance, service, timeout=3):
    out = _dns_sd_output(["-L", instance, service, "local"], timeout)
    return _parse_dns_sd_resolve(out)


def _parse_dns_sd_resolve(out):
    """dns-sd escapes spaces in each TXT item; split before unescaping."""
    info = {}
    host_port = re.search(r"can be reached at (\S+):(\d+)", out)
    if host_port:
        info["host"] = host_port.group(1).rstrip(".")
        info["port"] = int(host_port.group(2))
    for line in out.splitlines():
        if "can be reached at" in line:
            continue
        for token in re.findall(r"(?:\\[0-9]{3}|\\.|[^\s\\])+", line):
            key, sep, value = token.partition("=")
            if sep:
                info[key] = re.sub(
                    r"\\([0-9]{3}|.)",
                    lambda m: chr(int(m.group(1))) if m.group(1).isdigit()
                    else m.group(1), value)
    return info


def network_printers(timeout=3):
    found = {}
    services = [
        ("_ipp._tcp", "ipp"),
        ("_ipps._tcp", "ipps"),
        ("_pdl-datastream._tcp", "jetdirect"),
        ("_printer._tcp", "lpd"),
    ]
    for service, kind in services:
        for instance in _dns_sd_browse(service, timeout):
            info = _dns_sd_resolve(instance, service, timeout)
            host = info.get("host")
            if not host:
                continue
            key = (host.lower(), instance)
            ty = info.get("ty") or info.get("product", "").strip("()")
            make = info.get("usb_MFG") or info.get("mfg")
            model = info.get("usb_MDL") or info.get("mdl")
            if (not make or not model) and ty:
                bits = ty.split(None, 1)
                make = make or (bits[0] if bits else None)
                model = model or (bits[1] if len(bits) > 1 else ty)
            if kind in ("ipp", "ipps"):
                uri = "%s://%s:%d/%s" % (
                    "ipp" if kind == "ipp" else "ipps", host,
                    info.get("port", 631), info.get("rp", "ipp/print"))
                transport = "ipp"
            elif kind == "jetdirect":
                uri = "socket://%s:%d" % (host, info.get("port", 9100))
                transport = "jetdirect"
            else:
                uri = "lpd://%s/%s" % (host, quote(info.get("rp", ""), safe="/"))
                transport = "network-raw"
            pdl = info.get("pdl") or ""
            device_id = "MFG:%s;MDL:%s;" % (make or "", model or "")
            if pdl:
                device_id += "CMD:%s;" % pdl
            # One row per Bonjour printer, prefer its advertised raw socket
            # for driver-based queues; remember the IPP endpoint for driverless.
            if key in found:
                existing = found[key]
                existing["pdl"] = existing.get("pdl") or pdl
                if kind in ("ipp", "ipps") and not existing.get("ipp_uri"):
                    existing["ipp_uri"] = uri
                if kind != "jetdirect":
                    continue
                existing.update({"transport": transport, "uri": uri})
                continue
            found[key] = {
                "transport": transport, "uri": uri,
                "ipp_uri": uri if kind in ("ipp", "ipps") else None,
                "make": make, "model": model,
                "device_id": device_id, "pdl": pdl,
                "name": ty or instance, "serial": None,
                "bonjour": instance, "service": service,
            }
    return list(found.values())


def resolve_host(host):
    """One IPv4 address for a printer host name (``.local`` included), or None.
    The engine has no mDNS, so it gets addresses, never Bonjour names."""
    if not host:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        result = subprocess.run(["/usr/bin/dscacheutil", "-q", "host", "-a", "name", host],
                                capture_output=True, text=True, timeout=3)
        found = re.findall(r"ip_address: (\S+)", result.stdout)
        if found:
            return found[0]
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc = subprocess.Popen([DNS_SD, "-G", "v4", host], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        time.sleep(2.5)
        proc.kill()
        found = re.findall(r"\s(\d+\.\d+\.\d+\.\d+)\s", proc.stdout.read())
        if found:
            return found[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _network_addresses(device):
    host = urlsplit(device.get("uri", "")).hostname
    address = resolve_host(host)
    return {address} if address else set()


def _merge_network(printers, legacy):
    # Do not merge identical model names: there may be several physical printers.
    addresses = set()
    for device in printers:
        if device.get("transport") != "usb-raw":
            addresses.update(_network_addresses(device))
    for device in legacy:
        if device.get("address") not in addresses:
            printers.append(device)
            addresses.add(device.get("address"))
    return printers


def detect(network=True, timeout=3):
    if not network:
        return usb_printers()
    # Independent host-side probes: an absent Bonjour advertisement must not
    # prevent a legacy printer from being found or require booting the VM.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        scans = [pool.submit(usb_printers), pool.submit(network_printers, timeout),
                 pool.submit(legacy_discovery.discover)]
        results = []
        for source, scan in zip(("USB", "Bonjour", "legacy"), scans):
            try:
                results.append(scan.result())
            except Exception as exc:
                log("%s discovery failed: %s" % (source, exc))
                results.append([])
    return _merge_network(results[0] + results[1], results[2])


class DeviceCache(object):
    """Keep the detected-device list fresh on a background thread.

    A full scan costs seconds -- `lpinfo -v` alone is ~15 s on some Macs no
    matter which schemes you exclude, because macOS runs every backend before
    filtering. Nothing user-facing may block on that, so callers read the last
    known list and a scan happens behind them.
    """

    def __init__(self, network=True, interval=30):
        self.network = network
        self.interval = interval
        self._lock = threading.Lock()
        self._devices = []
        self._scanned_at = 0.0
        self._scanning = False
        self._generation = 0
        self._wake = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="discovery",
                                        daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop = True
        self._wake.set()

    def snapshot(self):
        """(devices, scanned_at, scanning) -- never blocks."""
        with self._lock:
            return list(self._devices), self._scanned_at, self._scanning

    def devices(self):
        return self.snapshot()[0]

    def request_refresh(self):
        with self._lock:
            self._generation += 1
            self._devices = []
            self._scanned_at = 0.0
            self._scanning = True
            self._wake.set()

    def _loop(self):
        while not self._stop:
            with self._lock:
                generation = self._generation
                self._wake.clear()
                self._scanning = True
            try:
                found = detect(network=self.network)
            except Exception as exc:  # noqa: BLE001
                log("discovery scan failed: %s" % exc)
                found = None
            with self._lock:
                if generation != self._generation:
                    continue  # A refresh superseded this scan; do not restore stale devices.
                if found is not None:
                    self._devices = found
                    self._scanned_at = time.time()
                self._scanning = False
            self._wake.wait(self.interval)


# ---------------------------------------------------------------------------
def match_profiles(profiles, devices):
    """Yield (device, profile) for every detected device a profile claims."""
    pairs = []
    for device in devices:
        did = (device.get("device_id") or "").lower()
        for profile in profiles.values():
            hinted = (profile["match"].get("device_id") or "").lower()
            if hinted and hinted in did:
                pairs.append((device, profile))
                continue
            for rule in profile["match"]["usb"]:
                v = str(rule.get("vendor", "")).lower().replace("0x", "")
                p = str(rule.get("product", "")).lower().replace("0x", "")
                if v and p and v in device.get("uri", "").lower() \
                        and p in device.get("uri", "").lower():
                    pairs.append((device, profile))
    return pairs


def report(profiles, runtime=None, network=True):
    devices = detect(network=network)
    matched = match_profiles(profiles, devices)
    matched_keys = {id(d) for d, _ in matched}
    lines = ["Detected printers:"]
    if not devices:
        lines.append("  (none -- attach a USB printer or check the network)")
    for device in devices:
        tag = ""
        for d, profile in matched:
            if d is device:
                tag = "  [profile: %s]" % profile["id"]
        lines.append("  %-11s %s" % (device["transport"], device["name"] or "?"))
        lines.append("              %s" % device["uri"])
        if device.get("device_id"):
            lines.append("              %s" % device["device_id"].strip())
        if runtime is not None:
            try:
                from .engine import Engine
                candidates = Engine(runtime).rank_drivers(device)
                if candidates:
                    for candidate in candidates[:3]:
                        lines.append("              -> %s  (%s)"
                                     % (candidate.get("driver"), candidate.get("ppd_name")))
                else:
                    lines.append("              -> no driver available")
            except Exception as exc:  # noqa: BLE001
                lines.append("              -> driver lookup failed: %s" % exc)
        lines.append(tag.strip() and ("           %s" % tag.strip()) or "")
    lines = [ln for ln in lines if ln != ""]
    lines.append("")
    lines.append("Profiles on file: %s" % (", ".join(sorted(profiles)) or "(none)"))
    return "\n".join(lines)
