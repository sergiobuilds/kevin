"""Controller/effect contracts. Windows and BLE fixtures are explicitly simulated."""
import asyncio
import base64
import copy
import io
import importlib.util
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import oba_christmas_show
import pixels
from show_control import ACTIVE, ShowController, ShowError, render_recipe
from show_transport import Cancelled, LedTransport, RadioPlayer, TransportError


def plan(**changes):
    return dict(dict(mode='update', preset='keep', speed=None, snow='keep', sky='keep',
                     music='keep', volume=None, explanation='테스트 모델의 해석'), **changes)


def small_scene(recipe=None):
    return {'frames': [[['#123456'] * 144 for _ in range(16)]], 'duration': 120}


class FakeTransport:
    def __init__(self):
        self.calls = []

    def send(self, scene, cancel):
        if cancel.is_set():
            raise Cancelled()
        pixels.validate(scene)
        self.calls.append(('send', copy.deepcopy(scene)))
        return {'status': 'acknowledged', 'physical_verified': False}

    def clear(self, cancel):
        self.calls.append(('clear',))
        return {'status': 'clear_acknowledged', 'physical_verified': False}


class FakeRadio:
    def __init__(self):
        self.calls = []
        self.state = {'status': 'not_started', 'audible_verified': False}

    def play(self, volume, cancel):
        if cancel.is_set():
            raise Cancelled()
        self.calls.append(('play', volume))
        self.state = {'status': 'playing', 'audible_verified': False}
        return self.state.copy()

    def stop(self):
        self.calls.append(('stop',))
        self.state = {'status': 'stopped', 'audible_verified': False}
        return self.state.copy()

    def status(self):
        return self.state.copy()

    def close(self):
        self.calls.append(('close',))


