# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Wire-level legacy discovery, subnet boundaries, and GUI discovery integration."""
import ipaddress
import socket
import threading
import unittest
from unittest.mock import patch, MagicMock

from agent import legacy_discovery as legacy
from agent.discovery import detect, _merge_network
from agent.__main__ import device_to_profile

INTERFACES = '''en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    inet 192.168.0.3 netmask 0xffffff00 broadcast 192.168.0.255
    status: active
en1: flags=8863<UP,BROADCAST,RUNNING> mtu 1500
    inet 10.0.0.2 netmask 0xffffff00 broadcast 10.0.0.255
    status: inactive
utun0: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1500
    inet 10.8.0.2 netmask 0xff000000
bridge100: flags=8863<UP,BROADCAST,RUNNING> mtu 1500
    inet 192.168.127.1 netmask 0xffffff00 broadcast 192.168.127.255
    status: active
'''


def encoded(tag, value):
    """Independent fixture BER encoder: only small definite-length messages."""
    size = bytes([len(value)]) if len(value) < 128 else bytes([0x81, len(value)])
    return bytes([tag]) + size + value


def reply(rid_bytes, oid_bytes, value_tag, value):
    binding = encoded(0x30, encoded(6, oid_bytes) + encoded(value_tag, value))
    pdu = encoded(0xa2, encoded(2, rid_bytes) + b'\x02\x01\x00\x02\x01\x00' + encoded(0x30, binding))
    return encoded(0x30, b'\x02\x01\x00\x04\x06public' + pdu)


