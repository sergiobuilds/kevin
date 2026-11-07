import io
import time
import unittest

from PIL import Image, ImageDraw

import guardian


def jpg(person=False):
    image = Image.new('RGB', (320, 180), (20, 20, 20))
    if person:
        ImageDraw.Draw(image).rectangle((120, 20, 200, 170), fill=(230, 230, 230))
    out = io.BytesIO()
    image.save(out, 'JPEG')
    return out.getvalue()


class Cam:
    def __init__(self, frames):
        self.frames = list(frames)

    def grab(self):
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]


class Transport:
    def __init__(self):
        self.sent = []

    def send(self, scene, cancel):
        self.sent.append(scene)
        return {'status': 'acknowledged'}


class Radio:
    def __init__(self):
        self.played = []

    def play(self, volume, cancel):
        self.played.append(volume)
        return {'status': 'playing'}


class Controller:
    def __init__(self):
        self.transport, self.radio, self.stops = Transport(), Radio(), 0

    def _prepare_audio(self, cancel):
        pass

    def stop(self):
        self.stops += 1
        return {'status': 'stopped'}


class Voice:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def no_model(obs):
    action, why = guardian.rule(obs)
    return action, why, 'rule'


class GuardianTests(unittest.TestCase):
    def make(self, frames, mode, idle=300):
        self.ctrl, self.voice, self.clock = Controller(), Voice(), Clock()
        return guardian.Guardian(self.ctrl, Cam(frames), voice=self.voice, mode=mode, idle_limit=idle,
                                 receipts=False, clock=self.clock)

    def test_intruder_needs_two_suspicious_looks_then_home_alone_response(self):
        g = self.make([jpg(), jpg(True), jpg(), jpg(True)], 'away')
        actions = [g.step(no_model)['action'] for _ in range(3)]
        self.assertEqual(actions, ['none', 'none', 'intruder_alert'])
        self.assertEqual(len(self.ctrl.transport.sent), 1)
        self.assertEqual(self.ctrl.radio.played, [100])
        self.assertIn('경보', self.voice.said[0])
        self.assertNotIn('신고했어요', self.voice.said[0])

    def test_alarm_does_not_repeat_during_cooldown(self):
        g = self.make([jpg(), jpg(True), jpg(), jpg(True), jpg(), jpg(True)], 'away')
        for _ in range(5):
            g.step(no_model)
        self.assertEqual(len(self.ctrl.transport.sent), 1)

    def test_quiet_home_without_presence_confirmation_keeps_devices_on(self):
        g = self.make([jpg()], 'home', idle=60)
        self.assertEqual(g.step(no_model)['action'], 'none')
        self.clock.t += 61
        self.assertEqual(g.step(no_model)['action'], 'none')
        self.assertEqual(self.ctrl.stops, 0)

    def test_confirmed_empty_home_switches_devices_off_once(self):
        g = self.make([jpg()], 'home', idle=60)
        class ConfirmedEmpty:
            def look(self, _):
                return {'motion': 0, 'people': 0, 'presence': False}
        g.detector = ConfirmedEmpty()
        for _ in range(6):
            self.assertEqual(g.step(no_model)['action'], 'none')
            self.clock.t += 10
        self.assertEqual(g.step(no_model)['action'], 'power_save')
        self.clock.t += 61
        self.assertEqual(g.step(no_model)['action'], 'none')
        self.assertEqual(self.ctrl.stops, 1)

    def test_model_cannot_raise_a_false_alarm(self):
        g = self.make([jpg()], 'home')
        event = g.step(lambda obs: guardian.reason(obs, url='http://127.0.0.1:9', timeout=.2))
        self.assertEqual(event['action'], 'none')
        self.assertEqual(self.ctrl.transport.sent, [])

    def test_intruder_scene_fits_the_panel(self):
        scene = guardian.intruder_scene()
        self.assertLessEqual(len(scene['frames']), 12)
        self.assertEqual(len(scene['frames'][0]), 16)


if __name__ == '__main__':
    unittest.main()
