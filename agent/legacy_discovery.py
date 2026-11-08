# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Bounded SNMPv1 discovery for printers that Bonjour does not enumerate.

Read-only Host Resources / PWG device identity, as used by CUPS discovery.
No printer status polling, credentials, VM networking, or print data involved.
Protocol references:
https://openprinting.github.io/cups/doc/man-cups-snmp.html
https://github.com/OpenPrinting/cups/blob/master/backend/backend-private.h
"""
import concurrent.futures
import ipaddress
import re
import secrets
import select
import socket
import subprocess
import time

from .ipp.server import log

HR_TYPE = (1, 3, 6, 1, 2, 1, 25, 3, 2, 1, 2)
HR_DESCRIPTION = (1, 3, 6, 1, 2, 1, 25, 3, 2, 1, 3)
PRINTER_TYPE = (1, 3, 6, 1, 2, 1, 25, 3, 1, 5)
DEVICE_ID = (1, 3, 6, 1, 4, 1, 2699, 1, 2, 1, 2, 1, 1, 3, 1)
HP_DEVICE_ID = (1, 3, 6, 1, 4, 1, 11, 2, 3, 9, 1, 1, 7, 0)
MAX_TARGETS = 1024
SCAN_SECONDS = 10


def _tlv(tag, data):
    size = len(data)
    length = bytes([size]) if size < 128 else b'\x82' + size.to_bytes(2, 'big')
    return bytes([tag]) + length + data


def _integer(value):
    data = value.to_bytes(max(1, (value.bit_length() + 8) // 8), 'big', signed=True)
    return _tlv(2, data)


def _base128(value):
    result = [value & 127]
    value >>= 7
    while value:
        result.insert(0, (value & 127) | 128)
        value >>= 7
    return bytes(result)


def _oid(value):
    return _tlv(6, _base128(value[0] * 40 + value[1]) + b''.join(_base128(x) for x in value[2:]))


def request(request_id, oid, next_value=False):
    binding = _tlv(0x30, _oid(oid) + _tlv(5, b''))
    pdu = _tlv(0xa1 if next_value else 0xa0,
               _integer(request_id) + _integer(0) + _integer(0) + _tlv(0x30, binding))
    return _tlv(0x30, _integer(0) + _tlv(4, b'public') + pdu)


class _Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def read(self, expected=None):
        if self.pos + 2 > len(self.data):
            raise ValueError('truncated SNMP field')
        tag, length = self.data[self.pos:self.pos + 2]
        self.pos += 2
        if length & 128:
            count = length & 127
            if not 1 <= count <= 2 or self.pos + count > len(self.data):
                raise ValueError('invalid SNMP length')
            length = int.from_bytes(self.data[self.pos:self.pos + count], 'big')
            self.pos += count
        end = self.pos + length
        if end > len(self.data) or (expected is not None and tag != expected):
            raise ValueError('invalid SNMP field')
        value = self.data[self.pos:end]
        self.pos = end
        return tag, value


def _decode_oid(data):
    values, current = [], 0
    if not data:
        raise ValueError('empty OID')
    for value in data:
        current = (current << 7) | (value & 127)
        if current > 0xffffffff:
            raise ValueError('oversized OID component')
        if not value & 128:
            values.append(current)
            current = 0
    if data[-1] & 128 or not values:
        raise ValueError('truncated OID')
    first = min(values[0] // 40, 2)
    return (first, values[0] - first * 40, *values[1:])


def response(data):
    outer = _Reader(data)
    message = _Reader(outer.read(0x30)[1])
    if message.read(2)[1] != b'\0' or message.read(4)[1] != b'public':
        raise ValueError('unexpected SNMP version/community')
    pdu = _Reader(message.read(0xa2)[1])
    request_id = int.from_bytes(pdu.read(2)[1], 'big', signed=True)
    error = int.from_bytes(pdu.read(2)[1], 'big')
    pdu.read(2)
    if error:
        raise ValueError('SNMP object unavailable')
    bindings = _Reader(pdu.read(0x30)[1])
    binding = _Reader(bindings.read(0x30)[1])
    oid = _decode_oid(binding.read(6)[1])
    tag, value = binding.read()
    if tag == 6:
        value = _decode_oid(value)
    elif tag == 4:
        value = value.decode('utf-8', errors='replace').strip('\0 \r\n')
    else:
        raise ValueError('unsupported identity value')
    return request_id, oid, value


def local_networks(output=None):
    """Read active broadcast-capable IPv4 interfaces; exclude tunnels/loopback."""
    if output is None:
        try:
            output = subprocess.run(['/sbin/ifconfig'], capture_output=True, text=True,
                                    timeout=3, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            return []
    found = []
    for block in re.split(r'\n(?=\S)', output):
        header = block.splitlines()[0] if block else ''
        if 'BROADCAST' not in header or 'UP' not in header or 'POINTOPOINT' in header:
            continue
        if 'status: inactive' in block:
            continue
        name = header.split(':', 1)[0]
        # en* are physical Ethernet/Wi-Fi; bridge0 is the user-facing Thunderbolt bridge.
        if not (re.fullmatch(r'en\d+', name) or name == 'bridge0'):
            continue
        for address, mask in re.findall(r'\binet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+|[0-9.]+)', block):
            try:
                if mask.startswith('0x'):
                    mask = str(ipaddress.IPv4Address(int(mask, 16)))
                iface = ipaddress.IPv4Interface(address + '/' + mask)
                if not (iface.ip.is_loopback or iface.ip.is_unspecified) and iface.network.prefixlen <= 30:
                    found.append((name, iface))
            except ValueError:
                continue
    return found


def scan_targets(networks):
    """Broadcast across each local subnet; bound unicast enumeration on large LANs."""
    own = {interface.ip for _, interface in networks}
    targets = {str(interface.network.broadcast_address) for _, interface in networks}
    for _, interface in networks:
        network = interface.network
        if network.num_addresses > MAX_TARGETS:
            # A bounded nearby /24 covers home LANs without sweeping a corporate /16.
            network = ipaddress.IPv4Network(str(interface.ip) + '/24', strict=False)
        for host in network.hosts():
            if host not in own:
                targets.add(str(host))
            if len(targets) >= MAX_TARGETS:
                return sorted(targets, key=ipaddress.IPv4Address)
    return sorted(targets, key=ipaddress.IPv4Address)


def collect(networks, targets, seconds=SCAN_SECONDS, port=161):
    """Multiplex identity queries with one socket and an overall deadline."""
    found, pending, walks = {}, {}, {}
    allowed = [interface.network for _, interface in networks]
    own = {str(interface.ip) for _, interface in networks}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        def send(host, oid, kind, next_value=False):
            rid = secrets.randbelow(0x7fffffff) + 1
            pending[rid] = (host, oid, kind, next_value)
            try:
                sock.sendto(request(rid, oid, next_value), (host, port))
            except OSError:
                pending.pop(rid, None)
        queue = [(host, oid, kind, nxt) for host in targets
                 for oid, kind, nxt in ((HR_TYPE, 'type', True), (DEVICE_ID, 'id', False))]
        deadline = time.monotonic() + seconds
        next_batch = 0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if queue and now >= next_batch:
                for _ in range(min(32, len(queue))):
                    send(*queue.pop())
                next_batch = now + 0.05
            if not select.select([sock], [], [], min(0.05, max(0, deadline-now)))[0]:
                continue
            try:
                data, sender = sock.recvfrom(8192)
                host, sender_port = sender
                ip = ipaddress.IPv4Address(host)
                if sender_port != port or host in own or not any(ip in net for net in allowed):
                    continue
                rid, oid, value = response(data)
                if rid not in pending:
                    continue
                target, requested, kind, nxt = pending[rid]
                broadcast = any(target == str(net.broadcast_address) for net in allowed)
                if not broadcast and host != target:
                    continue
                if not nxt and oid != requested:
                    continue
                if not broadcast:
                    pending.pop(rid, None)
                if kind == 'type':
                    if oid[:len(HR_TYPE)] != HR_TYPE or oid <= requested:
                        continue
                    if value == PRINTER_TYPE:
                        record = found.setdefault(host, {})
                        if not record.get('printer'):
                            record['printer'] = True
                            send(host, HR_DESCRIPTION + oid[len(HR_TYPE):], 'description')
                            send(host, HP_DEVICE_ID, 'id')
                    elif walks.get(host, 0) < 16:
                        walks[host] = walks.get(host, 0) + 1
                        send(host, oid, 'type', True)
                elif isinstance(value, str) and value:
                    record = found.setdefault(host, {})
                    if kind == 'id':
                        fields = device_fields(value)
                        if (fields.get('MFG') or fields.get('MANUFACTURER')) and (fields.get('MDL') or fields.get('MODEL')):
                            record['device_id'] = value
                            record['printer'] = True
                    else:
                        record[kind] = value
            except (ValueError, OSError, IndexError):
                continue
    return {host: info for host, info in found.items() if info.get('printer')}


def device_fields(value):
    return {key.strip().upper(): val.strip() for part in value.split(';')
            for key, sep, val in [part.partition(':')] if sep}


def identify(host, info):
    """Only present a printer after both identity and a raw transport are verified."""
    fields = device_fields(info.get('device_id', ''))
    make = fields.get('MFG') or fields.get('MANUFACTURER')
    model = fields.get('MDL') or fields.get('MODEL')
    description = info.get('description', '').strip()
    if not make or not model:
        parts = description.split(None, 1)
        if len(parts) != 2:
            return None
        make, model = parts
    if make.lower() == 'hewlett-packard':
        make = 'HP'
    if model.lower().startswith(make.lower() + ' '):
        model = model[len(make) + 1:]
    try:
        with socket.create_connection((host, 9100), timeout=0.7):
            pass  # Never send probe text to a raw printing port.
    except OSError:
        return None
    return dict(transport='jetdirect', uri='socket://%s:9100' % host,
                make=make, model=model, name=make + ' ' + model,
                device_id=info.get('device_id') or 'MFG:%s;MDL:%s;' % (make, model),
                serial=fields.get('SN') or fields.get('SERIALNUMBER'), discovery='snmp',
                address=host)


def discover():
    networks = local_networks()
    targets = scan_targets(networks)
    if not targets:
        return []
    log('legacy discovery: checking %d local IPv4 addresses on %s' %
        (len(targets), ', '.join(name + ':' + str(interface.network) for name, interface in networks)))
    try:
        identities = collect(networks, targets)
    except OSError as exc:
        log('legacy discovery failed: %s' % exc)
        return []
    # Cap candidates as well as address probes to preserve bounded discovery time.
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda item: identify(*item), list(identities.items())[:64]))
    devices = [device for device in results if device]
    log('legacy discovery: %d identified, %d reachable raw printers' % (len(identities), len(devices)))
    return devices
