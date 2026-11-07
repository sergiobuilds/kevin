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
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit
import urllib.parse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # embedded Pythons ignore PYTHONPATH
for _extra in os.environ.get('PYTHONPATH', '').split(os.pathsep):
    if _extra and _extra not in sys.path:
        sys.path.append(_extra)

import guardian
import kevin_agent
import show_intent
import song
from show_transport import check_cancel
from ai_settings import AISettings, SettingsError, runtime_config, request_json as ai_json

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
        self.error = None
        self.camera_name = None
        if os.getenv('KEVIN_CAMERA', '').strip().lower() == 'off':
            self.error = 'camera disabled'
        else:
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
                    self.error = 'no camera gives a picture'
                    time.sleep(2)
                    continue
            ok, frame = cap.read()
            if ok:
                ok, jpg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                with self.lock:
                    self.frame, self.error = jpg.tobytes(), None
            else:
                self.error = 'camera returned no frame'
                cap.release()
                cap = None
                time.sleep(1)
            time.sleep(0.1)

    def _open_best(self, cv2):
        """Laptops often expose an IR face camera first; pick the camera with a visible picture."""
        wanted = os.getenv('KEVIN_CAMERA', '').strip()
        if wanted.lower() == 'off':
            return None
        order = [int(wanted)] if wanted.isdigit() else [0, 1, 2, 3]
        best, best_mean = None, -1.0
        for index in order:
            for backend in (getattr(cv2, 'CAP_MSMF', 0), getattr(cv2, 'CAP_DSHOW', 0)):
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
        with self.lock:
            return self.frame


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
    """Use the selected command AI, without a silent cross-provider fallback."""
    from show_control import interpret
    config = runtime_config()
    return interpret(text, current, config), config['KEVIN_PLANNER']


VISION_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['person', 'what'],
                 'properties': {'person': {'type': 'boolean'}, 'what': {'type': 'string'}}}


def see_person(jpeg_b64: str) -> tuple[bool | None, str]:
    """Ask OpenAI vision whether a person is in the webcam frame. None when unavailable."""
    if not os.getenv('OPENAI_API_KEY', '').strip() or not jpeg_b64:
        return None, ''
    try:
        out = openai_json('You watch a home webcam. Answer whether a person is visible and describe in one short Korean phrase.',
                          [{'type': 'input_image', 'image_url': 'data:image/jpeg;base64,' + jpeg_b64}],
                          VISION_SCHEMA, 'kevin_seen', timeout=20)
        return bool(out['person']), out['what']
    except Exception:
        return None, ''


