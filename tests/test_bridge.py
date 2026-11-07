"""Web job/camera/presence regressions, without model, camera, BLE or audio."""
import copy
import io
import tempfile
import threading
import time
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import kevin_bridge
from bridge_runtime import FINAL
from show_control import ShowController
from show_transport import Cancelled, LedTransport
from test_show_control import FakeRadio, FakeTransport, plan, small_scene


def turn(led='keep', music='keep', volume=None):
    return {'reply': '처리할게.', 'led': {'mode': led, 'text': 'HELLO', 'color': '#FFFFFF',
        'bg': '#000000', 'animation': 'static', 'preset': 'santa', 'snow': 'keep',
        'sky': 'keep', 'score_left': 1, 'score_right': 0},
        'music': {'mode': music, 'title': '', 'tempo': 120,
                  'notes': [{'n': 'C4', 'b': 1}] if music == 'song' else [], 'volume': volume},
        'rps': {'human': '', 'kevin': ''}}


class Transport(FakeTransport):
    def command(self, name, cancel, *args, **kwargs):
        if cancel.is_set(): raise Cancelled()
        self.calls.append((name, args, kwargs))
        return {'status': 'acknowledged'}


class Eye:
    error = None
    camera_name = 'fixture'
    source = 'simulation'
    def __init__(self, clock=time.monotonic):
        self.clock, self.available, self.sequence = clock, False, 0
    def snapshot(self):
        if not self.available: return None
        self.sequence += 1
        out = io.BytesIO()
        Image.new('RGB', (80, 60), (self.sequence % 255, 20, 20)).save(out, 'JPEG')
        return {'image': out.getvalue(), 'captured_at': self.clock(), 'source': 'simulation'}
    def jpeg(self):
        frame = self.snapshot()
        return frame['image'] if frame else None


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.led, self.radio, self.eye = Transport(), FakeRadio(), Eye()
        self.radio.audio_path = Path(self.temp.name) / 'jingle_bells.wav'
        config = dict(KEVIN_SHOW_LIVE='1', KEVIN_LED_ADDRESS='fixture', KEVIN_RADIO_DEVICE='CRF-35B',
                      KEVIN_SHOW_DATA_DIR=self.temp.name)
        self.c = ShowController(transport=self.led, radio=self.radio, config=config, renderer=small_scene)
        self.c._prepare_audio = lambda cancel: None
        self.addCleanup(self.c.close)
        self.bridge = kevin_bridge.Bridge(self.c, self.eye, out_dir=Path(self.temp.name) / 'evidence',
            agent_fn=lambda *_: turn('text'), plan_fn=lambda *_: (plan(mode='unsupported'), 'fixture'),
            vision_fn=lambda *_: (None, ''))

    def wait(self, request):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            job = self.bridge.state(request['job_id'])['job']
            if job['status'] in FINAL: return job
            time.sleep(.005)
        self.fail('job did not finish: ' + str(job))

    def test_stop_while_agent_plans_prevents_late_device_action(self):
        started, release, returned = threading.Event(), threading.Event(), threading.Event()
        def agent(*_):
            started.set(); release.wait(2); returned.set(); return turn('text', 'carol')
        self.bridge.agent_fn = agent
        old = self.bridge.say('text and music')
        self.assertTrue(started.wait(1))
        self.wait(self.bridge.stop())
        release.set(); self.assertTrue(returned.wait(1)); time.sleep(.05)
        self.assertEqual(self.wait(old)['status'], 'stopped')
        self.assertFalse(any(call[0] in ('send_text', 'send') for call in self.led.calls))
        self.assertFalse(any(call[0] == 'play' for call in self.radio.calls))
        self.assertEqual(self.bridge.history, [])

    def test_stop_while_fallback_plans_prevents_late_command(self):
        started, release = threading.Event(), threading.Event()
        self.bridge.agent_fn = lambda *_: (_ for _ in ()).throw(RuntimeError())
        def fallback(*_):
            started.set(); release.wait(2); return plan(preset='santa'), 'fixture'
        self.bridge.plan_fn = fallback
        old = self.bridge.say('scene'); self.assertTrue(started.wait(1))
        self.wait(self.bridge.stop()); release.set(); time.sleep(.05)
        self.assertEqual(self.wait(old)['status'], 'stopped')
        self.assertFalse(any(call[0] == 'send' for call in self.led.calls))

    def test_new_request_replaces_slow_planner_without_waiting_for_it(self):
        started, release = threading.Event(), threading.Event()
        def agent(text, *_):
            if text == 'old': started.set(); release.wait(2)
            result = turn('text'); result['led']['text'] = text; return result
        self.bridge.agent_fn = agent
        old = self.bridge.say('old'); self.assertTrue(started.wait(1))
        new = self.wait(self.bridge.say('new')); release.set(); time.sleep(.05)
        self.assertEqual(new['status'], 'complete')
        self.assertEqual(self.wait(old)['status'], 'stopped')
        self.assertEqual([x[1][0] for x in self.led.calls if x[0] == 'send_text'], ['new'])

    def test_guard_stop_cancels_followup_radio(self):
        entered, release = threading.Event(), threading.Event()
        self.eye.available = True
        self.bridge.set_mode('away')
        self.bridge.vision_fn = lambda *_: (True, 'fixture person')
        def send(scene, cancel):
            entered.set(); release.wait(2)
            return {'status': 'acknowledged'}
        self.led.send = send
        alarm = self.bridge.guard({'mode': 'away', 'kind': 'motion'})
        self.assertTrue(entered.wait(1))
        self.wait(self.bridge.stop()); release.set(); time.sleep(.05)
        self.assertEqual(self.wait(alarm)['status'], 'stopped')
        self.assertFalse(any(c[0] == 'play' for c in self.radio.calls))

    def test_mode_change_discards_late_vision_action(self):
        entered, release = threading.Event(), threading.Event()
        self.eye.available = True; self.bridge.set_mode('away')
        def vision(*_): entered.set(); release.wait(2); return True, 'person'
        self.bridge.vision_fn = vision
        replies = []
        thread = threading.Thread(target=lambda: replies.append(self.bridge.guard({'mode':'away'})))
        thread.start(); self.assertTrue(entered.wait(1)); self.bridge.set_mode('talk'); release.set(); thread.join(2)
        self.assertEqual(replies[0]['action'], 'none')
        self.assertEqual(self.led.calls, [])

    def test_failure_keeps_completed_led_and_failed_radio(self):
        self.bridge.agent_fn = lambda *_: turn('text', 'carol')
        self.radio.play = lambda *_: (_ for _ in ()).throw(RuntimeError('private details'))
        job = self.wait(self.bridge.say('both'))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['device_results']['led']['status'], 'acknowledged')
        self.assertEqual(job['device_results']['radio']['status'], 'failed_unknown')
        self.assertNotIn('private details', str(job))

    def test_volume_only_zero_does_not_play_and_song_zero_stays_muted(self):
        self.radio.state['status'] = 'paused'
        self.bridge.agent_fn = lambda *_: turn(volume=0)
        self.assertEqual(self.wait(self.bridge.say('mute'))['status'], 'complete')
        self.assertEqual(self.radio.calls, [('volume', 0)])
        self.radio.calls.clear(); self.bridge.agent_fn = lambda *_: turn(music='song', volume=0)
        with patch('song.melody_wav', return_value=b'fixture wav'):
            self.assertEqual(self.wait(self.bridge.say('song'))['status'], 'complete')
        self.assertIn(('play', 0), self.radio.calls)

    def test_scene_and_song_do_not_double_play_carol(self):
        self.bridge.agent_fn = lambda *_: turn('scene', 'song', 20)
        with patch('song.melody_wav', return_value=b'fixture wav'):
            job = self.wait(self.bridge.say('both'))
        self.assertEqual(job['status'], 'complete')
        self.assertEqual(len([c for c in self.led.calls if c[0] == 'send']), 1)
        self.assertEqual([c for c in self.radio.calls if c[0] == 'play'], [('play', 20)])

    def test_missing_requested_radio_fails_before_led_effect(self):
        self.c.config.pop('KEVIN_RADIO_DEVICE')
        self.bridge.agent_fn = lambda *_: turn('text', 'carol')
        self.assertEqual(self.wait(self.bridge.say('both'))['status'], 'failed')
        self.assertEqual(self.led.calls, [])

    def test_preview_does_not_send_or_play(self):
        self.c.config['KEVIN_SHOW_LIVE'] = '0'
        self.bridge.agent_fn = lambda *_: turn('text', 'song')
        self.assertEqual(self.wait(self.bridge.say('preview'))['status'], 'preview')
        self.assertEqual(self.led.calls, []); self.assertEqual(self.radio.calls, [])

    def test_fallback_stop_exposes_final_stop_result_on_same_job(self):
        self.bridge.agent_fn = lambda *_: (_ for _ in ()).throw(RuntimeError())
        self.bridge.plan_fn = lambda *_: (plan(mode='stop'), 'fixture')
        self.assertEqual(self.wait(self.bridge.say('stop'))['status'], 'stopped')

    def test_stop_failure_is_not_success(self):
        self.radio.stop = lambda: (_ for _ in ()).throw(RuntimeError())
        self.assertEqual(self.wait(self.bridge.stop())['status'], 'failed')

    def test_evidence_is_published_before_terminal_completion(self):
        self.eye.available = True
        job = self.wait(self.bridge.say('text'))
        self.assertEqual(job['status'], 'complete')
        self.assertEqual(job['observation']['source'], 'simulation')
        self.assertEqual(job['observation']['scope'], 'visual_change_only')
        self.assertTrue(job['receipt']['integrity_verified'])
        self.assertFalse(job['physically_verified'])

    def test_cancel_during_evidence_never_publishes_late_receipt(self):
        self.eye.available = True
        entered, release = threading.Event(), threading.Event()
        from receipt import make_receipt
        def delayed(**kwargs):
            entered.set(); release.wait(2); return make_receipt(**kwargs)
        with patch('bridge_runtime.make_receipt', side_effect=delayed):
            request = self.bridge.say('text'); self.assertTrue(entered.wait(2))
            self.wait(self.bridge.stop()); release.set(); time.sleep(.05)
        job = self.wait(request)
        self.assertEqual(job['status'], 'stopped'); self.assertIsNone(job['receipt'])

    def home(self):
        self.now = 100.0
        self.bridge.clock = lambda:self.now
        self.eye.clock = self.bridge.clock; self.eye.available = True
        self.bridge.set_mode('home')
        self.c.state['transport'] = {'status':'acknowledged'}
        self.bridge.vision_fn = lambda *_: (False, 'no person')
    def observe(self, seconds, person=False):
        self.now += seconds; self.bridge.vision_fn = lambda *_:(person, '')
        return self.bridge.guard({'mode':'home', 'kind':'idle', 'idle_seconds':100000})

    def test_motionless_person_and_client_idle_never_trigger_shutdown(self):
        self.home()
        for _ in range(8): self.assertEqual(self.observe(10, True)['action'], 'none')
        self.assertEqual(self.radio.calls, [])

    def test_only_continuous_fresh_absence_triggers_stop(self):
        self.home()
        for _ in range(3): self.assertEqual(self.observe(10)['action'], 'none')
        result = self.observe(10)
        self.assertEqual(result['action'], 'power_save')
        self.assertEqual(self.wait(result)['status'], 'stopped')

    def test_unknown_or_gap_resets_absence_period(self):
        self.home(); self.observe(1); self.observe(10); self.observe(10)
        self.assertEqual(self.observe(1, None)['action'], 'unavailable')
        self.assertEqual(self.observe(1)['action'], 'none')
        self.assertEqual(self.observe(30)['action'], 'none')
        self.assertEqual(self.radio.calls, [])

    def test_stale_camera_is_not_accepted_from_client_image(self):
        self.home(); self.eye.available = False
        with patch.object(self.bridge, 'vision_fn') as vision:
            result=self.bridge.guard({'mode':'home','image':'client supplied','idle_seconds':100000})
        self.assertEqual(result['action'], 'unavailable'); vision.assert_not_called()

    def test_invalid_agent_action_cannot_touch_devices(self):
        bad=turn('text');bad['music']['volume']=-10
        self.bridge.agent_fn=lambda *_:bad
        self.assertEqual(self.wait(self.bridge.say('invalid'))['status'], 'unsupported')
        self.assertEqual(self.led.calls, [])


