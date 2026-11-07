"""KEVIN as an MCP server: any agent can call physical devices as tools.

Speaks MCP over stdio (JSON-RPC 2.0, one message per line). Every tool call
runs through the show controller's checks, then returns a signed physical
receipt with camera frames from before and after the action.

    python src/kevin_mcp.py            # register as a stdio MCP server
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
from pathlib import Path

PROTOCOL = '2025-06-18'
ROOT = Path(__file__).resolve().parent.parent

TOOLS = [
    {
        'name': 'led_scene',
        'description': 'Show an animated scene on the 133cm LED sign (144x16, BLE, no vendor app). '
                       'Returns a signed receipt with before/after camera frames.',
        'inputSchema': {
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'preset': {'type': 'string', 'enum': ['christmas_train', 'santa']},
                'snow': {'type': 'string', 'enum': ['none', 'normal', 'heavy']},
                'sky': {'type': 'string', 'pattern': '^#[0-9A-Fa-f]{6}$'},
                'speed': {'type': 'number', 'minimum': 0.5, 'maximum': 2},
            },
        },
    },
    {
        'name': 'radio',
        'description': 'Play or stop the carol on the old Bluetooth radio (CRF-35B).',
        'inputSchema': {
            'type': 'object', 'additionalProperties': False, 'required': ['music'],
            'properties': {
                'music': {'type': 'string', 'enum': ['play', 'stop', 'keep']},
                'volume': {'type': 'number', 'minimum': 0, 'maximum': 100},
            },
        },
    },
    {
        'name': 'say',
        'description': 'Give KEVIN a sentence in Korean or English; the on-device model plans it.',
        'inputSchema': {
            'type': 'object', 'additionalProperties': False, 'required': ['text'],
            'properties': {'text': {'type': 'string', 'minLength': 1, 'maxLength': 2000}},
        },
    },
    {
        'name': 'stop_all',
        'description': 'Stop every device now.',
        'inputSchema': {'type': 'object', 'additionalProperties': False, 'properties': {}},
    },
]

LIGHT_TOOL = {
    'name': 'light_segment',
    'description': 'Set one configured WLED segment and read its state back. '
                   'Reports simulation separately; a device reply is not a physical observation.',
    'inputSchema': {
        'type': 'object', 'additionalProperties': False, 'required': ['segment'],
        'properties': {
            'segment': {'type': 'integer', 'minimum': 0, 'maximum': 255},
            'on': {'type': 'boolean'},
            'brightness': {'type': 'integer', 'minimum': 0, 'maximum': 255},
        },
    },
}


def validate_arguments(tool, args):
    """Enforce the exposed tool contract before any camera/device effect."""
    schema = tool['inputSchema']
    if not isinstance(args, dict) or set(args) - set(schema['properties']):
        raise ValueError('invalid tool arguments')
    if any(key not in args for key in schema.get('required', [])):
        raise ValueError('missing required tool argument')
    for key, value in args.items():
        rule = schema['properties'][key]
        kind = rule['type']
        valid = ((kind == 'string' and isinstance(value, str)) or
                 (kind == 'boolean' and type(value) is bool) or
                 (kind == 'integer' and type(value) is int) or
                 (kind == 'number' and type(value) in (int, float) and math.isfinite(value)))
        if not valid or ('enum' in rule and value not in rule['enum']):
            raise ValueError('invalid tool argument: ' + key)
        if kind in ('integer', 'number') and not rule.get('minimum', -math.inf) <= value <= rule.get('maximum', math.inf):
            raise ValueError('out of range: ' + key)
        if kind == 'string':
            if not rule.get('minLength', 0) <= len(value) <= rule.get('maxLength', 4000):
                raise ValueError('invalid argument length: ' + key)
            if 'pattern' in rule and not re.fullmatch(rule['pattern'], value):
                raise ValueError('invalid argument format: ' + key)
    return dict(args)


def _plan(args, music='keep', volume=None):
    return {'mode': 'update', 'preset': args.get('preset', 'keep'), 'speed': args.get('speed'),
            'snow': args.get('snow', 'keep'), 'sky': (args.get('sky') or 'keep').lower(),
            'music': music, 'volume': volume, 'explanation': 'MCP tool call'}


class Kevin:
    def __init__(self, controller=None, camera=None, signer=None, receipts_dir=None, settle=1.5,
                 light=None):
        if controller is None:
            from show_control import ShowController
            controller = ShowController()
        if camera is None and os.getenv('KEVIN_CAMERA', '0') != 'off':
            from receipt import Camera
            camera = Camera(int(os.getenv('KEVIN_CAMERA', '0')))
        if signer is None:
            from receipt import Signer
            signer = Signer(ROOT / '.runtime' / 'kevin_ed25519.pem')
        self.controller, self.camera, self.signer, self.settle = controller, camera, signer, settle
        self.receipts_dir = Path(receipts_dir or ROOT / '.runtime' / 'receipts')
        roi = os.getenv('KEVIN_CAMERA_ROI')  # x,y,w,h around the device in the camera frame
        self.roi = tuple(int(v) for v in roi.split(',')) if roi else None
        self.lock = threading.Lock()
        # Only enqueue/stop share this lock. Never hold it while waiting for a
        # camera, controller completion or evidence files.
        self.dispatch_lock = threading.Lock()
        self.stop_requested = threading.Event()
        self.light = light

    @property
    def tools(self):
        return TOOLS + ([LIGHT_TOOL] if self.light is not None else [])

    def begin_operation(self):
        with self.dispatch_lock:
            self.stop_requested = threading.Event()
            return self.stop_requested

    @staticmethod
    def _cancelled_result(result):
        result['action_status'] = result.get('action_status', 'not_started')
        result['status'] = 'stopped'
        result['physically_verified'] = False
        result['observation'] = dict(result['observation'], status='unavailable',
                                     reason='observation_cancelled' if result['action_status'] == 'complete'
                                     else 'action_cancelled')
        result['message'] = ('Stop requested after the action completed; observation was cancelled. '
                             'Already-applied changes are not undone.' if result['action_status'] == 'complete'
                             else 'Stop requested; device state is unverified.')
        result.pop('receipt', None)
        return result

    def _wait(self, timeout=120):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            state = self.controller.get_state()
            if state['status'] in ('complete', 'preview', 'error', 'unsupported', 'idle', 'stopped'):
                return state
            time.sleep(.3)
        self.controller.stop()
        return dict(self.controller.get_state(), status='error', message='Execution timed out; stop requested.')

    def _run(self, name, args, plan=None, prompt=None, cancel=None):
        from receipt import make_receipt
        with self.lock:
            cancel = cancel if cancel is not None else self.begin_operation()
            started = time.time()
            before = None
            observation = {'status': 'unavailable', 'reason': 'camera_disabled',
                           'source': 'unspecified', 'scope': 'visual_change_only'}
            if self.camera:
                observation['source'] = getattr(self.camera, 'source', 'unspecified')
                try:
                    before = self.camera.grab()
                    observation['reason'] = 'action_not_complete'
                except Exception:
                    observation['reason'] = 'camera_unavailable'
            live = self.controller.live_configured
            with self.dispatch_lock:
                # Stop cannot land between this final check and command()
                # creating the controller's new cancellation token.
                if cancel.is_set():
                    return self._cancelled_result({'observation': observation})
                previous_planner = self.controller.planner
                if plan is not None:
                    self.controller.planner = lambda _p, _r, plan=plan: dict(plan)
                try:
                    self.controller.command(prompt or name, live)
                finally:
                    self.controller.planner = previous_planner
            state = self._wait()
            result = {'status': state['status'], 'message': state.get('message'),
                      'plan': state.get('plan'), 'led': state.get('transport'), 'radio': state.get('radio'),
                      'action_status': state['status'],
                      'live': live, 'physically_verified': False, 'observation': observation}
            if cancel.is_set():
                return self._cancelled_result(result)
            if before is not None and state['status'] == 'complete':
                if cancel.wait(self.settle):
                    return self._cancelled_result(result)
                try:
                    after = [self.camera.grab()]
                    if cancel.wait(.6):
                        return self._cancelled_result(result)
                    after.append(self.camera.grab())
                    if cancel.is_set():
                        return self._cancelled_result(result)
                    applied = state.get('plan') or {}
                    targets = []
                    if any(applied.get(k) not in (None, 'keep') for k in ('preset', 'snow', 'sky', 'speed')):
                        targets.append('led-144x16')
                    if applied.get('music') in ('play', 'stop') or applied.get('volume') is not None:
                        targets.append('radio-crf35b')
                    replies = {target: state.get('radio' if target == 'radio-crf35b' else 'transport')
                               for target in targets}
                    rec = make_receipt(device=targets[0] if len(targets) == 1 else 'kevin-show',
                                       action={'tool': name, 'arguments': args, 'targets': targets}, plan=state.get('plan'),
                                       device_reply=replies,
                                       before=before, after=after, started=started, roi=self.roi,
                                       out_dir=self.receipts_dir, signer=self.signer,
                                       source=getattr(self.camera, 'source', 'unspecified'))
                    result['receipt'] = {k: rec[k] for k in ('id', 'observation', 'path')}
                    result['observation'] = dict(rec['observation'], status='available')
                except Exception:
                    # A camera/signing failure cannot erase a completed device action.
                    result['observation']['reason'] = 'evidence_unavailable'
            if cancel.is_set():
                return self._cancelled_result(result)
            return result

    def call(self, name, args, *, cancel=None):
        tool = next((t for t in self.tools if t['name'] == name), None)
        if tool is None:
            raise KeyError(name)
        args = validate_arguments(tool, {} if args is None else args)
        if name == 'led_scene' and not args:
            raise ValueError('provide at least one scene change')
        if name == 'radio' and args.get('music') == 'keep' and 'volume' not in args:
            raise ValueError('provide a radio action or volume')
        if name == 'led_scene':
            return self._run(name, args, plan=_plan(args), cancel=cancel)
        if name == 'radio':
            return self._run(name, args, plan=_plan({}, args['music'], args.get('volume')), cancel=cancel)
        if name == 'say':
            return self._run(name, args, prompt=args['text'], cancel=cancel)
        if name == 'stop_all':
            with self.dispatch_lock:
                self.stop_requested.set()
                result = self.controller.stop()
                if self.light is not None:
                    result['light'] = self.light.stop().status
            return result
        if name == 'light_segment':
            from dataclasses import asdict
            with self.lock:
                cancel = cancel if cancel is not None else self.begin_operation()
                return asdict(self.light.apply(args, cancel))
        raise KeyError(name)


def serve(kevin, stdin=sys.stdin, stdout=sys.stdout):
    output_lock = threading.Lock()
    active = None

    def send(obj):
        with output_lock:
            stdout.write(json.dumps(obj, ensure_ascii=False) + '\n')
            stdout.flush()

    def invoke(mid, params, operation=None):
        try:
            options = {'cancel': operation} if operation is not None else {}
            out = kevin.call(params.get('name'), params.get('arguments'), **options)
            send({'jsonrpc': '2.0', 'id': mid, 'result': {
                'content': [{'type': 'text', 'text': json.dumps(out, ensure_ascii=False)}],
                'structuredContent': out,
                'isError': out.get('status') in ('error', 'failed', 'busy', 'unsupported')}})
        except (KeyError, ValueError):
            send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32602, 'message': 'unknown tool or invalid arguments'}})
        except Exception:
            send({'jsonrpc': '2.0', 'id': mid, 'result': {
                'content': [{'type': 'text', 'text': 'Tool failed; device state is unverified.'}], 'isError': True}})

    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}})
            continue
        if not isinstance(msg, dict):
            send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'invalid request'}})
            continue
        mid, method = msg.get('id'), msg.get('method')
        if mid is None:  # notification (e.g. notifications/initialized)
            continue
        if method == 'initialize':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {
                'protocolVersion': PROTOCOL, 'capabilities': {'tools': {}},
                'serverInfo': {'name': 'kevin', 'version': '0.1.0'}}})
        elif method == 'tools/list':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {'tools': getattr(kevin, 'tools', TOOLS)}})
        elif method == 'tools/call':
            params = msg.get('params') or {}
            if not isinstance(params, dict):
                send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32602, 'message': 'invalid parameters'}})
            elif params.get('name') == 'stop_all':
                invoke(mid, params)  # The reader remains responsive during a running tool.
            elif active is not None and active.is_alive():
                send({'jsonrpc': '2.0', 'id': mid, 'result': {
                    'content': [{'type': 'text', 'text': 'A tool is running; stop it or wait.'}], 'isError': True}})
            else:
                operation = kevin.begin_operation() if hasattr(kevin, 'begin_operation') else None
                active = threading.Thread(target=invoke, args=(mid, params, operation), daemon=True)
                active.start()
        elif method == 'ping':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {}})
        else:
            send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'method not found'}})
    if active is not None:
        active.join(timeout=125)


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    light = None
    if os.getenv('KEVIN_SHOW_LIVE') == '1' and os.getenv('KEVIN_WLED_HOST'):
        from devices.light import WLEDLightAdapter
        light = WLEDLightAdapter(os.environ['KEVIN_WLED_HOST'])
    serve(Kevin(light=light))