class Bridge:
    def __init__(self, controller=None, ai=None, eye=None):
        if controller is None:
            from show_control import ShowController
            controller = ShowController()
        self.c = controller
        self.ai = ai or AISettings()
        self.lock = threading.Lock()
        self.job_lock = threading.RLock()
        self.job_cancel = threading.Event()
        self.alarm_until = 0
        self.eye = eye or Eye()

    def health(self):
        return {'ok': True, 'live': self.c.live_configured, 'camera': self.eye.jpeg() is not None,
                'camera_error': self.eye.error, 'camera_name': self.eye.camera_name, 'voice': bool(os.getenv('ELEVENLABS_API_KEY', '').strip()),
                'openai': bool(os.getenv('OPENAI_API_KEY', '').strip()), 'ai': self.ai.public(),
                'led': bool(os.getenv('KEVIN_LED_ADDRESS')), 'radio': os.getenv('KEVIN_RADIO_DEVICE') or None}

    def _new_job(self):
        with self.job_lock:
            prev = getattr(self, 'job_cancel', None)
            if prev:
                prev.set()
            self.c.supersede()
            self.job_cancel = threading.Event()
            return self.job_cancel

    def stop(self):
        # Planning and device waits must never hold this lifecycle lock.
        with self.job_lock:
            if getattr(self, 'job_cancel', None):
                self.job_cancel.set()
            return self.c.stop()

    def _command(self, text, plan, cancel):
        with self.job_lock:
            check_cancel(cancel)
            return self.c.command(text, self.c.live_configured, cancel=cancel,
                                  planner=lambda *_: dict(plan))

    def _invalidate_scene(self, cancel):
        with self.job_lock:
            check_cancel(cancel)
            with self.c.lock:
                self.c.applied_scene = None

    def say(self, text):
        with self.lock:
            if not self.ai.public()['configured']:
                return {'reply': '먼저 명령 해석 AI를 설정해 주세요.', 'error': 'ai_setup_required'}
            self.history = getattr(self, 'history', [])
            cancel = self._new_job()
            state = self.c.get_state()
            summary = {'recipe': state.get('recipe'), 'led': getattr(self, 'led_now', 'scene'),
                       'radio': (state.get('radio') or {}).get('status')}
            try:
                config = runtime_config(self.ai.env)
                def selected_json(system, content, schema, name, timeout=30, model=None):
                    return ai_json(system, content, schema, config)
                turn = kevin_agent.decide(text, self.history, summary, selected_json)
                source = config['KEVIN_PLANNER'] + '-agent'
            except Exception:
                self.ai.mark_failed()
                return {'reply': 'AI 연결을 확인해 주세요. 설정에서 다시 확인할 수 있어요.', 'error': 'ai_unavailable'}
            if cancel.is_set():
                return {'reply': '알았어, 다 멈출게.', 'source': source, 'cancelled': True}
            self.history += [{'who': '사람', 'text': text}, {'who': 'KEVIN', 'text': turn['reply']}]
            self.history = self.history[-20:]
            threading.Thread(target=self._act, args=(turn, cancel), daemon=True).start()
            return {'reply': turn['reply'], 'turn': turn, 'source': source}

    def _act(self, turn, cancel):
        led, music = turn['led'], turn['music']
        live = self.c.live_configured
        def color(v, d):
            v = (v or '').strip().lstrip('#')
            return v.lower() if len(v) == 6 and all(ch in '0123456789abcdefABCDEF' for ch in v) else d
        try:
            check_cancel(cancel)
            if led['mode'] == 'scene':
                plan = {'mode': 'update', 'preset': led['preset'], 'speed': None, 'snow': led['snow'],
                        'sky': led['sky'].lower() if len(led['sky']) == 7 and led['sky'].startswith('#') else 'keep',
                        'music': 'play' if music['mode'] == 'carol' else 'stop' if music['mode'] == 'stop' else 'keep',
                        'volume': music['volume'], 'explanation': turn['reply']}
                if music['mode'] == 'carol':
                    self.c.audio_prepared = False
                self._command('agent', plan, cancel)
                self.led_now = 'scene'
            elif live and led['mode'] == 'text' and led['text'].strip():
                self._invalidate_scene(cancel)
                anim = {'scroll_left': 'scroll_left', 'blink': 'blink', 'snowflake': 'snowflake', 'fade': 'fade'}.get(led['animation'], 'static')
                if anim == 'static' and len(led['text']) > 9:
                    anim = 'scroll_left'
                self.c.transport.command('send_text', cancel, led['text'], animation=anim, speed=70,
                                         color=color(led['color'], 'ffffff'), bg_color=color(led['bg'], None) if led['bg'] not in ('#000000', '') else None)
                self.led_now = 'text: ' + led['text']
            elif live and led['mode'] == 'score':
                self._invalidate_scene(cancel)
                self.c.transport.command('set_scores', cancel, max(0, min(99, led['score_left'])), max(0, min(99, led['score_right'])))
                self.led_now = f"score {led['score_left']}:{led['score_right']}"
            elif live and led['mode'] == 'clear':
                self._invalidate_scene(cancel)
                self.c.transport.clear(cancel)
                self.led_now = 'clear'
            if cancel.is_set() or not live:
                return
            if music['mode'] == 'song' and music['notes']:
                wav = song.melody_wav(music['notes'], music['tempo'] or 120)
                check_cancel(cancel)
                self.c.radio.close()
                self.c.radio.audio_path.parent.mkdir(parents=True, exist_ok=True)
                self.c.radio.audio_path.write_bytes(wav)
                self.c.audio_prepared = True
                check_cancel(cancel)
                self._radio(self.c.radio.play(music['volume'] if music['volume'] is not None else 60, cancel))
            elif music['mode'] == 'carol' and led['mode'] != 'scene':
                self.c.radio.close()
                self.c.audio_prepared = False
                self.c._prepare_audio(cancel)
                check_cancel(cancel)
                self._radio(self.c.radio.play(music['volume'] if music['volume'] is not None else 60, cancel))
            elif music['mode'] == 'stop' and led['mode'] != 'scene':
                self._radio(self.c.radio.stop())
        except Exception as exc:
            self.last_error = f'{type(exc).__name__}: {str(exc)[:120]}'

    def _radio(self, result):
        with self.c.lock:
            self.c.state['radio'] = dict(result or {}, audible_verified=False)

    def _say_plan(self, text, cancel):
        state = self.c.get_state()
        current = dict(state.get('recipe') or {})
        p, source = plan(text, current)
        if cancel.is_set():
            return {'reply': '알았어, 다 멈출게.', 'source': source, 'cancelled': True}
        if p['mode'] == 'unsupported':
            return {'reply': p['explanation'], 'plan': p, 'source': source}
        if p['mode'] == 'stop':
            with self.job_lock:
                check_cancel(cancel)
                self.stop()
            return {'reply': p['explanation'], 'plan': p, 'source': source}
        self._command(text, p, cancel)
        return {'reply': p['explanation'], 'plan': p, 'source': source}

    def guard(self, body):
        mode = body.get('mode', 'away')
        now = time.time()
        if body.get('kind') == 'idle':
            obs = {'mode': 'home', 'devices_on': True, 'idle_seconds': int(body.get('idle_seconds', 0)),
                   'idle_limit': int(body.get('idle_limit', 30)), 'suspicious_streak': 0}
            action, why = guardian.rule(obs)
            if action == 'power_save':
                result = self.stop()
                status = result.get('status', 'unknown')
                if status != 'stopped':
                    return {'action': 'none', 'stop_status': status,
                            'reply': '정지 요청을 전달했어. 아직 정지를 확인 중이야.' if status == 'stopping' else
                                     '정지를 확인하지 못했어. 기기를 확인해줘.'}
                return {'action': action, 'stop_status': status, 'reply': why}
            return {'action': action, 'reply': why}
        if mode != 'away' or now < self.alarm_until:
            return {'action': 'none', 'reply': ''}
        # Observation alone must not interrupt a running user command. Keep
        # its generation so stop/new commands invalidate delayed vision.
        with self.job_lock:
            observed_job = self.job_cancel
        person, what = see_person(body.get('image') or base64.b64encode(self.eye.jpeg() or b'').decode())
        if observed_job.is_set():
            return {'action': 'none', 'reply': '', 'cancelled': True}
        streak = int(body.get('streak', 0))
        if person is False:  # the camera moved, but nobody is there
            return {'action': 'none', 'reply': '', 'seen': what}
        obs = {'mode': 'away', 'suspicious_streak': streak if person is None else max(streak, 2),
               'devices_on': False, 'idle_seconds': 0, 'idle_limit': 0}
        action, why = guardian.rule(obs)
        if action != 'intruder_alert':
            return {'action': 'none', 'reply': ''}
        with self.job_lock:
            if observed_job is not self.job_cancel or observed_job.is_set() or now < self.alarm_until:
                return {'action': 'none', 'reply': '', 'cancelled': True}
            cancel = self._new_job()
            self.alarm_until = now + 60
        result = {'action': action, 'reply': why, 'seen': what}
        def act():
            try:
                if self.c.live_configured:
                    self._invalidate_scene(cancel)
                    check_cancel(cancel)
                    self.c.transport.send(guardian.intruder_scene(), cancel)
                    check_cancel(cancel)
                    self.c._prepare_audio(cancel)
                    check_cancel(cancel)
                    self.c.radio.play(100, cancel)
            except Exception:
                pass
        threading.Thread(target=act, daemon=True).start()
        return result


