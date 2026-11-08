# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Durable, atomic storage of the printers chosen in the app."""
import json
import os
import tempfile
import threading


class Selections:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def read(self):
        try:
            with open(self.path) as source:
                return json.load(source)
        except FileNotFoundError:
            return {}

    def update(self, printer_id, profile=None):
        with self.lock:
            data = self.read()
            if profile is None:
                data.pop(printer_id, None)
            else:
                data[printer_id] = profile
            parent = os.path.dirname(self.path)
            os.makedirs(parent, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=parent, prefix=".printers-")
            try:
                with os.fdopen(fd, "w") as target:
                    json.dump(data, target, indent=2)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