def finished(controller):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = controller.get_state()
        if state['status'] not in ACTIVE:
            return state
        time.sleep(.005)
    raise AssertionError('job did not finish: ' + controller.get_state()['status'])


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.transport = FakeTransport()
        self.radio = FakeRadio()
        self.config = dict(TERMINATOR_SHOW_LIVE='1', TERMINATOR_LED_ADDRESS='test-only-target',
                           TERMINATOR_RADIO_DEVICE='CRF-35B', TERMINATOR_SHOW_DATA_DIR=self.temp.name)

    def controller(self, **kwargs):
        options = dict(planner=lambda *_: plan(), transport=self.transport,
                       radio=self.radio, renderer=small_scene, config=self.config)
        options.update(kwargs)
        controller = ShowController(**options)
        self.addCleanup(controller.close)
        return controller

    def test_preview_has_no_network_device_audio_or_file_effects(self):
        controller = self.controller(planner=lambda *_: plan(mode='start', music='play'))
        forbidden = Mock(side_effect=AssertionError('preview touched hardware'))
        with patch('subprocess.Popen', forbidden), patch('socket.socket', forbidden):
            result = controller.command('크리스마스 기차와 캐럴', False)
            self.assertEqual(result['status'], 'queued')
            state = finished(controller)
        self.assertEqual(state['status'], 'preview')
        self.assertEqual(state['recipe']['music'], 'play')
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.radio.calls, [])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        self.assertFalse(state['transport']['physical_verified'])

    def test_live_applies_led_before_radio_and_keeps_actual_state(self):
        controller = self.controller(planner=lambda *_: plan(mode='start', music='play', volume=37))
        controller._prepare_audio = lambda cancel: None
        original = self.radio.play
        def play_after_ack(volume, cancel):
            self.assertEqual(self.transport.calls[0][0], 'send')
            return original(volume, cancel)
        self.radio.play = play_after_ack
        controller.command('시작', True)
        state = finished(controller)
        self.assertEqual(state['status'], 'complete')
        self.assertEqual(self.radio.calls, [('play', 37)])
        self.assertEqual(state['transport']['status'], 'acknowledged')
        self.assertEqual(state['radio']['status'], 'playing')
        self.assertFalse(state['radio']['audible_verified'])

    def test_failed_transfer_invalidates_previous_scene(self):
        controller = self.controller()
        original = self.transport.send
        first, second = small_scene(), small_scene()
        second['frames'][0][0][0] = '#abcdef'
        controller.command('A', True, first)
        self.assertEqual(finished(controller)['status'], 'complete')
        self.transport.send = Mock(side_effect=TransportError('partial transfer'))
        controller.command('B', True, second)
        self.assertEqual(finished(controller)['status'], 'error')
        self.transport.send = original
        controller.command('A again', True, first)
        self.assertEqual(finished(controller)['status'], 'complete')
        self.assertEqual(len(self.transport.calls), 2)

    def test_manual_send_invalidates_cache_and_rejects_active_show(self):
        controller = self.controller()
        controller.command('A', True)
        self.assertEqual(finished(controller)['status'], 'complete')
        controller.send_manual_scene(small_scene(), 'test-only-target', True)
        controller.command('A again', True)
        self.assertEqual(finished(controller)['status'], 'complete')
        self.assertEqual(len(self.transport.calls), 3)
        with controller.lock:
            controller.state['status'] = 'planning'
        with self.assertRaises(ShowError):
            controller.send_manual_scene(small_scene(), 'test-only-target', True)
        self.assertEqual(len(self.transport.calls), 3)

    def test_failed_geometry_or_ack_never_starts_music(self):
        for failure in ('geometry', 'ack'):
            with self.subTest(failure=failure):
                controller = self.controller(planner=lambda *_: plan(music='play'))
                self.transport.send = Mock(side_effect=TransportError(failure))
                controller.command('시작', True)
                state = finished(controller)
                self.assertEqual(state['status'], 'error')
                self.assertEqual(state['transport']['status'], 'failed_unknown')
                self.assertNotIn(('play', 45), self.radio.calls)

    def test_manual_transfer_is_cancelled_by_stop(self):
        entered, errors = threading.Event(), []
        def send(scene, cancel):
            entered.set()
            cancel.wait(1)
            raise Cancelled()
        self.transport.send = send
        controller = self.controller()
        def manual():
            try:
                controller.send_manual_scene(small_scene(), 'test-only-target', True)
            except ShowError as error:
                errors.append(str(error))
        thread = threading.Thread(target=manual)
        thread.start()
        self.assertTrue(entered.wait(1))
        controller.stop()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(finished(controller)['status'], 'stopped')
        self.assertEqual(errors, ['manual transfer cancelled'])
        self.assertEqual(self.transport.calls, [('clear',)])
        self.assertEqual(self.radio.calls, [('stop',)])

    def test_stop_does_not_wait_for_planner_or_allow_late_effects(self):
        entered, release = threading.Event(), threading.Event()
        def planner(*_):
            entered.set()
            release.wait(2)
            return plan(mode='start', music='play')
        controller = self.controller(planner=planner)
        controller.command('시작', True)
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        controller.stop()
        self.assertLess(time.monotonic() - started, .3)
        self.assertEqual(finished(controller)['status'], 'stopped')
        controller.planner = lambda *_: plan(sky='#445566')
        controller.command('새 미리보기', False)
        self.assertEqual(finished(controller)['status'], 'preview')
        release.set()
        time.sleep(.03)
        self.assertEqual(controller.get_state()['recipe']['sky'], '#445566')
        self.assertEqual([call[0] for call in self.transport.calls], ['clear'])
        self.assertEqual(self.radio.calls, [('stop',)])

    def test_stop_during_transport_prevents_music_and_clears(self):
        entered = threading.Event()
        def send(scene, cancel):
            entered.set()
            cancel.wait(1)
            raise Cancelled()
        self.transport.send = send
        controller = self.controller(planner=lambda *_: plan(music='play'))
        controller.command('시작', True)
        self.assertTrue(entered.wait(1))
        controller.stop()
        self.assertEqual(finished(controller)['status'], 'stopped')
        self.assertEqual(self.radio.calls, [('stop',)])
        self.assertEqual(self.transport.calls, [('clear',)])

    def test_partial_edits_preserve_recipe_and_manual_pixels(self):
        specs = iter([plan(mode='start', snow='heavy', sky='#102030', speed=2),
                      plan(volume=30), plan(speed=1)])
        renderer = Mock(side_effect=small_scene)
        controller = self.controller(planner=lambda *_: next(specs), renderer=renderer)
        controller.command('기차 눈 많이 빠르게', False)
        first = finished(controller)
        current = copy.deepcopy(first['scene'])
        current['frames'][0][5][8] = '#abcdef'
        current['duration'] = 60
        controller.command('음량만 30', False, current)
        second = finished(controller)
        controller.command('속도만 1', False)
        third = finished(controller)
        self.assertEqual(second['recipe']['snow'], 'heavy')
        self.assertEqual(second['recipe']['sky'], '#102030')
        self.assertEqual(third['recipe']['volume'], 30)
        self.assertEqual(third['scene']['frames'], current['frames'])
        self.assertEqual(third['scene']['duration'], 120)
        self.assertEqual(renderer.call_count, 1)

    def test_music_only_keeps_acknowledged_scene_without_resending(self):
        specs = iter([plan(), plan(music='play')])
        controller = self.controller(planner=lambda *_: next(specs))
        controller._prepare_audio = lambda cancel: None
        controller.command('기차', True)
        first = finished(controller)
        controller.command('음악만', True)
        second = finished(controller)
        self.assertEqual(second['revision'], first['revision'])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.radio.calls, [('play', 45)])

    def test_partial_edits_do_not_rewrite_open_carol_file(self):
        controller = self.controller()
        with patch('oba_christmas_show.jingle_bells_wav', return_value=b'owned-test-wav') as synthesize:
            controller._prepare_audio(threading.Event())
            first = (Path(self.temp.name) / 'jingle_bells.wav').stat().st_mtime_ns
            controller._prepare_audio(threading.Event())
            self.assertEqual(synthesize.call_count, 1)
        self.assertEqual((Path(self.temp.name) / 'jingle_bells.wav').stat().st_mtime_ns, first)

    def test_live_gate_and_busy_gate(self):
        controller = self.controller(config={})
        with self.assertRaises(ShowError):
            controller.command('시작', True)
        entered, release = threading.Event(), threading.Event()
        def wait(*_):
            entered.set()
            release.wait(1)
            return plan()
        controller.planner = wait
        controller.command('미리보기', False)
        self.assertTrue(entered.wait(1))
        with self.assertRaises(ShowError):
            controller.command('추가 명령', False)
        release.set()
        finished(controller)

    def test_network_error_is_visible_without_fallback_or_secret(self):
        controller = self.controller(planner=Mock(side_effect=RuntimeError('secret-token test-only-target')))
        controller.command('기차 시작', False)
        state = finished(controller)
        self.assertEqual(state['status'], 'error')
        self.assertNotIn('secret-token', json.dumps(state))
        self.assertNotIn('test-only-target', json.dumps(state))
        self.assertNotIn('scene', state)
        self.assertEqual(self.transport.calls, [])

    def test_unsupported_and_stop_preview_never_dispatch(self):
        controller = self.controller(planner=lambda *_: plan(mode='unsupported'))
        controller.command('불가능한 요청', False)
        self.assertEqual(finished(controller)['status'], 'unsupported')
        controller.planner = lambda *_: plan(mode='stop', music='stop')
        controller.command('전체 정지', False)
        self.assertEqual(finished(controller)['status'], 'preview')
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.radio.calls, [])

    def test_transcription_boundaries_and_error_state(self):
        transcriber = Mock(return_value={'text': '기차 시작', 'language': 'ko'})
        controller = self.controller(transcriber=transcriber)
        audio = base64.b64encode(b'fake-test-audio').decode()
        self.assertEqual(controller.transcribe(audio, 'audio/webm;codecs=opus')['text'], '기차 시작')
        for bad in ('not base64', '', base64.b64encode(b'x' * (2 * 1024 * 1024 + 1)).decode()):
            with self.assertRaises(ShowError):
                controller.transcribe(bad, 'audio/webm')
        self.assertEqual(transcriber.call_count, 1)
        transcriber.side_effect = RuntimeError('private ASR token')
        with self.assertRaises(ShowError):
            controller.transcribe(audio, 'audio/webm')
        self.assertNotIn('private', controller.get_state()['speech_error'])
        self.assertEqual(self.transport.calls, [])

    def test_clear_failure_is_visible(self):
        controller = self.controller()
        self.transport.clear = Mock(side_effect=TransportError('failed'))
        controller.stop()
        self.assertEqual(finished(controller)['transport']['status'], 'clear_failed_unknown')
        self.assertEqual(controller.get_state()['status'], 'error')


