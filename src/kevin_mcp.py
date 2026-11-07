"""KEVIN as an MCP server: any agent can call physical devices as tools.

Speaks MCP over stdio (JSON-RPC 2.0, one message per line). Every tool call
runs through the show controller's checks, then returns a signed physical
receipt with camera frames from before and after the action.

    python src/kevin_mcp.py            # register as a stdio MCP server
"""
from __future__ import annotations

import json
import os
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
                'music': {'type': 'string', 'enum': ['play', 'stop']},
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


def _plan(args, music='keep', volume=None):
    return {'mode': 'update', 'preset': args.get('preset', 'keep'), 'speed': args.get('speed'),
            'snow': args.get('snow', 'keep'), 'sky': (args.get('sky') or 'keep').lower(),
            'music': music, 'volume': volume, 'explanation': 'MCP tool call'}


class Kevin:
    def __init__(self, controller=None, camera=None, signer=None, receipts_dir=None, settle=1.5):
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

    def _wait(self, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            state = self.controller.get_state()
            if state['status'] in ('complete', 'preview', 'error', 'unsupported', 'idle', 'stopped'):
                return state
            time.sleep(.3)
        return self.controller.get_state()

    def _run(self, name, args, plan=None, prompt=None):
        from receipt import make_receipt
        with self.lock:
            started = time.time()
            before = self.camera.grab() if self.camera else None
            live = self.controller.live_configured
            if plan is not None:
                self.controller.planner = lambda _p, _r, plan=plan: dict(plan)
            try:
                self.controller.command(prompt or name, live)
                state = self._wait()
            finally:
                self.controller.planner = None
            result = {'status': state['status'], 'message': state.get('message'),
                      'plan': state.get('plan'), 'led': state.get('transport'), 'radio': state.get('radio'),
                      'live': live}
            if self.camera and state['status'] == 'complete':
                time.sleep(self.settle)
                after = [self.camera.grab(), (time.sleep(.6), self.camera.grab())[1]]
                rec = make_receipt(device='led-144x16' if name != 'radio' else 'radio-crf35b',
                                   action={'tool': name, 'arguments': args}, plan=state.get('plan'),
                                   device_reply=state.get('transport'), before=before, after=after,
                                   started=started, roi=self.roi, out_dir=self.receipts_dir, signer=self.signer)
                result['receipt'] = {k: rec[k] for k in ('id', 'observation', 'path')}
            return result

    def call(self, name, args):
        args = args or {}
        if name == 'led_scene':
            return self._run(name, args, plan=_plan(args))
        if name == 'radio':
            return self._run(name, args, plan=_plan({}, args['music'], args.get('volume')))
        if name == 'say':
            return self._run(name, args, prompt=args['text'])
        if name == 'stop_all':
            return self.controller.stop()
        raise KeyError(name)


def serve(kevin, stdin=sys.stdin, stdout=sys.stdout):
    def send(obj):
        stdout.write(json.dumps(obj, ensure_ascii=False) + '\n')
        stdout.flush()

    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}})
            continue
        mid, method = msg.get('id'), msg.get('method')
        if mid is None:  # notification (e.g. notifications/initialized)
            continue
        if method == 'initialize':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {
                'protocolVersion': PROTOCOL, 'capabilities': {'tools': {}},
                'serverInfo': {'name': 'kevin', 'version': '0.1.0'}}})
        elif method == 'tools/list':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {'tools': TOOLS}})
        elif method == 'tools/call':
            params = msg.get('params') or {}
            try:
                out = kevin.call(params.get('name'), params.get('arguments'))
                send({'jsonrpc': '2.0', 'id': mid, 'result': {
                    'content': [{'type': 'text', 'text': json.dumps(out, ensure_ascii=False)}],
                    'structuredContent': out, 'isError': out.get('status') == 'error'}})
            except KeyError:
                send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32602, 'message': 'unknown tool'}})
            except Exception as exc:  # report, never crash the server
                send({'jsonrpc': '2.0', 'id': mid, 'result': {
                    'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}})
        elif method == 'ping':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {}})
        else:
            send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'method not found'}})


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    serve(Kevin())
