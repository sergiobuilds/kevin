"""Guardian mode: KEVIN watches the room on its own and decides what to do.

Every second it looks through the webcam, summarises what it sees, and asks the
on-device model what to do. A rule guard has the final say, so the model can
explain and choose but never trigger an alarm or switch things off on a hunch.

  away  ->  someone appears:   "Home Alone" response - red warning on the LED
                                sign, the radio blasting, KEVIN shouting.
  home  ->  nobody for a while: switch the sign and radio off to save power.

    python src/guardian.py --mode away          # 집을 비웠을 때
    python src/guardian.py --mode home --idle 300
"""
from __future__ import annotations

import argparse
import io
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import pixels

ROOT = Path(__file__).resolve().parent.parent
ACTIONS = ('intruder_alert', 'power_save', 'none')
SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['action', 'why'],
    'properties': {'action': {'type': 'string', 'enum': list(ACTIONS)},
                   'why': {'type': 'string', 'minLength': 1, 'maxLength': 200}},
}
SYSTEM = """You are KEVIN, guarding a home with the devices you can use: an LED sign
and an old Bluetooth radio. You receive one JSON observation from the webcam.
Choose exactly one action:
- intruder_alert: the owner is away and a person or strong movement is seen.
- power_save: the owner is home mode, nobody has been seen for idle_limit seconds,
  and devices are still on.
- none: anything else.
Return JSON {"action":..., "why":...}. why is one short Korean sentence in casual
speech that KEVIN says out loud, e.g. "아무도 없어서 전광판이랑 라디오 껐어."
"""


# ---------- seeing ----------

class Detector:
    """Motion between frames plus OpenCV's built-in pedestrian detector."""

    def __init__(self):
        self.prev = None
        self.hog = None
        try:
            import cv2
            self.hog = cv2.HOGDescriptor()
            self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        except Exception:
            self.hog = None

    def look(self, jpg: bytes) -> dict:
        image = Image.open(io.BytesIO(jpg)).convert('L').resize((320, 180))
        motion = 0.0
        if self.prev is not None:
            from PIL import ImageChops
            diff = ImageChops.difference(self.prev, image).point(lambda v: 255 if v >= 28 else 0)
            motion = diff.histogram()[255] / (320 * 180)
        self.prev = image
        people = 0
        if self.hog is not None:
            import numpy as np
            rects, _ = self.hog.detectMultiScale(np.array(image.resize((640, 360))), winStride=(8, 8))
            people = len(rects)
        return {'motion': round(motion, 4), 'people': people}


# ---------- deciding ----------

def rule(obs: dict) -> tuple[str, str]:
    if obs['mode'] == 'away' and obs['suspicious_streak'] >= 2:
        return 'intruder_alert', '누구세요? 지금 경찰에 신고했어요!'
    if (obs['mode'] == 'home' and obs['devices_on'] and obs['idle_seconds'] >= obs['idle_limit']):
        return 'power_save', '한동안 아무도 없어서 전광판이랑 라디오 껐어.'
    return 'none', ''


