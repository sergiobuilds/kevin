"""KEVIN bridge: the web app's hands on this computer.

The deployed web page (DOS-style UI, webcam, voice) runs in the browser. Devices
live on this machine over Bluetooth, so the page talks to this small local
server. OpenAI plans the request when OPENAI_API_KEY is set; otherwise the
on-device model does. The API key never leaves this machine.

    python src/kevin_bridge.py            # http://127.0.0.1:8787
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))  # embedded Pythons ignore PYTHONPATH
for _extra in os.environ.get('PYTHONPATH', '').split(os.pathsep):
    if _extra and _extra not in sys.path:
        sys.path.append(_extra)

import guardian
import kevin_agent
import show_intent
import song

def _load_env_file():
    """Read KEY=VALUE lines from .openai.env next to the repo, if the shell did not set them."""
    path = Path(__file__).resolve().parent.parent / '.openai.env'
    if path.is_file():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and key and not os.getenv(key):
                os.environ[key] = value.strip().strip('"')


_load_env_file()


class Eye:
    """KEVIN's eye: the webcam on this machine, shared by every viewer of the web app."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None
        self.captured_at = None
        self.error = None
        self.camera_name = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            import cv2
        except Exception as exc:
            self.error = 'opencv missing: ' + str(exc)[:60]
            return
        cap = None
        while True:
            if cap is None or not cap.isOpened():
                cap = self._open_best(cv2)
                if cap is None:
                    self._invalidate('no camera gives a picture')
                    time.sleep(2)
                    continue
            ok, frame = cap.read()
            if ok and frame is not None:
                ok, jpg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                if ok and jpg is not None:
                    with self.lock:
                        self.frame, self.error, self.captured_at = jpg.tobytes(), None, time.monotonic()
                else:
                    self._invalidate('camera encoding failed')
            else:
                self._invalidate('camera returned no frame')
                cap.release()
                cap = None
                time.sleep(1)
            time.sleep(0.1)

    def _open_best(self, cv2):
        """Laptops often expose an IR face camera first; pick the camera with a visible picture."""
        wanted = os.getenv('KEVIN_CAMERA', '')
        order = [int(wanted)] if wanted.isdigit() else [0, 1, 2, 3]
        best, best_mean = None, -1.0
        for index in order:
            backends = (cv2.CAP_MSMF, cv2.CAP_DSHOW) if sys.platform == 'win32' else (cv2.CAP_ANY,)
            for backend in backends:
                cap = cv2.VideoCapture(index, backend)
                if not cap.isOpened():
                    continue
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                mean = -1.0
                for _ in range(15):
                    ok, frame = cap.read()
                    if ok and frame is not None and frame.ndim == 3:
                        mean = float(frame.mean())
                if mean > best_mean:
                    if best is not None:
                        best.release()
                    best, best_mean, self.camera_name = cap, mean, f'{index}/{backend}'
                else:
                    cap.release()
                break
            if best_mean > 40:
                break
        return best

    def jpeg(self):
        frame = self.snapshot()
        return frame['image'] if frame else None

    def _invalidate(self, reason):
        with self.lock:
            self.frame, self.captured_at, self.error = None, None, reason

    def snapshot(self):
        with self.lock:
            if self.frame is None or self.error or self.captured_at is None or time.monotonic() - self.captured_at > 2:
                return None
            return {'image': self.frame, 'captured_at': self.captured_at, 'source': 'camera'}


ELEVEN_VOICE = os.getenv('ELEVENLABS_VOICE_ID', '')
_tts_cache = {}


def tts(text: str) -> bytes | None:
    key = os.getenv('ELEVENLABS_API_KEY', '').strip()
    if not key or not text:
        return None
    if text in _tts_cache:
        return _tts_cache[text]
    voice = ELEVEN_VOICE or 'IQt4I2Udet4gAwcFGAlU'  # Oki: playful young Korean male
    body = {'text': text, 'model_id': os.getenv('ELEVENLABS_MODEL_ID', 'eleven_multilingual_v2'),
            'voice_settings': {'stability': 0.45, 'similarity_boost': 0.8, 'style': 0.35, 'use_speaker_boost': True}}
    req = urllib.request.Request(f'https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128',
                                 json.dumps(body, ensure_ascii=False).encode(),
                                 {'xi-api-key': key, 'Content-Type': 'application/json', 'Accept': 'audio/mpeg'})
    with urllib.request.urlopen(req, timeout=20) as r:
        audio = r.read()
    if len(_tts_cache) > 200:
        _tts_cache.clear()
    _tts_cache[text] = audio
    return audio
