# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""App setup contracts: remembered choices, exact targeting and idle discovery."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent.__main__ import device_to_profile
from agent.selections import Selections
from agent.control import ControlAPI
from agent.manager import PrinterManager
from agent.ipp.server import PrinterRegistry
from tests.test_multiprinter import CAPS


class Setup(unittest.TestCase):
    def test_identical_models_have_distinct_stable_ids(self):
        a = dict(make="Samsung", model="ML-2580N", name="Samsung", transport="usb-raw", uri="usb://Samsung/ML-2580N?serial=A")
        b = dict(a, uri="usb://Samsung/ML-2580N?serial=B")
        self.assertEqual(device_to_profile(a)["id"], device_to_profile(dict(a))["id"])
        self.assertNotEqual(device_to_profile(a)["id"], device_to_profile(b)["id"])

    def test_selection_survives_restart_and_atomic_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "printers.json")
            store = Selections(path)
            store.update("a", {"id": "a"})
            store.update("b", {"id": "b"})
            self.assertEqual(set(Selections(path).read()), {"a", "b"})
            with patch("agent.selections.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError): store.update("c", {"id": "c"})
            self.assertEqual(set(store.read()), {"a", "b"})
            store.update("a")
            self.assertEqual(set(Selections(path).read()), {"b"})
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_discovery_does_not_wait_for_driver_or_repeatedly_wake_vm(self):
        started, finish = threading.Event(), threading.Event()
        def rank(_device):
            started.set(); finish.wait(2)
            return [{"ppd_name": "drv:///splix-samsung.drv/ml2580n.ppd", "driver": "splix",
                     "make_and_model": "Samsung ML-2580N", "source": "device-id"}]
        engine = SimpleNamespace(rank_drivers=Mock(side_effect=rank), runtime=Mock())
        lifecycle = Mock()
        manager = SimpleNamespace(engine=engine, lifecycle=lifecycle, ids=lambda: [])
        devices = Mock()
        devices.devices.return_value = [{"make": "Samsung", "model": "ML-2580N", "transport": "usb-raw", "uri": "usb://test"}]
        ui = ControlAPI(manager, device_to_profile, devices)
        self.assertIsNone(ui.discover()[0]["available"])
        self.assertTrue(started.wait(1))
        finish.set()
        for thread in threading.enumerate():
            if thread.name == "driver-hint": thread.join(2)
        for _ in range(3):
            ui._discover_cache = (0, [])
            self.assertTrue(ui.discover()[0]["available"])
        self.assertEqual(engine.rank_drivers.call_count, 1)
        lifecycle.job_started.assert_called_once()
        lifecycle.job_finished.assert_called_once()

    def test_queue_failure_never_reports_success_or_remembers_printer(self):
        options = SimpleNamespace(refresh=False, transport="dryrun", output_dir=None,
                                  keep_spool=False, boot_timeout=1, port=8631, advertise=False, no_auto_queue=False)
        runtime = Mock(); lifecycle = Mock(); registry = PrinterRegistry()
        manager = PrinterManager(runtime, lifecycle, registry, options)
        manager.selections = Mock()
        profile = device_to_profile({"make": "Test", "model": "Laser", "transport": "usb-raw", "uri": "usb://test"})
        with patch("agent.manager.provision.resolve_driver", return_value={"ppd_name": "x", "candidates": []}), \
             patch("agent.manager.provision.ensure_queue", return_value="/fixture.ppd"), \
             patch("agent.manager.provision.capabilities", return_value=CAPS), \
             patch.object(manager, "_build_transport", return_value=Mock()), \
             patch("agent.manager.Pipeline"), \
             patch("agent.manager.macos.ensure_everywhere_queue", return_value=False):
            ok, message = manager.add(profile)
        self.assertFalse(ok)
        self.assertIn("macOS", message)
        self.assertEqual(registry.ids(), [])
        manager.selections.update.assert_not_called()
        lifecycle.job_finished.assert_called_once()

    def test_managed_sync_does_not_add_unselected_printers(self):
        manager = PrinterManager(Mock(), Mock(), PrinterRegistry(), Mock())
        manager.add = Mock()
        manager.sync([{"model": "Unexpected printer"}], device_to_profile, auto_add=False)
        manager.add.assert_not_called()

    def test_add_uses_exact_id_not_ambiguous_model_substring(self):
        manager = SimpleNamespace(add=Mock(return_value=(True, "ok")))
        devices = Mock()
        devices.devices.return_value = [dict(make="Test", model="Printer", uri="usb://one"), dict(make="Test", model="Printer", uri="usb://two")]
        ui = ControlAPI(manager, device_to_profile, devices)
        handler = Mock(path="/api/printers", headers={"X-lpbridge": "1"})
        with patch.object(ui, "_body", return_value={"device": "Printer"}), patch.object(ui, "_json") as response:
            ui.handle_api(handler, "POST")
            manager.add.assert_not_called()
            self.assertEqual(response.call_args.args[-1], 404)
        pid = device_to_profile(devices.devices.return_value[1])["id"]
        with patch.object(ui, "_body", return_value={"device": pid}), patch.object(ui, "_json"):
            ui.handle_api(handler, "POST")
        self.assertEqual(manager.add.call_args.args[0]["id"], pid)

    def test_finished_empty_scan_is_visible_without_api_cache_delay(self):
        devices = Mock()
        devices.devices.return_value = [dict(make='Test', model='Printer', uri='usb://one')]
        manager = SimpleNamespace(ids=lambda: [])
        ui = ControlAPI(manager, device_to_profile, devices)
        ui._driver_hints[('Test', 'Printer', None)] = {'available': False}
        self.assertEqual(len(ui.discover()), 1)
        devices.devices.return_value = []
        self.assertEqual(ui.discover(), [])