class Legacy(unittest.TestCase):
    def test_getnext_matches_independent_wire_fixture(self):
        expected = bytes.fromhex('302802010004067075626c6963a11b0201010201000201003010300e060a2b0601020119030201020500')
        self.assertEqual(legacy.request(1, legacy.HR_TYPE, True), expected)

    def test_response_and_malformed_packet(self):
        packet = reply(b'\x2a', bytes.fromhex('2b06010401950b0102010201010301'),
                       4, b'MFG:Samsung;MDL:ML-2580N;')
        self.assertEqual(legacy.response(packet), (42, legacy.DEVICE_ID, 'MFG:Samsung;MDL:ML-2580N;'))
        for length in range(len(packet)):
            with self.assertRaises(ValueError): legacy.response(packet[:length])
        with self.assertRaises(ValueError): legacy.response(b'\x30\x80')

    def test_local_subnets_exclude_vpn_vm_inactive_and_self(self):
        networks = legacy.local_networks(INTERFACES)
        self.assertEqual([str(i) for _, i in networks], ['192.168.0.3/24'])
        targets = legacy.scan_targets(networks)
        self.assertIn('192.168.0.255', targets)
        self.assertIn('192.168.0.2', targets)
        self.assertNotIn('192.168.0.3', targets)
        self.assertTrue(all(ipaddress.ip_address(t) in networks[0][1].network for t in targets))
        large = legacy.scan_targets([('en0', ipaddress.IPv4Interface('10.2.3.4/8'))])
        self.assertLessEqual(len(large), legacy.MAX_TARGETS)
        self.assertIn('10.255.255.255', large)

    def test_identity_required_and_port_probe_sends_no_bytes(self):
        connection = MagicMock()
        with patch.object(legacy.socket, 'create_connection', return_value=connection):
            self.assertIsNone(legacy.identify('192.168.0.5', {'description': 'Router'}))
            device = legacy.identify('192.168.0.5', {'device_id': 'MFG:Samsung;MDL:ML-2580N;SN:ABC;'})
        connection.sendall.assert_not_called()
        connection.__enter__.return_value.sendall.assert_not_called()
        self.assertEqual(device['model'], 'ML-2580N')
        transport = device_to_profile(device)['transport']
        self.assertEqual({k: transport[k] for k in ('kind', 'host', 'port')},
                         {'kind': 'jetdirect', 'host': '192.168.0.5', 'port': 9100})
        with patch.object(legacy.socket, 'create_connection', side_effect=TimeoutError):
            self.assertIsNone(legacy.identify('192.168.0.5', {'description': 'Samsung ML-2580N'}))

    def test_snmp_printer_appears_when_bonjour_is_empty_or_fails(self):
        printer = dict(uri='socket://192.168.0.5:9100', address='192.168.0.5', make='Samsung', model='ML-2580N', transport='jetdirect')
        with patch('agent.discovery.usb_printers', return_value=[]), \
             patch('agent.discovery.network_printers', return_value=[]), \
             patch('agent.discovery.legacy_discovery.discover', return_value=[printer]):
            self.assertEqual(detect(), [printer])
        with patch('agent.discovery.usb_printers', return_value=[]), \
             patch('agent.discovery.network_printers', side_effect=RuntimeError('Bonjour unavailable')), \
             patch('agent.discovery.legacy_discovery.discover', return_value=[printer]):
            self.assertEqual(detect(), [printer])

    def test_no_network_mode_never_probes_lan(self):
        with patch('agent.discovery.usb_printers', return_value=[]), \
             patch('agent.discovery.legacy_discovery.discover') as probe:
            self.assertEqual(detect(network=False), [])
            probe.assert_not_called()

    def test_dedup_uses_address_not_model(self):
        bonjour = dict(uri='socket://samsung.local:9100', transport='jetdirect', model='ML-2580N')
        same = dict(uri='socket://192.168.0.5:9100', address='192.168.0.5', model='ML-2580N')
        other = dict(uri='socket://192.168.0.6:9100', address='192.168.0.6', model='ML-2580N')
        with patch('agent.discovery._network_addresses', return_value={'192.168.0.5'}):
            self.assertEqual(_merge_network([bonjour], [same, other]), [bonjour, other])

    def test_real_udp_query_response_without_bonjour(self):
        self._udp_identity("device-id")

    def test_host_mib_identity_when_pwg_device_id_is_missing(self):
        self._udp_identity("host-mib")

    def _udp_identity(self, mode):
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(('127.0.0.1', 0)); server.settimeout(0.1)
        port = server.getsockname()[1]
        stop = threading.Event()
        observed = []
        def serve():
            while not stop.is_set():
                try: data, addr = server.recvfrom(4096)
                except socket.timeout: continue
                observed.append(data)
                # Requests here are under 128 bytes. Extract request-id from
                # fixed SNMPv1/public envelope independently of the parser.
                size = data[16]
                rid = data[17:17 + size]
                if mode == 'device-id' and bytes.fromhex('2b06010401950b0102010201010301') in data:
                    packet = reply(rid, bytes.fromhex('2b06010401950b0102010201010301'),
                                   4, b'MFG:Samsung;MDL:ML-2580N;')
                    server.sendto(packet, addr)
                    # Unrelated IDs and damaged packets must not interrupt discovery.
                    server.sendto(reply(b'\x00', bytes.fromhex('2b06010401950b0102010201010301'), 4, b'MFG:Fake;MDL:Wrong;'), addr)
                    server.sendto(b'\x30\x80', addr)
                elif mode == 'host-mib' and bytes.fromhex('2b060102011903020102') in data:
                    server.sendto(reply(rid, bytes.fromhex('2b06010201190302010201'),
                                        6, bytes.fromhex('2b0601020119030105')), addr)
                elif mode == 'host-mib' and bytes.fromhex('2b06010201190302010301') in data:
                    server.sendto(reply(rid, bytes.fromhex('2b06010201190302010301'),
                                        4, b'Samsung ML-2580N'), addr)
        thread = threading.Thread(target=serve); thread.start()
        try:
            result = legacy.collect([('test', ipaddress.IPv4Interface('127.0.0.2/8'))], ['127.0.0.1'], seconds=0.4, port=port)
            if mode == 'device-id':
                self.assertEqual(result['127.0.0.1']['device_id'], 'MFG:Samsung;MDL:ML-2580N;')
            else:
                self.assertEqual(result['127.0.0.1']['description'], 'Samsung ML-2580N')
            self.assertGreaterEqual(len(observed), 2)
        finally:
            stop.set(); thread.join(1); server.close()