def make_handler(bridge):
    extra_hosts = {v.strip().lower() for v in os.getenv('KEVIN_BRIDGE_ALLOWED_HOSTS', '').split(',') if v.strip()}
    extra_origins = {v.strip() for v in os.getenv('KEVIN_BRIDGE_ALLOWED_ORIGINS', '').split(',') if v.strip()}
    class Handler(BaseHTTPRequestHandler):
        WEB_ORIGIN = 'https://sergiobuilds.github.io'

        def log_message(self, *a):
            pass

        def _access(self):
            """Reject DNS rebinding and requests from unrelated browser sites."""
            hosts = self.headers.get_all('Host', [])
            try:
                host = urlsplit('http://' + hosts[0]) if len(hosts) == 1 else None
                valid_host = (host is not None and host.hostname
                              and not host.username and not host.password and not host.path
                              and not host.query and not host.fragment and
                              ((host.hostname in ('127.0.0.1', 'localhost', '::1') and
                                (host.port or 80) == self.server.server_port) or hosts[0].lower() in extra_hosts))
            except ValueError:
                valid_host = False
            if not valid_host:
                self._send(403, {'error': 'local bridge Host required'})
                return False
            allowed = {self.WEB_ORIGIN, 'http://' + hosts[0]} | extra_origins
            if hosts[0].lower() in extra_hosts:
                allowed.add('https://' + hosts[0])
            origins = self.headers.get_all('Origin', [])
            if origins:
                if len(origins) != 1 or origins[0] not in allowed:
                    self._send(403, {'error': 'origin not allowed'})
                    return False
                self._allowed_origin = origins[0]
            elif self.headers.get('Sec-Fetch-Site') in ('cross-site', 'same-site'):
                # <img> camera requests do not carry Origin. Require the known
                # web UI's Referer rather than exposing frames to arbitrary sites.
                ref = urlsplit(self.headers.get('Referer', ''))
                if ref.scheme + '://' + ref.netloc not in allowed:
                    self._send(403, {'error': 'browser site not allowed'})
                    return False
            return True

        def _cors(self):
            origin = getattr(self, '_allowed_origin', None)
            if origin:
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Vary', 'Origin')
                self.send_header('Access-Control-Allow-Private-Network', 'true')

        def _send(self, code, data, private=False):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            if private:
                self.send_header('Cache-Control', 'no-store')
            else:
                self._cors()
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            if not self._access():
                return
            if self.path.startswith('/ai/'):
                return self._send(403, {'error': '로컬 앱에서 AI 설정을 열어 주세요.'}, private=True)
            self.send_response(204)
            self._cors()
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
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
            if not self._access():
                return
            if self.path == '/ai/config':
                if not self._ai_access():
                    return
                try:
                    return self._send(200, {'settings': bridge.ai.public(), 'csrf': bridge.ai.csrf}, private=True)
                except SettingsError as error:
                    return self._send(400, {'error': str(error)}, private=True)
            if self.path.startswith('/health'):
                return self._send(200, bridge.health())
            if self.path.startswith('/camera.jpg'):
                data = bridge.eye.jpeg()
                if not data:
                    return self._send(503, {'error': bridge.eye.error or 'camera starting'})
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Cache-Control', 'no-store')
                self._cors()
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path.startswith('/state'):
                s = bridge.c.get_state()
                out = {k: s.get(k) for k in ('status', 'message', 'recipe', 'transport', 'radio')}
                out.update(led_now=getattr(bridge, 'led_now', None), last_error=getattr(bridge, 'last_error', None))
                return self._send(200, out)
            if not self._static():
                self._send(404, {'error': 'not found'})

        def do_POST(self):
            if not self._access():
                return
            if self.headers.get_content_type() != 'application/json':
                return self._send(415, {'error': 'application/json required'})
            if self.path.startswith('/ai/'):
                return self._ai_post()
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 <= n <= 4_000_000:
                    return self._send(413, {'error': 'request too large'})
                body = json.loads(self.rfile.read(n) or b'{}')
                if not isinstance(body, dict):
                    return self._send(400, {'error': 'JSON object required'})
                if self.path.startswith('/say'):
                    return self._send(200, bridge.say(str(body.get('text', ''))[:500]))
                if self.path.startswith('/tts'):
                    audio = tts(str(body.get('text', ''))[:300])
                    if not audio:
                        return self._send(503, {'error': 'voice unavailable'})
                    self.send_response(200)
                    self.send_header('Content-Type', 'audio/mpeg')
                    self._cors()
                    self.send_header('Content-Length', str(len(audio)))
                    self.end_headers()
                    self.wfile.write(audio)
                    return
                if self.path.startswith('/stop'):
                    result = bridge.stop()
                    failed = result.get('status') == 'error'
                    return self._send(503 if failed else 200, dict(result,
                        reply='정지를 확인하지 못했어. 기기를 확인해줘.' if failed else '정지 요청을 전달했어.'))
                if self.path.startswith('/guard'):
                    return self._send(200, bridge.guard(body))
                self._send(404, {'error': 'not found'})
            except Exception as exc:
                self._send(400, {'error': str(exc)[:200], 'reply': '어, 뭔가 꼬였어. 다시 말해줄래?'})

        def _ai_access(self, write=False):
            # Protect the new credential-writing surface from hosted pages and DNS rebinding.
            host = self.headers.get('Host', '')
            try:
                parsed = urllib.parse.urlsplit('http://' + host)
                allowed = {'127.0.0.1', 'localhost', '::1', self.server.server_address[0]}
                valid = (parsed.hostname in allowed and (parsed.port or 80) == self.server.server_port and
                         not parsed.username and not parsed.password and not parsed.path)
            except ValueError:
                valid = False
            origin = self.headers.get('Origin')
            if not valid or origin not in (None, 'http://' + host):
                self._send(403, {'error': '로컬 앱에서 AI 설정을 열어 주세요.'}, private=True)
                return False
            if write and not hmac.compare_digest(self.headers.get('X-KEVIN-Setup', '').encode(), bridge.ai.csrf.encode()):
                self._send(403, {'error': '설정을 다시 열어 주세요.'}, private=True)
                return False
            return True

        def _ai_post(self):
            if not self._ai_access(write=True):
                return
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= 8192 or self.headers.get_content_type() != 'application/json':
                    return self._send(400, {'error': 'AI 설정 요청 형식을 확인하세요.'}, private=True)
                body = json.loads(self.rfile.read(n))
                if not isinstance(body, dict):
                    raise SettingsError('AI 설정 요청 형식을 확인하세요.')
                if self.path == '/ai/test' and set(body) == {'draft'}:
                    return self._send(200, bridge.ai.test(body['draft']), private=True)
                if self.path == '/ai/save' and set(body) == {'draft', 'test_id'}:
                    result = bridge.ai.save(body['draft'], body['test_id'])
                    return self._send(200, {'settings': result}, private=True)
                raise SettingsError('AI 설정 요청 형식을 확인하세요.')
            except SettingsError as error:
                self._send(400, {'error': str(error)}, private=True)
            except Exception:
                self._send(400, {'error': 'AI 설정을 처리하지 못했습니다.'}, private=True)
    return Handler


WARM_LINES = ['안녕, 난 케빈이야. 이 집은 내가 맡을게. 말 걸어줘.', '응, 말 걸어줘.', '다녀와. 집은 내가 지킬게.',
              '아무도 없으면 내가 알아서 끌게.', '알았어, 다 멈출게.', '누구세요? 지금 경찰에 신고했어요!',
              '한동안 아무도 없어서 전광판이랑 라디오 껐어.']


def warm_up():
    """Open the OpenAI/ElevenLabs connections and cache fixed lines so the first request is fast."""
    try:
        if AISettings().public()['configured']:
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