def reason(obs: dict, url=None, model=None, timeout=8) -> tuple[str, str, str]:
    """Ask the local model, then let the rule guard decide what may actually run."""
    allowed, fallback_why = rule(obs)
    url = (url or os.getenv('KEVIN_OLLAMA_URL', 'http://127.0.0.1:11434')).rstrip('/') + '/api/chat'
    body = {'model': model or os.getenv('KEVIN_OLLAMA_MODEL', 'qwen2.5:3b'), 'stream': False, 'format': SCHEMA,
            'options': {'temperature': 0, 'num_predict': 120},
            'messages': [{'role': 'system', 'content': SYSTEM},
                         {'role': 'user', 'content': json.dumps(obs, ensure_ascii=False)}]}
    try:
        req = urllib.request.Request(url, json.dumps(body).encode(), {'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(json.loads(r.read())['message']['content'])
        proposed, why = out['action'], out['why'].strip()
        source = 'model'
    except Exception:
        proposed, why, source = allowed, fallback_why, 'rule'
    if proposed != allowed:  # the model may not invent or skip an alarm or a shutdown
        source += '+guard'
    # KEVIN says a fixed line for what actually runs; the model's wording is only logged.
    return allowed, fallback_why, source if not why else f'{source}: {why}'


# ---------- acting ----------

def _font(size):
    for path in ('C:/Windows/Fonts/malgunbd.ttf', 'C:/Windows/Fonts/malgun.ttf',
                 '/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc'):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def intruder_scene() -> dict:
    """Flashing red warning for the 144x16 sign (kept under the panel's frame limit)."""
    frames = []
    for i, (bg, fg, text) in enumerate([('#c00010', '#ffffff', '누구야?!'), ('#000000', '#ff2a3d', '누구야?!'),
                                         ('#c00010', '#ffffff', '경찰 신고'), ('#000000', '#ffd75c', 'KEVIN 집 지킴')]):
        image = Image.new('RGB', (144, 16), bg)
        draw = ImageDraw.Draw(image)
        font = _font(13)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        draw.text(((144 - (right - left)) / 2 - left, (16 - (bottom - top)) / 2 - top), text, font=font, fill=fg)
        px = image.load()
        frames.append([['#%02x%02x%02x' % px[x, y] for x in range(144)] for y in range(16)])
    return pixels.validate({'frames': frames, 'duration': 300})


class Guardian:
    def __init__(self, controller, camera, detector=None, voice=None, *, mode='away', idle_limit=300,
                 receipts=True, clock=time.time):
        self.controller, self.camera = controller, camera
        self.detector = detector or Detector()
        if voice is None:
            from voice_out import Voice
            voice = Voice()
        self.voice, self.mode, self.idle_limit = voice, mode, idle_limit
        self.clock = clock
        self.last_seen = clock()
        self.streak = 0
        self.devices_on = mode == 'home'
        self.alarm_until = 0
        self.receipts = receipts
        self.log = []

    def observe(self) -> tuple[dict, bytes]:
        jpg = self.camera.grab()
        seen = self.detector.look(jpg)
        now = self.clock()
        suspicious = seen['people'] > 0 or seen['motion'] >= 0.08
        if suspicious:
            self.last_seen = now
        self.streak = self.streak + 1 if suspicious else 0
        return dict(seen, mode=self.mode, suspicious_streak=self.streak,
                    idle_seconds=round(now - self.last_seen), idle_limit=self.idle_limit,
                    devices_on=self.devices_on, alarm_active=now < self.alarm_until), jpg

    def step(self, reasoner=reason) -> dict:
        obs, before = self.observe()
        if obs['alarm_active']:
            return {'obs': obs, 'action': 'none', 'why': '', 'source': 'cooldown'}
        action, why, source = reasoner(obs)
        event = {'time': time.strftime('%H:%M:%S'), 'obs': obs, 'action': action, 'why': why, 'source': source}
        if action == 'intruder_alert':
            self.voice.say(why)
            cancel = threading.Event()
            led = self.controller.transport.send(intruder_scene(), cancel)
            try:
                self.controller._prepare_audio(cancel)
                radio = self.controller.radio.play(100, cancel)
            except Exception as exc:
                radio = {'status': 'failed', 'detail': str(exc)[:80]}
            self.devices_on = True
            self.alarm_until = self.clock() + 60
            event.update(led=led, radio=radio)
            if self.receipts:
                event['receipt'] = self._receipt(before, event)
        elif action == 'power_save':
            self.voice.say(why)
            event['stop'] = self.controller.stop()
            self.devices_on = False
        self.log.append(event)
        return event

    def _receipt(self, before, event):
        try:
            from receipt import Signer, make_receipt
            time.sleep(1.0)
            after = [self.camera.grab(), (time.sleep(.6), self.camera.grab())[1]]
            rec = make_receipt(device='home-guard', action={'tool': 'intruder_alert', 'why': event['why']},
                               plan={'obs': event['obs']}, device_reply={'led': event.get('led'), 'radio': event.get('radio')},
                               before=before, after=after, started=self.clock(),
                               out_dir=ROOT / '.runtime' / 'receipts', signer=Signer(ROOT / '.runtime' / 'kevin_ed25519.pem'))
            return {'id': rec['id'], 'path': rec['path'], 'observation': rec['observation']}
        except Exception as exc:
            return {'error': str(exc)[:80]}


def main(argv=None):
    parser = argparse.ArgumentParser(description='KEVIN guardian mode')
    parser.add_argument('--mode', choices=('away', 'home'), default='away')
    parser.add_argument('--idle', type=int, default=300, help='seconds without anyone before saving power')
    parser.add_argument('--interval', type=float, default=1.0)
    args = parser.parse_args(argv)
    from receipt import Camera
    from show_control import ShowController
    guardian = Guardian(ShowController(), Camera(int(os.getenv('KEVIN_CAMERA', '0'))), mode=args.mode, idle_limit=args.idle)
    print(f'KEVIN ▶ 지킴이 모드 시작 ({args.mode}). Ctrl+C로 끝.', flush=True)
    guardian.voice.say('집은 내가 지킬게.' if args.mode == 'away' else '아무도 없으면 내가 알아서 끌게.')
    try:
        while True:
            event = guardian.step()
            o = event['obs']
            line = f"[{time.strftime('%H:%M:%S')}] 사람 {o['people']} · 움직임 {o['motion']:.3f} · 빈 시간 {o['idle_seconds']}s"
            if event['action'] != 'none':
                line += f"  →  {event['action']} ({event['source']})  KEVIN ▶ {event['why']}"
            print(line, flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