class RenderTests(unittest.TestCase):
    def test_parameterized_snow_sky_and_exact_timing(self):
        original = oba_christmas_show.christmas_scene()
        self.assertEqual(original, oba_christmas_show.christmas_scene(snow='normal', sky=None))
        none = oba_christmas_show.christmas_scene(snow='none', sky='#120826')
        heavy = oba_christmas_show.christmas_scene(snow='heavy', sky='#120826')
        self.assertNotEqual(none, heavy)
        self.assertEqual(none['frames'][0][0][0], '#120826')
        for speed in (.5, .75, 1, 1.3, 2):
            recipe = dict(preset='christmas_train', snow='none', sky=None, speed=speed)
            scene = render_recipe(recipe)
            self.assertEqual(scene['duration'] % 10, 0)
            self.assertTrue(40 <= scene['duration'] <= 5000)
        santa = render_recipe(dict(preset='santa', snow='none', sky='#113355', speed=2))
        self.assertEqual((len(santa['frames']), santa['duration']), (32, 60))
        self.assertEqual(santa['frames'][0][0][0], '#113355')


@unittest.skipUnless(importlib.util.find_spec('pypixelcolor'), 'optional pypixelcolor required')
class BleContractTests(unittest.TestCase):
    """Real pinned encoder, fake low-level BLE session: no physical claims."""
    def session(self, *, width=144, device_type=135, ack=True, connect_error=False):
        owner = self
        class Session:
            async def connect(self):
                owner.events.append('connect')
                if connect_error:
                    raise RuntimeError('partial connection')
            def get_device_info(self):
                return SimpleNamespace(width=width, height=16, device_type=device_type)
            async def execute_command(self, command, *args):
                owner.events.append(command.__name__)
                if command.__name__ == 'prepared_image_plan':
                    encoded = command(*args, device_info=self.get_device_info())
                    payload = b''.join(window.data[15:] for window in encoded.windows)
                    owner.assertEqual(payload, args[0])
                return SimpleNamespace(success=ack)
            async def disconnect(self):
                owner.events.append('disconnect')
        return Session()

    def setUp(self):
        self.events = []

    def test_geometry_type_ack_and_partial_connect(self):
        for options in ({'width': 64}, {'device_type': 999}, {'ack': False}, {'connect_error': True}):
            with self.subTest(options=options):
                self.events.clear()
                transport = LedTransport('test-only', session_factory=lambda _: self.session(**options))
                with self.assertRaises(TransportError):
                    transport.send(small_scene(), threading.Event())
                self.assertEqual(self.events[-1], 'disconnect')
                self.assertNotIn('prepared_image_plan', self.events)

    def test_exact_prepared_bytes_and_clear(self):
        transport = LedTransport('test-only', session_factory=lambda _: self.session())
        self.assertEqual(transport.send(small_scene(), threading.Event())['status'], 'acknowledged')
        self.assertEqual(transport.clear(threading.Event())['status'], 'clear_acknowledged')
        self.assertEqual(self.events, ['connect', 'set_brightness', 'prepared_image_plan', 'disconnect'] * 2)

    def test_cancel_pending_connection_closes_session(self):
        entered = threading.Event()
        owner = self
        class Session:
            async def connect(self):
                entered.set()
                await asyncio.Event().wait()
            async def disconnect(self):
                owner.events.append('disconnect')
        transport = LedTransport('test-only', session_factory=lambda _: Session())
        cancel = threading.Event()
        errors = []
        def send():
            try:
                transport.send(small_scene(), cancel)
            except Cancelled:
                errors.append('cancelled')
        thread = threading.Thread(target=send)
        thread.start()
        self.assertTrue(entered.wait(1))
        cancel.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['cancelled'])
        self.assertEqual(self.events, ['disconnect'])

    def test_real_session_honors_negotiated_mtu_and_partial_cleanup(self):
        events, sizes = [], []
        class FakeBle:
            mtu_size = 23
            def __init__(self, address, disconnected_callback):
                self.callback = None
                self.disconnected = disconnected_callback
                self.remaining = 0
            async def connect(self): events.append('connect')
            async def disconnect(self):
                events.append('disconnect')
                self.disconnected(self)
            async def start_notify(self, characteristic, callback): self.callback = callback
            async def stop_notify(self, characteristic): pass
            async def write_gatt_char(self, characteristic, data, response):
                data = bytes(data)
                sizes.append(len(data))
                if len(data) == 8 and data[:4] == bytes([8, 0, 1, 128]):
                    self.callback(None, bytes([0, 0, 0, 0, 135]))
                    return
                if len(data) == 5 and data[2:4] == bytes([4, 128]):
                    self.callback(None, bytes([5, 0, 0, 0, 0]))
                    return
                if not self.remaining:
                    self.remaining = int.from_bytes(data[:2], 'little')
                self.remaining -= len(data)
                if self.remaining == 0:
                    self.callback(None, bytes([5, 0, 0, 0, 3]))
        with patch('pypixelcolor.lib.device_session.BleakClient', FakeBle):
            result = LedTransport('test-only').send(small_scene(), threading.Event())
        self.assertEqual(result['status'], 'acknowledged')
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), 20)
        self.assertEqual(events, ['connect', 'disconnect'])
        events.clear()
        class PartialConnect(FakeBle):
            async def connect(self):
                events.append('allocated')
                raise RuntimeError('connect failed after resources allocated')
        with patch('pypixelcolor.lib.device_session.BleakClient', PartialConnect):
            with self.assertRaises(TransportError):
                LedTransport('test-only').send(small_scene(), threading.Event())
        self.assertEqual(events, ['allocated', 'disconnect'])


