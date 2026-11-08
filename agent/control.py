#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""The agent's control API: what the setup app and the menubar app talk to.

Served off the same loopback port as IPP (invariant 5 -- 127.0.0.1 only, no
auth, no TLS). JSON in, JSON out; there is no HTML here on purpose -- the
native app is the front end, and one front end is enough to keep in sync.

    GET    /                       plain-text pointer for anyone who opens the port in a browser
    GET    /api/printers           what we are serving, with live job state
    GET    /api/discover           printers we can see + the driver each needs
    GET    /api/log?n=200          recent agent log lines
    GET    /api/health             build, runtime, printer count
    POST   /api/printers           {"device": "<id>", "ppd": "<optional ppd-name>"}  -> add
    POST   /api/printers/<id>/identify
    POST   /api/drivers            raw .ppd/.ppd.gz/.deb body + X-lpbridge-filename header
    POST   /api/shell              ask the user (dialog), boot the engine, hold it, return how to ssh in; 403 if refused
    POST   /api/shell/<token>/renew
    DELETE /api/shell/<token>
    DELETE /api/printers/<id>

Mutating calls require the header ``X-lpbridge: 1``. A web page on another
origin cannot set a custom header without a CORS preflight (which we never
answer), so that is enough to stop a random site from poking the agent.
"""

import json
import threading

from . import discovery, provision, shell
from .build_id import BUILD_ID, STARTED_AT
from .ipp.server import log, recent_log

class ControlAPI(object):
    def __init__(self, manager, device_to_profile, devices, network=True):
        self.manager = manager
        self.device_to_profile = device_to_profile
        self.devices = devices          # a discovery.DeviceCache
        self.network = network
        self._driver_hints = {}
        self._driver_pending = set()
        self._driver_lock = threading.Lock()

    def _resolve_hint(self, key, device):
        lifecycle = getattr(self.manager, "lifecycle", None)
        if lifecycle:
            lifecycle.job_started(setup=True)
        try:
            rec = provision.lookup_driver(self.manager.engine, device, install_vendor=False)
        except Exception as exc:
            rec = {"available": False, "note": "Driver lookup failed: %s" % exc}
        finally:
            if lifecycle:
                lifecycle.job_finished()
        with self._driver_lock:
            self._driver_hints[key] = rec
            self._driver_pending.discard(key)

    # -- plumbing ---------------------------------------------------
    @staticmethod
    def _send(handler, code, body, content_type="application/json"):
        if not isinstance(body, bytes):
            body = body.encode("utf-8")
        handler.send_response(code)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)

    def _json(self, handler, obj, code=200):
        self._send(handler, code, json.dumps(obj, default=str))

    @staticmethod
    def _guarded(handler):
        if handler.headers.get("X-lpbridge") != "1":
            ControlAPI._send(handler, 403,
                        json.dumps({"error": "missing X-lpbridge header"}))
            return False
        return True

    @staticmethod
    def _body(handler):
        length = int(handler.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(handler.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    # -- discovery (never blocks; a scan takes ~15s in the background) ----
    def discover(self, force=False):
        if force:
            self.devices.request_refresh()
            with self._driver_lock:
                self._driver_hints.clear()
        devices = self.devices.devices()
        serving = set(self.manager.ids())
        out = []
        for device in devices:
            profile = self.device_to_profile(device)
            entry = {
                "id": profile["id"],
                "name": device.get("name") or profile["name"],
                "make": device.get("make"),
                "model": device.get("model"),
                "uri": device.get("uri"),
                "transport": device.get("transport"),
                "device_id": device.get("device_id"),
                "serving": profile["id"] in serving,
            }
            key = (device.get("make"), device.get("model"), device.get("device_id"))
            with self._driver_lock:
                rec = self._driver_hints.get(key)
                if rec is None and key not in self._driver_pending:
                    self._driver_pending.add(key)
                    threading.Thread(target=self._resolve_hint, args=(key, device),
                                     daemon=True, name="driver-hint").start()
                entry.update({k: (rec or {}).get(k) for k in
                              ("driver", "ppd_name", "available", "note", "source", "candidates")})
                entry["pdl"] = device.get("pdl")
            out.append(entry)
        return out

    # -- routing ---------------------------------------------------
    def handle_get(self, handler):
        path = handler.path.split("?", 1)[0].rstrip("/") or "/"
        query = handler.path.split("?", 1)[1] if "?" in handler.path else ""

        if path == "/":
            self._send(handler, 200, "lpbridge agent \u2014 control API; use the lpbridge app\n",
                       "text/plain; charset=utf-8")
            return
        if path == "/api/printers":
            self._json(handler, {"printers": self.manager.status()})
            return
        if path == "/api/discover":
            force = "force=1" in query
            found = self.discover(force=force)
            _, scanned_at, scanning = self.devices.snapshot()
            self._json(handler, {"devices": found, "scanning": scanning,
                                 "scanned_at": scanned_at})
            return
        if path == "/api/log":
            n = 200
            for part in query.split("&"):
                if part.startswith("n="):
                    try:
                        n = max(1, min(500, int(part[2:])))
                    except ValueError:
                        pass
            self._json(handler, {"lines": recent_log(n)})
            return
        if path == "/api/health":
            self._json(handler, {"ok": True, "printers": len(self.manager.ids()),
                                 "app_protocol": 1,
                                 "service_build": BUILD_ID,
                                 "started_at": STARTED_AT,
                                 "managed": bool(getattr(self.manager, "selections", None)),
                                 "runtime": self.manager.runtime.name})
            return
        self._send(handler, 404, json.dumps({"error": "not found"}))

    def handle_api(self, handler, method):
        path = handler.path.split("?", 1)[0].rstrip("/")
        if not self._guarded(handler):
            return

        if method == "POST" and path == "/api/printers":
            body = self._body(handler)
            token = str(body.get("device") or "")
            if not token:
                self._json(handler, {"error": "device required"}, 400)
                return
            for device in self.devices.devices():
                profile = self.device_to_profile(device)
                if token in (device.get("uri"), profile["id"]):
                    ppd_name = body.get("ppd") or None
                    try:
                        ok, message = self.manager.add(profile, ppd_name=ppd_name)
                    except Exception as exc:
                        ok, message = False, str(exc)
                    self._json(handler, {"ok": ok, "message": message},
                               200 if ok else 400)
                    return
            self._json(handler, {"error": "no matching printer"}, 404)
            return

        if method == "POST" and path == "/api/drivers":
            # raw file body; the name comes in a header (no multipart parsing)
            name = handler.headers.get("X-lpbridge-filename") or "driver"
            try:
                length = int(handler.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 256 * 1024 * 1024:
                self._json(handler, {"error": "file missing or larger than 256 MB"}, 400)
                return
            data = handler.rfile.read(length)
            try:
                kind, detail = self.manager.add_user_driver(name, data)
            except Exception as exc:  # noqa: BLE001
                log("driver file %s rejected: %s" % (name, exc))
                self._json(handler, {"ok": False, "error": str(exc)}, 400)
                return
            log("driver file %s installed (%s)" % (name, kind))
            with self._driver_lock:
                self._driver_hints.clear()
            self._json(handler, {"ok": True, "kind": kind, "detail": detail})
            return

        if method == "POST" and path == "/api/shell":
            try:
                info = self.manager.shells.open()
            except shell.ShellRefused as exc:
                self._json(handler, {"ok": False, "error": str(exc)}, 403)
                return
            except Exception as exc:  # noqa: BLE001
                self._json(handler, {"ok": False, "error": "could not open a shell: %s" % exc}, 500)
                return
            self._json(handler, dict(info, ok=True))
            return
        if method == "POST" and path.startswith("/api/shell/") and path.endswith("/renew"):
            token = path[len("/api/shell/"):-len("/renew")]
            self._json(handler, {"ok": self.manager.shells.renew(token)})
            return
        if method == "DELETE" and path.startswith("/api/shell/"):
            token = path[len("/api/shell/"):]
            self._json(handler, {"ok": self.manager.shells.close(token)})
            return

        if method == "POST" and path.endswith("/identify"):
            printer_id = path[len("/api/printers/"):-len("/identify")]
            entry = self.manager.entry(printer_id)
            if not entry:
                self._json(handler, {"error": "not found"}, 404)
                return
            log("Identify requested for '%s'" % entry["profile"]["name"])
            self._json(handler, {"ok": True})
            return

        if method == "DELETE" and path.startswith("/api/printers/"):
            printer_id = path[len("/api/printers/"):]
            ok, message = self.manager.remove(printer_id)
            self._json(handler, {"ok": ok, "message": message},
                       200 if ok else 404)
            return

        self._json(handler, {"error": "not found"}, 404)
