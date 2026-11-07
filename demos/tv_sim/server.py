"""Loopback ADB peer for the real Apache-2.0 adb-shell client.

This implements a small CNXN/OPEN/OKAY/WRTE/CLSE protocol subset. It is not
Android, an emulator, TV firmware, or proof that a physical TV was controlled.
Only a fixed dumpsys response and configured am-start components are accepted.
Protocol reference: https://android.googlesource.com/platform/packages/modules/adb/+/refs/heads/main/protocol.txt
"""
from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import shlex
import socket
import socketserver
import struct
import threading


APPS = {'home': 'com.android.tv.launcher/.MainActivity',
        'youtube': 'com.google.android.youtube.tv/.MainActivity'}


class SimulatedTV:
    def __init__(self, apps=None):
        self.apps = dict(apps or APPS)
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with self.lock:
            self.current_app = next(iter(self.apps))
            self.fault = 'none'
            self.traces = []

    def set_fault(self, fault):
        if fault not in ('none', 'offline', 'ignore', 'timeout', 'empty'):
            raise ValueError('unknown TV simulator fault')
        with self.lock:
            self.fault = fault

    def set_current_app(self, app):
        if app not in self.apps:
            raise ValueError('unknown simulated app')
        with self.lock:
            self.current_app = app

    def trace(self, **event):
        with self.lock:
            self.traces.append(dict(sequence=len(self.traces) + 1, simulated=True, **event))
            self.traces[:] = self.traces[-100:]

    def snapshot(self):
        with self.lock:
            return {'simulated': True, 'source': 'simulation', 'current_app': self.current_app,
                    'fault': self.fault, 'apps': list(self.apps), 'traces': copy.deepcopy(self.traces)}

    def shell(self, command):
        with self.lock:
            if command == 'dumpsys window windows':
                reply = ('mCurrentFocus=null\n' if self.fault == 'empty' else
                         f'mCurrentFocus=Window{{abc123 u0 {self.apps[self.current_app]}}}\n')
                self.trace(kind='shell', command=command, applied=False)
                return reply
            words = shlex.split(command)
            if len(words) == 4 and words[:3] == ['am', 'start', '-n']:
                app = next((key for key, component in self.apps.items() if component == words[3]), None)
                if app is not None:
                    applied = self.fault != 'ignore'
                    if applied:
                        self.current_app = app
                    self.trace(kind='shell', command=command, applied=applied, app=app)
                    return f'Starting: Intent {{ cmp={words[3]} }}\n'
            self.trace(kind='shell', command=command, applied=False, rejected=True)
            return 'Error: simulator accepts only configured commands\n'


def _read_exact(sock, size):
    data = b''
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError('peer closed')
        data += chunk
    return data


def _read_packet(sock):
    command, arg0, arg1, size, checksum, magic = struct.unpack('<6I', _read_exact(sock, 24))
    if command ^ magic != 0xFFFFFFFF or size > 65536:
        raise ValueError('invalid ADB header')
    payload = _read_exact(sock, size)
    if (sum(payload) & 0xFFFFFFFF) != checksum:
        raise ValueError('invalid ADB checksum')
    return command.to_bytes(4, 'little'), arg0, arg1, payload


def _send_packet(sock, command, arg0, arg1, payload=b''):
    # Independently pack the documented wire header so the simulator does not
    # mock or reuse the client's serializer under test.
    code = int.from_bytes(command, 'little')
    sock.sendall(struct.pack('<6I', code, arg0, arg1, len(payload),
                            sum(payload) & 0xFFFFFFFF, code ^ 0xFFFFFFFF) + payload)


class SimulatorServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_close(self):
        self.stopping.set()
        super().server_close()


def make_server(host='127.0.0.1', port=0, *, apps=None):
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError('TV protocol simulator binds only to loopback')
    device = SimulatedTV(apps)

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            try:
                while not self.server.stopping.is_set():
                    command, arg0, arg1, payload = _read_packet(self.request)
                    device.trace(kind='adb', command=command.decode(), bytes=len(payload))
                    fault = device.snapshot()['fault']
                    if fault == 'offline':
                        return
                    if fault == 'timeout':
                        self.server.stopping.wait(2)
                        return
                    if command == b'CNXN':
                        _send_packet(self.request, b'CNXN', 0x01000000, 4096, b'device::ro.product.name=kevin_sim;\0')
                    elif command == b'OPEN':
                        if not payload.startswith(b'shell:') or not payload.endswith(b'\0'):
                            _send_packet(self.request, b'CLSE', 0, arg0)
                            continue
                        shell = payload[6:-1].decode('utf-8')
                        reply = device.shell(shell).encode()
                        remote_id = arg0 + 100
                        _send_packet(self.request, b'OKAY', remote_id, arg0)
                        _send_packet(self.request, b'WRTE', remote_id, arg0, reply)
                        ack, _, _, _ = _read_packet(self.request)
                        if ack != b'OKAY':
                            return
                        device.trace(kind='adb', command='OKAY', bytes=0)
                        _send_packet(self.request, b'CLSE', remote_id, arg0)
                    elif command == b'CLSE':
                        continue
                    else:
                        return
            except (OSError, ConnectionError, ValueError, UnicodeError):
                return

    server = SimulatorServer((host, port), Handler)
    server.device, server.stopping = device, threading.Event()
    return server


def start_fixture(*, timeout=2):
    """Return (running_server, adapter); caller shuts down/closes the server."""
    from devices.tv import AndroidTVAdapter
    server = make_server()
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()
    adapter = AndroidTVAdapter(*server.server_address, apps=APPS, simulated=True, timeout=timeout)
    return server, adapter


def main():
    from dataclasses import asdict
    parser = argparse.ArgumentParser(description='Loopback Android TV ADB demo')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    if args.demo:
        server, adapter = start_fixture()
        try:
            for plan in ({'action': 'snapshot'}, {'action': 'launch', 'app': 'youtube'}, {'action': 'restore'}):
                print(json.dumps(asdict(adapter.apply(plan, threading.Event())), ensure_ascii=False))
        finally:
            server.shutdown()
            server.server_close()
    else:
        server = make_server(port=args.port)
        print(f'Simulated ADB peer on 127.0.0.1:{server.server_address[1]}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