class RadioDriverTests(unittest.TestCase):
    """Protocol/lifecycle mock only; WinRT and audible output remain unverified."""
    def test_unresponsive_owned_child_is_stopped_with_bounded_wait(self):
        child = SimpleNamespace(pid=45234, stdin=io.StringIO(), returncode=None)
        child.poll = lambda: child.returncode
        def terminate():
            child.returncode = -15
        child.terminate = Mock(side_effect=terminate)
        child.wait = Mock(return_value=-15)
        child.kill = Mock()
        player = RadioPlayer('CRF-35B', '/tmp/jingle_bells.wav')
        player.process = child
        with patch('show_transport.subprocess.Popen') as spawn:
            started = time.monotonic()
            self.assertEqual(player.stop()['status'], 'stopped_child_terminated')
            self.assertLess(time.monotonic() - started, .8)
            spawn.assert_not_called()
        child.terminate.assert_called_once()
        child.kill.assert_not_called()
        self.assertIsNone(player.process)

    def test_exited_child_cannot_report_playback_success(self):
        player = RadioPlayer('CRF-35B', '/tmp/jingle_bells.wav')
        exited = SimpleNamespace(pid=46234, stdin=io.StringIO(), stdout=io.StringIO(),
                                 poll=lambda: 1)
        with patch('show_transport.sys.platform', 'win32'), patch('show_transport.subprocess.Popen', return_value=exited):
            with self.assertRaises(TransportError):
                player.play(45, threading.Event())
        player.close()

    def test_persistent_owned_child_and_no_spawn_until_play(self):
        output = queue.Queue()
        calls = []
        class Lines:
            def __iter__(self): return self
            def __next__(self):
                line = output.get()
                if line is None: raise StopIteration
                return line
            def close(self): pass
        class Input:
            def write(self, line):
                request = json.loads(line)
                calls.append(request['command'])
                state = 'playing' if request['command'] in ('play', 'status') else 'stopped'
                output.put(json.dumps(dict(id=request['id'], ok=True,
                                           result=dict(status=state, audible_verified=False))))
                if request['command'] == 'quit':
                    child.returncode = 0
                    output.put(None)
            def flush(self): pass
            def close(self): pass
        child = SimpleNamespace(pid=41234, stdin=Input(), stdout=Lines(), returncode=None)
        child.poll = lambda: child.returncode
        child.terminate = lambda: self.fail('normal shutdown should use quit')
        player = RadioPlayer('CRF-35B', '/tmp/jingle_bells.wav')
        with patch('show_transport.sys.platform', 'win32'), patch('show_transport.subprocess.Popen', return_value=child) as spawn:
            self.assertEqual(player.status()['status'], 'stopped')
            player.stop()
            spawn.assert_not_called()
            self.assertEqual(player.play(45, threading.Event())['status'], 'playing')
            player.play(30, threading.Event())
            player.status()
            player.stop()
            player.close()
            self.assertEqual(spawn.call_count, 1)
        self.assertEqual(calls, ['play', 'play', 'status', 'stop', 'quit'])


if __name__ == '__main__':
    unittest.main()