PORT = int(os.getenv('KEVIN_BRIDGE_PORT', '8787'))
OPENAI_URL = 'https://api.openai.com/v1/responses'


def openai_json(system: str, user_content, schema: dict, name: str, timeout=30, model=None) -> dict:
    key = os.getenv('OPENAI_API_KEY', '').strip()
    if not key:
        raise RuntimeError('OPENAI_API_KEY not set')
    body = {
        'model': model or os.getenv('KEVIN_OPENAI_MODEL', 'gpt-4.1-mini'),
        'input': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user_content}],
        'text': {'format': {'type': 'json_schema', 'name': name, 'schema': schema, 'strict': True}},
    }
    if body['model'].startswith('gpt-5'):
        body['reasoning'] = {'effort': os.getenv('KEVIN_REASONING', 'none')}
    req = urllib.request.Request(OPENAI_URL, json.dumps(body).encode(),
                                 {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    text = data.get('output_text')
    if not text:
        for item in data.get('output', []):
            for part in item.get('content', []) or []:
                if part.get('type') == 'output_text':
                    text = part['text']
    return json.loads(text)


def plan(text: str, current: dict) -> tuple[dict, str]:
    """OpenAI first, on-device model second; explicit words always win (show_intent.ground)."""
    if os.getenv('OPENAI_API_KEY', '').strip():
        try:
            raw = openai_json(show_intent.SYSTEM,
                              json.dumps({'prompt': text, 'current_recipe': current}, ensure_ascii=False),
                              show_intent.SCHEMA, 'kevin_plan')
            return show_intent._validate(show_intent.ground(text, show_intent._validate(raw), current)), 'openai'
        except Exception:
            pass
    return show_intent.interpret(text, current), 'local'


VISION_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['person', 'what'],
                 'properties': {'person': {'type': ['boolean', 'null']}, 'what': {'type': 'string'}}}


def see_person(jpeg_b64: str) -> tuple[bool | None, str]:
    """Ask OpenAI vision whether a person is in the webcam frame. None when unavailable."""
    if not os.getenv('OPENAI_API_KEY', '').strip() or not jpeg_b64:
        return None, ''
    try:
        out = openai_json('You watch a home webcam. Return person=true if a person is visible, '
                          'false only when the room view clearly supports no person being present, '
                          'or null if visibility is poor, obstructed or uncertain. Describe in one short Korean phrase.',
                          [{'type': 'input_image', 'image_url': 'data:image/jpeg;base64,' + jpeg_b64}],
                          VISION_SCHEMA, 'kevin_seen', timeout=20)
        if type(out.get('person')) is not bool or not isinstance(out.get('what'), str):
            return None, ''
        return out['person'], out['what']
    except Exception:
        return None, ''


from bridge_runtime import BridgeRuntime


class Bridge(BridgeRuntime):
    def __init__(self, controller=None, eye=None, **kwargs):
        if controller is None:
            from show_control import ShowController
            controller = ShowController()
        super().__init__(controller, Eye() if eye is None else eye,
            plan_fn=kwargs.pop('plan_fn', plan),
            agent_fn=kwargs.pop('agent_fn', lambda text, history, state:
                kevin_agent.decide(text, history, state, openai_json, judge=False)),
            vision_fn=kwargs.pop('vision_fn', see_person), **kwargs)

    def health(self):
        frame = self.eye.snapshot()
        return {'ok': True, 'live': self.c.live_configured, 'camera': frame is not None,
                'camera_error': self.eye.error, 'camera_name': self.eye.camera_name,
                'voice': bool(os.getenv('ELEVENLABS_API_KEY', '').strip()),
                'openai': bool(os.getenv('OPENAI_API_KEY', '').strip()),
                'led': bool(self.c.config.get('KEVIN_LED_ADDRESS')),
                'radio': self.c.config.get('KEVIN_RADIO_DEVICE') or None,
                'mode': self.mode, 'idle_limit': self.idle_limit,
                'availability_scope': 'configured_not_device_verified'}


def make_handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, data):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.end_headers()

        def _static(self):
            root = Path(__file__).resolve().parent.parent / 'docs' / 'app'
            rel = self.path.split('?', 1)[0].lstrip('/') or 'index.html'
            if rel in ('app', 'app/'):
                rel = 'index.html'
            rel = rel.removeprefix('app/')
            target = (root / rel).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                return False
            body = target.read_bytes()
            ctype = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                     '.css': 'text/css; charset=utf-8', '.png': 'image/png'}.get(target.suffix, 'application/octet-stream')
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(body)
            return True

        def do_GET(self):
            if self.path.startswith('/health'):
                return self._send(200, bridge.health())
            if self.path.startswith('/camera.jpg'):
                data = bridge.eye.jpeg()
                if not data:
                    return self._send(503, {'error': bridge.eye.error or 'camera starting'})
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path.startswith('/state'):
                return self._send(200, bridge.state())
            path = urlsplit(self.path).path
            if re.fullmatch(r'/jobs/[a-f0-9]{32}', path):
                try:
                    return self._send(200, bridge.state(path.rsplit('/', 1)[1]))
                except KeyError:
                    return self._send(404, {'error': 'job not found'})
            match = re.fullmatch(r'/evidence/([0-9]{8}-[0-9]{6}-[a-f0-9]{6})/(receipt.json|before.jpg|after[12].jpg)', path)
            if match:
                rid, name = match.groups()
                with bridge.lock:
                    known = any((j.get('receipt') or {}).get('id') == rid for j in bridge.jobs.values())
                target = bridge.out_dir / 'receipts' / rid / name
                if known and target.is_file() and target.resolve().is_relative_to((bridge.out_dir / 'receipts').resolve()):
                    data = target.read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json' if name.endswith('.json') else 'image/jpeg')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(data)
                    return
                return self._send(404, {'error': 'evidence unavailable'})
            if not self._static():
                self._send(404, {'error': 'not found'})

        def do_POST(self):
            try:
                n = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(min(n, 4_000_000)) or b'{}')
                if self.path.startswith('/say'):
                    return self._send(200, bridge.say(str(body.get('text', ''))[:500]))
                if self.path.startswith('/tts'):
                    audio = tts(str(body.get('text', ''))[:300])
                    if not audio:
                        return self._send(503, {'error': 'voice unavailable'})
                    self.send_response(200)
                    self.send_header('Content-Type', 'audio/mpeg')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Content-Length', str(len(audio)))
                    self.end_headers()
                    self.wfile.write(audio)
                    return
                if self.path.startswith('/stop'):
                    return self._send(200, bridge.stop())
                if self.path.startswith('/mode'):
                    return self._send(200, bridge.set_mode(body.get('mode')))
                if self.path.startswith('/guard'):
                    return self._send(200, bridge.guard(body))
                self._send(404, {'error': 'not found'})
            except Exception:
                self._send(400, {'error': 'request failed', 'reply': '요청을 처리하지 못했어. 실행 결과를 확인해 줘.'})
    return Handler


WARM_LINES = ['안녕, 난 케빈이야. 이 집은 내가 맡을게. 말 걸어줘.', '응, 말 걸어줘.', '다녀와. 집은 내가 지킬게.',
              '사람이 없는 상태가 확인되면 끌게. 못 보면 기다릴게.', '중단을 요청할게.', '누구세요? 경보를 울립니다!',
              '사람이 없는 상태를 확인해서 중단을 요청했어.']


def warm_up():
    """Open the OpenAI/ElevenLabs connections and cache fixed lines so the first request is fast."""
    try:
        plan('하늘 그대로 둬', {})
    except Exception:
        pass
    for line in WARM_LINES:
        try:
            tts(line)
        except Exception:
            pass


def main():
    bridge = Bridge()
    threading.Thread(target=warm_up, daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1', PORT), make_handler(bridge))
    h = bridge.health()
    print(f"KEVIN bridge  http://127.0.0.1:{PORT}  live={h['live']}  openai={h['openai']}", flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