class EyeTests(unittest.TestCase):
    def test_uncertain_or_malformed_vision_is_never_confirmed_absence(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'fixture-not-a-key'}):
            for value in (None, '', 0, 'false', [], {}):
                with self.subTest(value=value), patch('kevin_bridge.openai_json', return_value={'person': value, 'what': ''}):
                    self.assertEqual(kevin_bridge.see_person('fixture'), (None, ''))
            with patch('kevin_bridge.openai_json', return_value={'person': False, 'what': 'empty'}):
                self.assertEqual(kevin_bridge.see_person('fixture'), (False, 'empty'))

    def test_failed_or_stale_frame_cannot_be_returned(self):
        eye=kevin_bridge.Eye.__new__(kevin_bridge.Eye)
        eye.lock=threading.Lock();eye.frame=b'fixture';eye.error=None;eye.captured_at=time.monotonic()-3
        self.assertIsNone(eye.jpeg())
        eye.captured_at=time.monotonic();self.assertEqual(eye.jpeg(), b'fixture')
        eye._invalidate('capture failed');self.assertIsNone(eye.jpeg());self.assertIsNone(eye.frame)

    def test_native_led_command_is_cancellable_without_waiting_for_timeout(self):
        import asyncio
        transport=LedTransport('fixture'); entered=threading.Event();cancel=threading.Event();out=[]
        async def pending(*_):
            entered.set();await asyncio.sleep(30)
        transport._command=pending
        def execute():
            try:transport.command('send_text',cancel,'test')
            except Cancelled:out.append('cancelled')
        worker=threading.Thread(target=execute);worker.start();self.assertTrue(entered.wait(1))
        cancel.set();worker.join(1)
        self.assertFalse(worker.is_alive());self.assertEqual(out,['cancelled'])
