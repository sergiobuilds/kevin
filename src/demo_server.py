"""Interactive KEVIN simulation using a real python-wled HTTP client.

Only a task-owned loopback WLED simulator is reachable from this app. Physical
device environment variables are deliberately unused. Images are synthesized,
labelled as simulation, and signed with the existing receipt implementation.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import re
import sys
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from PIL import Image, ImageDraw, ImageStat

from demos.wled_sim.server import make_server as make_wled_server
from devices.light import WLEDLightAdapter
from receipt import Signer, check, make_receipt

MAX_BODY = 64 * 1024
BRIGHT_THRESHOLD = 80
EVIDENCE_ID = re.compile(r'^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$')


def _frame(brightness):
    """A synthetic entrance image, not a webcam or illuminance measurement."""
    shade = round(18 + max(0, min(255, brightness)) * .65)
    image = Image.new('RGB', (640, 360), (shade, shade, max(0, shade - 5)))
    draw = ImageDraw.Draw(image)
    draw.rectangle((230, 60, 420, 330), fill=(shade // 2, shade // 2, shade // 2))
    draw.rectangle((245, 75, 405, 315), outline=(shade + 15,) * 3, width=3)
    draw.ellipse((380, 190, 390, 200), fill=(min(255, shade + 30),) * 3)
    draw.ellipse((295, 20, 345, 45), fill=(min(255, 25 + brightness),) * 3)
    draw.text((16, 16), 'SIMULATION / SYNTHETIC FRAME', fill=(255, 210, 80))
    output = io.BytesIO()
    image.save(output, 'JPEG', quality=90)
    return output.getvalue()


def _luminance(image):
    with Image.open(io.BytesIO(image)) as frame:
        return round(ImageStat.Stat(frame.convert('L').crop((0, 50, 220, 340))).mean[0], 2)


class DemoApp:
    def __init__(self, *, out_dir=None, adapter_timeout=1.5):
        self.out_dir = Path(out_dir) if out_dir is not None else ROOT / '.runtime' / 'demo'
        self.signer = Signer(self.out_dir / 'demo-signing-key.pem')
        self.simulator = make_wled_server('127.0.0.1', 0)
        self._sim_thread = threading.Thread(target=self.simulator.serve_forever, daemon=True)
        self._sim_thread.start()
        self.adapter = WLEDLightAdapter('127.0.0.1', self.simulator.server_port,
                                        simulated=True, timeout=adapter_timeout)
        self._operation = threading.Lock()
        self._guard = threading.Lock()
        self.cancel = threading.Event()
        self.last_result = None
        self.history = []
        self._closed = False
        self.devices = None
        self.adapter_timeout = adapter_timeout
        self._stopping = 0

    def close(self):
        with self._guard:
            if self._closed:
                return
            self._closed = True
            self.cancel.set()
        self.adapter.stop()
        if self.devices is not None:
            self.devices.close()
        self.simulator.shutdown()
        self.simulator.server_close()
        self._sim_thread.join(timeout=2)

    def state(self):
        with self._guard:
            values = {'last_result': copy.deepcopy(self.last_result),
                      'history': copy.deepcopy(self.history)}
        return dict(values, device=self.simulator.device.snapshot(),
                    tracks=self.devices.snapshot() if self.devices is not None else {}, source='simulation',
                    physically_verified=False, busy=self._operation.locked())

    @staticmethod
    def _result(status, message, **extra):
        result = dict(status=status, action_status=None, message=message, result=None,
                      before_url=None, after_url=None, receipt=None,
                      source='simulation', physically_verified=False)
        result.update(extra)
        return result

    def _remember_locked(self, result):
        """Publish while holding _guard, atomically with cancellation checks."""
        self.last_result = copy.deepcopy(result)
        self.history.append(copy.deepcopy(result))
        self.history[:] = self.history[-20:]
        return result

    @staticmethod
    def _cancelled_result(result):
        # Preserve what the adapter reported separately from the cancelled job.
        # A late image/signature must never turn a stopped job back into success.
        result.update(action_status=result.get('action_status') or result['status'],
                      status='stopped', before_url=None, after_url=None, receipt=None,
                      message='요청을 취소했어요. 이미 보낸 변경은 남아 있을 수 있습니다.')
        result.pop('receipt_error', None)
        return result

    @staticmethod
    def validate(body):
        if not isinstance(body, dict) or not isinstance(body.get('action'), str):
            raise ValueError('action이 필요합니다.')
        action = body['action']
        allowed = {'light': {'action', 'segment', 'on', 'brightness', 'color'},
                   'observe': {'action', 'scenario'}, 'fault': {'action', 'fault'},
                   'reset': {'action'}, 'stop': {'action'},
                   'device': {'action', 'device', 'plan'},
                   'device_fault': {'action', 'device', 'fault'}}
        if action not in allowed or set(body) - allowed[action]:
            raise ValueError('지원하지 않는 동작 또는 인수입니다.')
        if action == 'light':
            if type(body.get('segment')) is not int or not 0 <= body['segment'] <= 2:
                raise ValueError('segment는 0부터 2까지입니다.')
            if not set(body) & {'on', 'brightness', 'color'}:
                raise ValueError('변경할 조명 값을 하나 이상 지정하세요.')
            if 'on' in body and type(body['on']) is not bool:
                raise ValueError('on은 boolean이어야 합니다.')
            if 'brightness' in body and (type(body['brightness']) is not int or
                                         not 0 <= body['brightness'] <= 255):
                raise ValueError('brightness는 0부터 255까지입니다.')
            if 'color' in body and (not isinstance(body['color'], list) or len(body['color']) != 3 or
                                    any(type(v) is not int or not 0 <= v <= 255 for v in body['color'])):
                raise ValueError('color는 RGB 정수 3개여야 합니다.')
        elif action == 'observe' and body.get('scenario') not in ('dark', 'already_bright', 'still_dark', 'stale'):
            raise ValueError('지원하지 않는 관찰 시나리오입니다.')
        elif action == 'fault' and body.get('fault') not in ('none', 'ignore', 'timeout'):
            raise ValueError('지원하지 않는 가상 오류입니다.')
        elif action in ('device', 'device_fault'):
            from demo_devices import FAULTS
            if not isinstance(body.get('device'), str) or body['device'] not in FAULTS:
                raise ValueError('지원하지 않는 장치입니다.')
            if action == 'device' and not isinstance(body.get('plan'), dict):
                raise ValueError('장치 명령이 필요합니다.')
            if action == 'device_fault' and body.get('fault') not in FAULTS[body['device']]:
                raise ValueError('지원하지 않는 가상 오류입니다.')

    def action(self, body):
        self.validate(body)
        if body['action'] == 'stop':
            with self._guard:
                self._stopping += 1
                self.cancel.set()
                self.adapter.stop()
                devices = self.devices
            try:
                stopped = devices.stop() if devices is not None else {}
                failed = any(r['status'] == 'failed' for r in stopped.values())
                with self._guard:
                    return self._remember_locked(self._result('failed' if failed else 'stopped',
                        '요청은 취소했지만 일부 장치의 정지 응답을 확인하지 못했어요.' if failed else
                        '대기 중인 제어를 취소했어요. 조명 상태는 그대로 둡니다.',
                        result=stopped, device_results=stopped))
            finally:
                with self._guard:
                    self._stopping -= 1
        # Register the active operation and its cancellation token together.
        # stop() must not see an acquired operation with the previous token.
        with self._guard:
            if self._closed:
                return self._result('failed', '데모 서버가 종료됐어요.')
            if self._stopping:
                return self._result('busy', '장치의 정지 응답을 확인 중이에요.')
            if not self._operation.acquire(blocking=False):
                return self._result('busy', '이전 요청을 처리하고 있어요. 정지하거나 잠시 기다려 주세요.')
            self.cancel = threading.Event()
            cancel = self.cancel
        try:
            if body['action'] == 'fault':
                self.simulator.device.set_fault(body['fault'])
                result = self._result('ready', '가상 장치의 응답 조건을 바꿨어요.')
            elif body['action'] == 'reset':
                self.simulator.device.reset()
                if self.devices is not None:
                    self.devices.reset()
                with self._guard:
                    self.history.clear()
                result = self._result('ready', '가상 장치를 처음 상태로 되돌렸어요.')
            elif body['action'] == 'light':
                result = self._light(body, cancel)
            elif body['action'] in ('device', 'device_fault'):
                if self.devices is None:
                    from demo_devices import DemoDevices
                    self.devices = DemoDevices(timeout=self.adapter_timeout)
                if body['action'] == 'device_fault':
                    self.devices.set_fault(body['device'], body['fault'])
                    result = self._result('ready', '가상 장치의 응답 조건을 바꿨어요.', device=body['device'])
                else:
                    reply = asdict(self.devices.apply(body['device'], body['plan'], cancel))
                    status = reply['status']
                    message = {'acknowledged': '장치 응답을 확인했어요. 실물 동작은 별도 확인이 필요합니다.',
                               'sent_unverified': '전송했어요. 실제 동작은 아직 확인하지 못했어요.',
                               'failed': '요청한 결과를 확인하지 못했어요.',
                               'stopped': '요청을 취소했어요. 정지 응답을 확인해 주세요.'}.get(status, '장치 결과를 확인해 주세요.')
                    result = self._result(status, message, device=body['device'],
                                          action_status=status, result=reply)
            else:
                result = self._observe(body, cancel)
            with self._guard:
                if cancel.is_set():
                    result = self._cancelled_result(result)
                return self._remember_locked(result)
        finally:
            self._operation.release()

    def _brightness(self, segment):
        state = self.simulator.device.snapshot()['state']
        target = next(s for s in state['seg'] if s['id'] == segment)
        return target['bri'] if target['on'] and state['on'] else 0

    def _attach_receipt(self, result, body, before, after, started, cancel):
        if cancel.is_set() or result['status'] == 'stopped':
            return self._cancelled_result(result)
        try:
            receipt = make_receipt(
                device='simulated-wled-entrance', action=body,
                plan={key: value for key, value in body.items() if key != 'action'},
                device_reply=result['result'], before=before, after=[after], started=started,
                out_dir=self.out_dir / 'receipts', signer=self.signer, source='simulation')
            folder = Path(receipt['path'])
            stored = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
            images = {name: (folder / name).read_bytes() for name in stored['evidence']}
            rid = stored['id']
            result['before_url'] = f'/evidence/{rid}/before.jpg'
            result['after_url'] = f'/evidence/{rid}/after1.jpg'
            result['receipt'] = {'id': rid, 'observation': stored['observation'],
                                 'verified': check(stored, images),
                                 'url': f'/evidence/{rid}/receipt.json'}
        except Exception:
            # Device/API outcome remains available when evidence writing fails.
            result['receipt_error'] = '합성 관찰 기록을 저장하지 못했어요.'
        return self._cancelled_result(result) if cancel.is_set() else result

    def _light(self, body, cancel):
        started = time.time()
        before = _frame(self._brightness(body['segment']))
        plan = {key: value for key, value in body.items() if key != 'action'}
        if cancel.is_set():
            return self._cancelled_result(self._result('stopped', '', action_status='not_sent'))
        reply = asdict(self.adapter.apply(plan, cancel))
        status = reply['status']
        messages = {'acknowledged': '요청한 구역의 상태를 다시 읽어 변경을 확인했어요.',
                    'failed': '요청한 상태를 확인하지 못했어요. 성공으로 처리하지 않았습니다.',
                    'stopped': '요청을 취소했어요. 이미 보낸 변경은 남아 있을 수 있습니다.'}
        result = self._result(status, messages.get(status, '장치 응답을 확인해 주세요.'), action_status=status)
        result['result'] = reply
        if cancel.is_set() or status == 'stopped':
            return self._cancelled_result(result)
        after = _frame(self._brightness(body['segment']))
        return self._attach_receipt(result, body, before, after, started, cancel)

    def _observe(self, body, cancel):
        started, scenario = time.time(), body['scenario']
        if cancel.is_set():
            return self._cancelled_result(self._result('stopped', '', action_status='not_sent'))
        if scenario == 'stale':
            result = self._result('stale', '오래된 관찰이라 조명 명령을 보내지 않았어요.', action_status='not_sent')
            result['result'] = {'scenario': scenario, 'command_sent': False, 'frame_age_seconds': 120}
            return result
        # Set a deterministic fixture precondition, not a claimed device command.
        # Only this simulator's entrance is seeded; other segments/faults stay.
        brightness = 180 if scenario == 'already_bright' else 24
        with self.simulator.device.lock:
            entrance = next(s for s in self.simulator.device.state['seg'] if s['id'] == 0)
            entrance.update(on=True, bri=brightness)
        before = _frame(brightness)
        before_value = _luminance(before)
        info = {'scenario': scenario, 'synthetic_precondition': {'segment': 0, 'brightness': brightness},
                'before_luminance': before_value, 'threshold': BRIGHT_THRESHOLD, 'command_sent': False}
        if before_value >= BRIGHT_THRESHOLD:
            result = self._result('already_bright', '이미 밝아서 조명 명령을 보내지 않았어요.', action_status='not_sent')
            info['after_luminance'] = before_value
            result['result'] = info
            return self._attach_receipt(result, body, before, before, started, cancel)
        traces = self.simulator.device.traces
        previous_sequence = traces[-1]['sequence'] if traces else 0
        reply = asdict(self.adapter.apply({'segment': 0, 'on': True, 'brightness': 220}, cancel))
        info.update(command_sent=any(t['method'] == 'POST' and t['sequence'] > previous_sequence
                                     for t in self.simulator.device.traces),
                    device=reply)
        after = _frame(24 if scenario == 'still_dark' else self._brightness(0))
        after_value = _luminance(after)
        info.update(after_luminance=after_value, observed_bright=after_value >= BRIGHT_THRESHOLD)
        if reply['status'] == 'stopped' or cancel.is_set():
            result = self._result('stopped', '관찰 작업을 취소했어요. 이미 보낸 변경은 남아 있을 수 있습니다.')
        elif reply['status'] != 'acknowledged':
            result = self._result('failed', '조명 상태가 요청과 달라요. 성공으로 처리하지 않았습니다.')
        elif after_value < BRIGHT_THRESHOLD:
            result = self._result('failed', '조명 응답은 왔지만 합성 화면은 여전히 어두워요. 확인이 필요합니다.')
        else:
            result = self._result('verified', '조명 상태와 합성 화면의 밝기 변화를 확인했어요.')
        result['action_status'] = reply['status']
        result['result'] = info
        return self._attach_receipt(result, body, before, after, started, cancel)


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        super().server_close()
        # TCPServer also calls this when binding fails during construction.
        app = getattr(self, 'app', None)
        if app is not None:
            app.close()


def make_server(host='127.0.0.1', port=8790, *, out_dir=None, adapter_timeout=1.5, static_dir=None):
    app = DemoApp(out_dir=out_dir, adapter_timeout=adapter_timeout)
    static_dir = Path(static_dir) if static_dir is not None else ROOT / 'docs' / 'demo'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_bytes(self, status, data, content_type):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def send_json(self, status, value):
            self.send_bytes(status, json.dumps(value, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/api/state':
                return self.send_json(200, app.state())
            static = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/demo.js': ('demo.js', 'text/javascript; charset=utf-8'),
                      '/demo.css': ('demo.css', 'text/css; charset=utf-8'),
                      '/review.html': ('review.html', 'text/html; charset=utf-8')}
            if path in static:
                filename, content_type = static[path]
                try:
                    return self.send_bytes(200, (static_dir / filename).read_bytes(), content_type)
                except OSError:
                    return self.send_json(404, {'error': 'not found'})
            parts = path.split('/')
            if (len(parts) == 4 and parts[1] == 'evidence' and EVIDENCE_ID.fullmatch(parts[2]) and
                    parts[3] in ('receipt.json', 'before.jpg', 'after1.jpg')):
                candidate = app.out_dir / 'receipts' / parts[2] / parts[3]
                base = (app.out_dir / 'receipts').resolve()
                if not candidate.resolve().is_relative_to(base):
                    return self.send_json(404, {'error': 'not found'})
                try:
                    kind = 'application/json; charset=utf-8' if parts[3] == 'receipt.json' else 'image/jpeg'
                    return self.send_bytes(200, candidate.read_bytes(), kind)
                except OSError:
                    pass
            self.send_json(404, {'error': 'not found'})

        def do_POST(self):
            if urlsplit(self.path).path != '/api/action':
                return self.send_json(404, {'error': 'not found'})
            origin = self.headers.get('Origin')
            if origin is not None:
                try:
                    parsed = urlsplit(origin)
                except ValueError:
                    return self.send_json(403, {'error': 'same-origin requests only'})
                if (parsed.scheme not in ('http', 'https') or parsed.netloc != self.headers.get('Host') or
                        parsed.path or parsed.query or parsed.fragment):
                    return self.send_json(403, {'error': 'same-origin requests only'})
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                return self.send_json(403, {'error': 'same-origin requests only'})
            if self.headers.get_content_type() != 'application/json':
                return self.send_json(415, {'error': 'application/json required'})
            if self.headers.get('Transfer-Encoding'):
                return self.send_json(400, {'error': 'chunked requests are not supported'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= MAX_BODY:
                    return self.send_json(413, {'error': 'request must contain 1..65536 bytes'})
                self.connection.settimeout(5)
                body = json.loads(self.rfile.read(size))
                result = app.action(body)
            except (ValueError, TypeError):
                return self.send_json(400, {'error': 'invalid action or arguments'})
            except Exception:
                return self.send_json(500, {'error': 'demo request failed'})
            self.send_json(200, result)

    try:
        server = DemoServer((host, port), Handler)
    except Exception:
        app.close()
        raise
    server.app = app
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description='KEVIN simulation demo; never connects to physical devices.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument('--out-dir', type=Path)
    args = parser.parse_args(argv)
    server = make_server(args.host, args.port, out_dir=args.out_dir)
    print(f'SIMULATION: KEVIN demo at http://{args.host}:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
