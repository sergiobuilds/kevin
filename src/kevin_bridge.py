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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # embedded Pythons ignore PYTHONPATH

import guardian
import show_intent

PORT = int(os.getenv('KEVIN_BRIDGE_PORT', '8787'))
OPENAI_URL = 'https://api.openai.com/v1/responses'


def openai_json(system: str, user_content, schema: dict, name: str, timeout=30) -> dict:
    key = os.getenv('OPENAI_API_KEY', '').strip()
    if not key:
        raise RuntimeError('OPENAI_API_KEY not set')
    body = {
        'model': os.getenv('KEVIN_OPENAI_MODEL', 'gpt-4.1-mini'),
        'input': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user_content}],
        'text': {'format': {'type': 'json_schema', 'name': name, 'schema': schema, 'strict': True}},
    }
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
    def __init__(self, controller=None):
        if controller is None:
            from show_control import ShowController
            controller = ShowController()
        self.c = controller
        self.lock = threading.Lock()
        self.alarm_until = 0

    def health(self):
        return {'ok': True, 'live': self.c.live_configured,
                'openai': bool(os.getenv('OPENAI_API_KEY', '').strip()),
                'led': bool(os.getenv('KEVIN_LED_ADDRESS')), 'radio': os.getenv('KEVIN_RADIO_DEVICE') or None}

    def say(self, text):
        with self.lock:
            state = self.c.get_state()
            if state['status'] in ('queued', 'planning', 'rendering', 'sending', 'audio', 'stopping'):
                return {'reply': '잠깐, 아직 하던 거 하는 중이야.', 'busy': True}
            current = dict(state.get('recipe') or {})
            p, source = plan(text, current)
            if p['mode'] == 'unsupported':
                return {'reply': p['explanation'], 'plan': p, 'source': source}
            if p['mode'] == 'stop':
                self.c.stop()
                return {'reply': p['explanation'], 'plan': p, 'source': source}
            self.c.planner = lambda _t, _r, p=p: dict(p)
            try:
                self.c.command(text, self.c.live_configured)
            finally:
                self.c.planner = None
            return {'reply': p['explanation'], 'plan': p, 'source': source}

    def guard(self, body):
        mode = body.get('mode', 'away')
        now = time.time()
        if body.get('kind') == 'idle':
            obs = {'mode': 'home', 'devices_on': True, 'idle_seconds': int(body.get('idle_seconds', 0)),
                   'idle_limit': int(body.get('idle_limit', 30)), 'suspicious_streak': 0}
            action, why = guardian.rule(obs)
            if action == 'power_save':
                self.c.stop()
            return {'action': action, 'reply': why}
        if mode != 'away' or now < self.alarm_until:
            return {'action': 'none', 'reply': ''}
        person, what = see_person(body.get('image', ''))
        streak = int(body.get('streak', 0))
        if person is False:  # the camera moved, but nobody is there
            return {'action': 'none', 'reply': '', 'seen': what}
        obs = {'mode': 'away', 'suspicious_streak': streak if person is None else max(streak, 2),
               'devices_on': False, 'idle_seconds': 0, 'idle_limit': 0}
        action, why = guardian.rule(obs)
        if action != 'intruder_alert':
            return {'action': 'none', 'reply': ''}
        self.alarm_until = now + 60
        cancel = threading.Event()
        result = {'action': action, 'reply': why, 'seen': what}
        def act():
            try:
                if self.c.live_configured:
                    self.c.transport.send(guardian.intruder_scene(), cancel)
                    self.c._prepare_audio(cancel)
                    self.c.radio.play(100, cancel)
            except Exception:
                pass
        threading.Thread(target=act, daemon=True).start()
        return result


def make_handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, data):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
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

        def do_GET(self):
            if self.path.startswith('/health'):
                return self._send(200, bridge.health())
            if self.path.startswith('/state'):
                s = bridge.c.get_state()
                return self._send(200, {k: s.get(k) for k in ('status', 'message', 'recipe', 'transport', 'radio')})
            self._send(404, {'error': 'not found'})

        def do_POST(self):
            try:
                n = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(min(n, 4_000_000)) or b'{}')
                if self.path.startswith('/say'):
                    return self._send(200, bridge.say(str(body.get('text', ''))[:500]))
                if self.path.startswith('/stop'):
                    bridge.c.stop()
                    return self._send(200, {'reply': '알았어, 다 멈출게.'})
                if self.path.startswith('/guard'):
                    return self._send(200, bridge.guard(body))
                self._send(404, {'error': 'not found'})
            except Exception as exc:
                self._send(400, {'error': str(exc)[:200], 'reply': '어, 뭔가 꼬였어. 다시 말해줄래?'})
    return Handler


def main():
    bridge = Bridge()
    server = ThreadingHTTPServer(('127.0.0.1', PORT), make_handler(bridge))
    h = bridge.health()
    print(f"KEVIN bridge  http://127.0.0.1:{PORT}  live={h['live']}  openai={h['openai']}", flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
