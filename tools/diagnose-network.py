#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

"""Offline, bounded printer-discovery diagnostics. No settings or queues changed."""
import argparse
import concurrent.futures
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ('_ipp._tcp', '_ipps._tcp', '_pdl-datastream._tcp', '_printer._tcp')


def capture(args, timeout=10):
    start = time.monotonic()
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        return {'command': args, 'error': str(exc)}
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
    return dict(command=args, stdout=stdout, stderr=stderr, returncode=proc.returncode,
                timed_out=timed_out, seconds=round(time.monotonic()-start, 2))


def api(path):
    # Ignore proxy settings: the service is always reached over loopback.
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:8631' + path, timeout=3) as response:
            return json.load(response)
    except Exception as exc:
        return {'error': str(exc)}


def api_samples():
    samples = []
    started = time.monotonic()
    for i in range(20):
        samples.append({'elapsed_seconds': round(time.monotonic() - started, 2), 'health': api('/api/health'),
                        'discovery': api('/api/discover' + ('?force=1' if i == 0 else ''))})
        if i != 19:
            time.sleep(5)
    return {'samples': samples, 'recent_agent_log': api('/api/log?n=120')}


def bonjour(service):
    sys.path.insert(0, str(ROOT))
    from agent.discovery import _parse_dns_sd_resolve, _dns_sd_output
    browse = {'stdout': _dns_sd_output(['-B', service, 'local'], 12),
              'capture': 'terminal, continuous read', 'seconds': 12}
    instances = []
    for line in browse.get('stdout', '').splitlines():
        if ' Add ' in line and service in line:
            name = line.split(service, 1)[1].lstrip('. \t').rstrip()
            if name and name not in instances:
                instances.append(name)
    resolved = []
    for instance in instances[:12]:
        raw = {'stdout': _dns_sd_output(['-L', instance, service, 'local'], 5),
               'capture': 'terminal, continuous read', 'seconds': 5}
        resolved.append({'instance': instance, 'raw': raw,
                         'parsed': _parse_dns_sd_resolve(raw.get('stdout', ''))})
    return {'browse': browse, 'resolved': resolved,
            'truncated': len(instances) > 12}


def versions():
    roots = [ROOT, ROOT/'build/lpbridge.app/Contents/Resources',
             Path('/Applications/lpbridge.app/Contents/Resources'),
             Path.home()/'Applications/lpbridge.app/Contents/Resources']
    result = {}
    for root in roots:
        files = {}
        for relative in ('agent/discovery.py', 'agent/legacy_discovery.py', 'agent/control.py', 'agent/runtime/lifecycle.py'):
            path = root/relative
            if path.is_file():
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        if files:
            result[str(root)] = files
    plist = Path.home()/'Library/LaunchAgents/com.lpbridge.app.agent.plist'
    if plist.exists():
        try:
            data = plistlib.loads(plist.read_bytes())
            result['launch_agent'] = {k: data.get(k) for k in
                                     ('ProgramArguments', 'WorkingDirectory', 'StandardErrorPath')}
        except Exception as exc:
            result['launch_agent_error'] = str(exc)
    process = capture(['/bin/ps', '-axo', 'pid,lstart,command'])
    result['processes'] = [line for line in process.get('stdout', '').splitlines()
                           if '/Contents/MacOS/lpbridge' in line or ' -m agent' in line]
    return result


def probe(address):
    result = {}
    for port in (9100, 631, 515):
        try:
            with socket.create_connection((address, port), timeout=3):
                result[str(port)] = 'connected (no bytes sent)'
        except OSError as exc:
            result[str(port)] = str(exc)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--printer-ip', type=ipaddress.ip_address)
    args = parser.parse_args()
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    directory = ROOT/'build/diagnostics'/stamp
    directory.mkdir(parents=True, exist_ok=True)
    report = {'started': stamp, 'versions': versions(), 'printer_ip': str(args.printer_ip or '')}
    tasks = {
        'api_samples': api_samples,
        'interfaces': lambda: capture(['/sbin/ifconfig']),
        'routes': lambda: capture(['/usr/sbin/netstat', '-rn']),
        'network_state': lambda: capture(['/usr/sbin/scutil', '--nwi']),
        'hardware_ports': lambda: capture(['/usr/sbin/networksetup', '-listallhardwareports']),
        'cups_discovery': lambda: capture(['/usr/sbin/lpinfo', '-l', '-v'], 25),
        'current_code_scan': lambda: capture([sys.executable, '-c',
            'import sys,json; sys.path.insert(0, '+repr(str(ROOT))+'); '
            'from agent.discovery import detect; print(json.dumps(detect(timeout=3)))'], 120),
    }
    for service in SERVICES:
        tasks[service] = lambda service=service: bonjour(service)
    # The app now discovers legacy identities automatically; record the exact
    # connected ranges even when no device responds, without asking for an IP.
    sys.path.insert(0, str(ROOT))
    from agent.legacy_discovery import local_networks, scan_targets
    networks = local_networks()
    report['legacy_scan_scope'] = {'interfaces': [(name, str(interface)) for name, interface in networks],
                                   'target_count': len(scan_targets(networks))}
    if args.printer_ip:
        tasks['printer_ports'] = lambda: probe(str(args.printer_ip))
        tasks['printer_route'] = lambda: capture(['/sbin/route', '-n', 'get', str(args.printer_ip)])
    print('Collecting local diagnostics for about two minutes. Keep Debug connected.', flush=True)
    print('No internet needed; no print jobs or settings changes.', flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        pending = {pool.submit(fn): name for name, fn in tasks.items()}
        for future in concurrent.futures.as_completed(pending):
            name = pending[future]
            try:
                report[name] = future.result()
            except Exception as exc:
                report[name] = {'error': str(exc)}
            (directory/'report.json').write_text(json.dumps(report, indent=2))
            print('Collected: '+name, flush=True)
    print('\nDone. Reconnect to the internet and open an issue with this report:\n'+str(directory/'report.json'), flush=True)


if __name__ == '__main__':
    main()
