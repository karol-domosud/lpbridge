# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Regression coverage for actual dns-sd formatting and endpoint selection."""
import unittest
from unittest.mock import patch

from agent.discovery import _parse_dns_sd_resolve, network_printers, usb_printers
from agent.__main__ import device_to_profile


class Discovery(unittest.TestCase):
    def test_txt_fields_do_not_consume_next_key_or_keep_escapes(self):
        info = _parse_dns_sd_resolve(
            'Printer can be reached at samsung.local.:9100 (interface 14)\n'
            r' ty=Samsung\ ML-2580N product=\(Samsung\ ML-2580N\) '
            r'usb_MFG=Samsung usb_MDL=ML-2580N note=Office\032Laser rp=auto' + '\n')
        self.assertEqual(info['ty'], 'Samsung ML-2580N')
        self.assertEqual(info['product'], '(Samsung ML-2580N)')
        self.assertEqual(info['note'], 'Office Laser')
        self.assertEqual(info['usb_MDL'], 'ML-2580N')
        self.assertEqual(info['host'], 'samsung.local')

    def test_protocol_duplicates_prefer_advertised_socket(self):
        def resolve(instance, service, timeout):
            return _parse_dns_sd_resolve(
                'Printer can be reached at samsung.local.:%d\n' %
                (9100 if service == '_pdl-datastream._tcp' else 631) +
                r' ty=Samsung\ ML-2580N product=\(Samsung\ ML-2580N\) rp=auto')
        with patch('agent.discovery._dns_sd_browse', return_value=['Samsung']), \
             patch('agent.discovery._dns_sd_resolve', side_effect=resolve):
            devices = network_printers(0)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]['model'], 'ML-2580N')
        transport = device_to_profile(devices[0])['transport']
        self.assertEqual({k: transport[k] for k in ('kind', 'host', 'port')},
                         {'kind': 'jetdirect', 'host': 'samsung.local', 'port': 9100})

    def test_ipp_and_lpd_keep_exact_network_uri(self):
        for transport, uri in [('driverless-candidate', 'ipp://laser.local:631/ipp/print'),
                               ('network-raw', 'lpd://laser.local/queue')]:
            profile = device_to_profile(dict(transport=transport, uri=uri))
            self.assertEqual(profile['transport']['uri'], uri)

    def test_usb_percent_encoding_is_decoded_before_lookup(self):
        uri = 'usb://Samsung/ML%2D2580N?serial=A'
        with patch('agent.discovery.macos.usb_socket_uris', return_value=[{'uri': uri}]):
            device = usb_printers()[0]
        self.assertEqual(device['model'], 'ML-2580N')
        self.assertEqual(device_to_profile(device)['transport']['uri'], uri)

    def test_short_scan_keeps_line_buffered_output(self):
        import sys
        from agent.discovery import _dns_sd_output
        with patch('agent.discovery.DNS_SD', sys.executable):
            output = _dns_sd_output(['-c', 'import time; print("Bonjour fixture"); time.sleep(5)'], 0.3)
        self.assertIn('Bonjour fixture', output)

    def test_refresh_discards_in_progress_results_from_previous_network(self):
        import threading
        from agent.discovery import DeviceCache
        started, release, rescanned = threading.Event(), threading.Event(), threading.Event()
        cache = DeviceCache(interval=1000)
        def scan(**_):
            if not started.is_set():
                started.set()
                release.wait(2)
                return [{'name': 'old-network-printer'}]
            rescanned.set()
            return []
        with patch('agent.discovery.detect', side_effect=scan):
            cache._devices = [{'name': 'cached-old-printer'}]
            cache.start()
            try:
                self.assertTrue(started.wait(1))
                cache.request_refresh()
                self.assertEqual(cache.devices(), [])
                release.set()
                self.assertTrue(rescanned.wait(1))
                self.assertEqual(cache.devices(), [])
            finally:
                release.set()
                cache.stop()
                cache._thread.join(2)
