"""Explicitly simulated HA and Valetudo HTTP peer for integration demos.

This is not Home Assistant or Valetudo firmware. The adapter uses their public
API shapes; neither a wallpad nor a vacuum is physically connected here.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


DEFAULT_ENTITIES = {'living_1': 'light.living_1', 'living_2': 'light.living_2', 'hall': 'light.hall'}


class HouseholdDevice:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with self.lock:
            self.fault = 'none'
            self.lights = {'light.living_1': False, 'light.living_2': True, 'light.hall': True}
            self.vacuum = {'status': 'docked', 'flag': 'none', 'last_zone_request': None}
            self.capabilities = ['ZoneCleaningCapability', 'BasicControlCapability']
            self._traces, self._seq = [], 0

    def set_fault(self, fault):
        if fault not in ('none', 'ignore', 'timeout', 'stale', 'unavailable', 'collateral'):
            raise ValueError('unknown simulator fault')
        with self.lock:
            self.fault = fault

    @property
    def traces(self):
        with self.lock:
            return copy.deepcopy(self._traces)

    def snapshot(self):
        with self.lock:
            return {'simulated': True, 'fault': self.fault, 'wallpad': copy.deepcopy(self.lights),
                    'vacuum': copy.deepcopy(self.vacuum), 'traces': self.traces}

    def trace(self, method, path, body, code, applied=False):
        with self.lock:
            self._seq += 1
            self._traces.append({'sequence': self._seq, 'method': method, 'path': path,
                'body': copy.deepcopy(body), 'status': code, 'applied': applied, 'simulated': True})
            self._traces[:] = self._traces[-100:]

    def get(self, path):
        with self.lock:
            if path == '/api/states':
                now = datetime.now(timezone.utc)
                report = now - timedelta(seconds=600 if self.fault == 'stale' else 0)
                return [{'entity_id': entity, 'state': ('unavailable' if self.fault == 'unavailable' else 'on' if on else 'off'),
                         'attributes': {'friendly_name': entity.split('.')[1]},
                         'last_reported': report.isoformat(), 'last_updated': (now - timedelta(days=1)).isoformat(),
                         'last_changed': (now - timedelta(days=1)).isoformat(), 'context': {'id': 'simulation'}}
                        for entity, on in self.lights.items()]
            if path == '/api/v2/robot/capabilities':
                return list(self.capabilities)
            if path == '/api/v2/robot/capabilities/ZoneCleaningCapability/properties':
                return {'zoneCount': {'min': 1, 'max': 1}, 'iterationCount': {'min': 1, 'max': 3}}
            if path == '/api/v2/robot/state/attributes':
                if self.fault == 'unavailable':
                    return []
                return [{'__class': 'StatusStateAttribute', 'metaData': {},
                         'value': self.vacuum['status'], 'flag': self.vacuum['flag']}]
            if path == '/simulation':
                return self.snapshot()
            return None

    def mutate(self, method, path, body):
        with self.lock:
            if path in ('/api/services/light/turn_on', '/api/services/light/turn_off') and method == 'POST':
                if not isinstance(body, dict) or set(body) != {'entity_id'} or body['entity_id'] not in self.lights:
                    raise ValueError('unknown light')
                if self.fault != 'ignore':
                    self.lights[body['entity_id']] = path.endswith('turn_on')
                    if self.fault == 'collateral':
                        other = next(k for k in self.lights if k != body['entity_id'])
                        self.lights[other] = not self.lights[other]
                return self.get('/api/states'), self.fault != 'ignore'
            if path == '/api/v2/robot/capabilities/ZoneCleaningCapability' and method == 'PUT':
                if (not isinstance(body, dict) or body.get('action') != 'clean' or
                    not isinstance(body.get('zones'), list) or len(body['zones']) != 1 or
                    not isinstance(body['zones'][0].get('points'), dict) or
                    set(body['zones'][0]['points']) != {'pA', 'pB', 'pC', 'pD'} or
                    type(body.get('iterations')) is not int or not 1 <= body['iterations'] <= 3):
                    raise ValueError('invalid zones')
                if self.fault != 'ignore':
                    self.vacuum.update(status='cleaning', flag='zone', last_zone_request=copy.deepcopy(body))
                return None, self.fault != 'ignore'
            if path == '/api/v2/robot/capabilities/BasicControlCapability' and method == 'PUT':
                if body != {'action': 'pause'}:
                    raise ValueError('only pause supported')
                if self.fault != 'ignore':
                    self.vacuum.update(status='paused', flag='none')
                return None, self.fault != 'ignore'
            raise ValueError('unsupported route')


class HouseholdServer(ThreadingHTTPServer):
    daemon_threads = True
    def server_close(self):
        self.stopping.set()
        super().server_close()


def make_server(host='127.0.0.1', port=0):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError('simulator binds only to a loopback IPv4 address') from None
    if address.version != 4 or not address.is_loopback:
        raise ValueError('simulator binds only to a loopback IPv4 address')
    device = HouseholdDevice()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def send(self, code, value=None):
            raw = json.dumps(value).encode() if value is not None else b'OK'
            self.send_response(code)
            self.send_header('Content-Type', 'application/json' if value is not None else 'text/plain')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def delayed(self):
            if device.fault == 'timeout':
                device.trace(self.command, self.path, None, 'timeout')
                self.server.stopping.wait(5)
                self.send(504, {'error': 'simulated timeout'})
                return True
            return False
        def do_GET(self):
            if self.delayed():
                return
            value = device.get(self.path)
            code = 404 if value is None else 200
            device.trace('GET', self.path, None, code)
            self.send(code, value)
        def mutate(self):
            if self.delayed():
                return
            body = None
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size <= 16384:
                    raise ValueError('invalid request size')
                body = json.loads(self.rfile.read(size))
                result, applied = device.mutate(self.command, self.path, body)
                device.trace(self.command, self.path, body, 200, applied)
                self.send(200, result)
            except (ValueError, TypeError, KeyError):
                device.trace(self.command, self.path, body, 400)
                self.send(400, {'error': 'invalid request'})
        do_POST = mutate
        do_PUT = mutate
    server = HouseholdServer((host, port), Handler)
    server.device, server.stopping = device, threading.Event()
    return server
