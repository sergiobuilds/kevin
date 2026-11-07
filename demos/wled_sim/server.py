"""Small local WLED API peer for the real python-wled client.

This is a protocol simulator, not WLED firmware or evidence of device support.
Implemented subset: https://kno.wled.ge/interfaces/json-api/
"""
from __future__ import annotations

import argparse
import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _initial_state():
    return {'on': True, 'bri': 255, 'transition': 0, 'ps': -1, 'pl': -1,
            'mainseg': 0, 'nl': {}, 'udpn': {}, 'lor': 0,
            'seg': [
                {'id': i, 'start': i * 30, 'stop': (i + 1) * 30, 'n': name,
                 'on': True, 'bri': brightness, 'col': [color, [0, 0, 0], [0, 0, 0]],
                 'fx': 0, 'sx': 128, 'ix': 128, 'pal': 0, 'sel': i == 0, 'rev': False}
                for i, name, brightness, color in (
                    (0, 'Entrance', 24, [255, 185, 95]),
                    (1, 'Display', 128, [80, 180, 255]),
                    (2, 'Lounge', 180, [220, 110, 210]))]}


class SimulatedWLED:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with self.lock:
            self.state = _initial_state()
            self.fault = 'none'
            self._traces = []
            self._sequence = 0

    def set_fault(self, fault):
        if fault not in ('none', 'ignore', 'timeout'):
            raise ValueError('fault must be none, ignore or timeout')
        with self.lock:
            self.fault = fault

    @property
    def traces(self):
        with self.lock:
            return copy.deepcopy(self._traces)

    def snapshot(self):
        with self.lock:
            return {'simulated': True, 'fault': self.fault,
                    'state': copy.deepcopy(self.state), 'traces': self.traces}

    def trace(self, method, path, body, status, applied=False):
        with self.lock:
            self._sequence += 1
            self._traces.append({'sequence': self._sequence, 'method': method,
                                 'path': path, 'body': copy.deepcopy(body),
                                 'status': status, 'applied': applied, 'simulated': True})
            self._traces[:] = self._traces[-100:]

    def response(self, path):
        with self.lock:
            info = {'ver': '0.15.1', 'vid': 'simulator', 'name': 'KEVIN simulated WLED',
                    'product': 'FOSS', 'arch': 'esp32', 'fs': {'pmt': 0, 't': 1024, 'u': 0},
                    'uptime': 60, 'fxcount': 1, 'palcount': 1,
                    'leds': {'count': 90, 'maxseg': 3, 'seglc': [1, 1, 1], 'lc': 1}}
            if path in ('/json', '/json/si'):
                return {'state': copy.deepcopy(self.state), 'info': info,
                        'effects': ['Solid'], 'palettes': ['Default'], 'simulated': True}
            return {'/json/state': copy.deepcopy(self.state), '/json/info': info,
                    '/json/effects': ['Solid'], '/json/palettes': ['Default'],
                    '/json/fxdata': [''], '/json/cfg': {'hw': {'led': {'ins': []}}},
                    '/presets.json': {}, '/simulation': self.snapshot()}.get(path)

    def apply(self, body):
        if not isinstance(body, dict) or set(body) - {'seg', 'v', 'tt'}:
            raise ValueError('simulator accepts segment updates only')
        segments = body.get('seg', [])
        if isinstance(segments, dict):
            segments = [segments]
        if not isinstance(segments, list):
            raise ValueError('seg must be a list')
        with self.lock:
            updated = copy.deepcopy(self.state)
            for patch in segments:
                if not isinstance(patch, dict) or set(patch) - {'id', 'on', 'bri', 'col'}:
                    raise ValueError('unsupported segment update')
                if type(patch.get('id')) is not int:
                    raise ValueError('segment id required')
                target = next((s for s in updated['seg'] if s['id'] == patch['id']), None)
                if target is None:
                    raise ValueError('unknown segment')
                if 'on' in patch and type(patch['on']) is not bool:
                    raise ValueError('on must be boolean')
                if 'bri' in patch and (type(patch['bri']) is not int or not 0 <= patch['bri'] <= 255):
                    raise ValueError('bri outside range')
                if 'col' in patch:
                    colors = patch['col']
                    if not isinstance(colors, list) or not 1 <= len(colors) <= 3 or any(
                        not isinstance(c, list) or len(c) not in (3, 4) or
                        any(type(v) is not int or not 0 <= v <= 255 for v in c) for c in colors):
                        raise ValueError('invalid colors')
                target.update({k: copy.deepcopy(v) for k, v in patch.items() if k != 'id'})
            if self.fault == 'ignore':
                return False
            self.state = updated
            return bool(segments)


class SimulatorServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        self.stopping.set()
        super().server_close()


def make_server(host='127.0.0.1', port=0):
    device = SimulatedWLED()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code, value):
            data = json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _delayed(self):
            with device.lock:
                timeout = device.fault == 'timeout'
            if timeout:
                device.trace(self.command, self.path, None, 'timeout')
                self.server.stopping.wait(5)
            return timeout

        def do_GET(self):
            if self._delayed():
                return self._send(504, {'error': 'simulated timeout'})
            result = device.response(self.path)
            code = 404 if result is None else 200
            device.trace('GET', self.path, None, code)
            self._send(code, result if result is not None else {'error': 'not found'})

        def do_POST(self):
            if self.path != '/json/state':
                return self._send(404, {'error': 'not found'})
            if self._delayed():
                return self._send(504, {'error': 'simulated timeout'})
            body = None
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 16384:
                    raise ValueError('invalid request size')
                body = json.loads(self.rfile.read(size))
                applied = device.apply(body)
                device.trace('POST', self.path, body, 200, applied)
                self._send(200, device.response('/json/state'))
            except (ValueError, TypeError):
                device.trace('POST', self.path, body, 400)
                self._send(400, {'error': 'invalid segment update'})

    server = SimulatorServer((host, port), Handler)
    server.device = device
    server.stopping = threading.Event()
    return server


def main():
    parser = argparse.ArgumentParser(description='Local simulated WLED API, never physical lighting.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8928)
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print(f'SIMULATION: WLED API at http://{args.host}:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
