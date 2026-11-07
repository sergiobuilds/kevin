"""Mock-only regressions for bridge cancellation, access, and camera opt-out."""
import asyncio
import http.client
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer

import kevin_bridge
from show_transport import Cancelled, LedTransport


class BridgeSafetyTests(unittest.TestCase):

    def bridge(self):
        controller = Mock(live_configured=True)
        controller.lock = threading.RLock()
        controller.get_state.return_value = {'recipe': {}, 'radio': {}}
        controller.stop.return_value = {'status': 'stopped'}
        with patch.object(kevin_bridge, 'Eye'):
            bridge = kevin_bridge.Bridge(controller)
        return bridge


    def test_observation_without_person_does_not_supersede_user_command(self):
        bridge = self.bridge()
        token = bridge.job_cancel
        with patch.object(kevin_bridge, 'see_person', return_value=(False, 'empty')):
            result = bridge.guard({'mode': 'away', 'streak': 2, 'image': 'mock'})
        self.assertEqual(result['action'], 'none')
        bridge.c.supersede.assert_not_called()
        self.assertFalse(token.is_set())


    def test_stop_during_vision_cannot_start_alarm(self):
        bridge = self.bridge()
        def observe(_):
            bridge.stop()
            return True, 'person'
        with patch.object(kevin_bridge, 'see_person', side_effect=observe):
            result = bridge.guard({'mode': 'away', 'streak': 2, 'image': 'mock'})
        self.assertEqual(result['action'], 'none')
        bridge.c.transport.send.assert_not_called()
        bridge.c.radio.play.assert_not_called()


    def test_stop_during_fallback_planning_cannot_queue_show(self):
        bridge = self.bridge()
        def planning(*_):
            bridge.stop()
            return {'mode': 'update'}, 'mock'
        with patch.object(kevin_bridge.kevin_agent, 'decide', side_effect=RuntimeError('offline')), patch.object(kevin_bridge, 'plan', side_effect=planning):
            result = bridge.say('test')
        self.assertTrue(result['cancelled'])
        bridge.c.command.assert_not_called()


    def test_guard_stop_token_reaches_audio_prepare(self):
        bridge = self.bridge()
        reached = threading.Event()
        def prepare(cancel):
            reached.set()
            cancel.wait(1)
        bridge.c._prepare_audio.side_effect = prepare
        with patch.object(kevin_bridge, 'see_person', return_value=(True, 'person')):
            bridge.guard({'mode': 'away', 'streak': 2, 'image': 'mock'})
        self.assertTrue(reached.wait(1))
        bridge.stop()
        self.assertTrue(bridge.job_cancel.is_set())
        bridge.c.radio.play.assert_not_called()


    def test_native_command_ack_wait_is_cancelled(self):
        entered, finished = threading.Event(), threading.Event()
        transport = LedTransport('mock')
        async def pending(*_):
            entered.set()
            try: await asyncio.Event().wait()
            finally: finished.set()
        transport._command = pending
        cancel = threading.Event(); errors = []
        def run():
            try: transport.command('send_text', cancel, 'mock')
            except Cancelled: errors.append('cancelled')
        worker = threading.Thread(target=run); worker.start()
        self.assertTrue(entered.wait(1)); cancel.set(); worker.join(1)
        self.assertFalse(worker.is_alive()); self.assertTrue(finished.is_set())
        self.assertEqual(errors, ['cancelled'])


    def test_native_commands_invalidate_scene(self):
        for mode in ('text', 'score', 'clear'):
            bridge = self.bridge()
            turn = {'reply': 'ok', 'led': {'mode': mode, 'text': 'hello', 'animation': 'static',
                    'color': '#ffffff', 'bg': '', 'score_left': 1, 'score_right': 2},
                    'music': {'mode': 'carol', 'volume': 0}}
            bridge._act(turn, bridge.job_cancel)
            self.assertIsNone(bridge.c.applied_scene)


    def test_native_commands_keep_zero_volume(self):
        for mode in ('text', 'score', 'clear'):
            bridge = self.bridge()
            turn = {'reply': 'ok', 'led': {'mode': mode, 'text': 'hello', 'animation': 'static',
                    'color': '#ffffff', 'bg': '', 'score_left': 1, 'score_right': 2},
                    'music': {'mode': 'carol', 'volume': 0}}
            bridge._act(turn, bridge.job_cancel)
            bridge.c.radio.play.assert_called_once_with(0, bridge.job_cancel)


    def test_camera_off_never_opens_a_camera(self):
        with patch.dict('os.environ', {'KEVIN_CAMERA': 'off'}), patch.object(kevin_bridge.threading, 'Thread') as thread:
            eye = kevin_bridge.Eye()
            camera = Mock()
            self.assertIsNone(eye._open_best(camera))
            camera.VideoCapture.assert_not_called()
            thread.assert_not_called()
            self.assertEqual(eye.error, 'camera disabled')


    def test_http_rejects_untrusted_origin_and_host_before_dispatch(self):
        bridge = self.bridge()
        server = ThreadingHTTPServer(('127.0.0.1', 0), kevin_bridge.make_handler(bridge))
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        def request(origin=None, host=None, method='POST'):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
            headers = {'Content-Type': 'application/json'}
            if origin: headers['Origin'] = origin
            if host: headers['Host'] = host
            connection.request(method, '/stop', '{}', headers)
            response = connection.getresponse(); status = response.status; cors = response.getheader('Access-Control-Allow-Origin')
            response.read(); connection.close(); return status, cors
        try:
            self.assertEqual(request('https://evil.example')[0], 403)
            self.assertEqual(request(host='evil.example')[0], 403)
            bridge.c.stop.assert_not_called()
            self.assertEqual(request('https://sergiobuilds.github.io'), (200, 'https://sergiobuilds.github.io'))
            self.assertEqual(request()[0], 200)
            bridge.c.stop.return_value = {'status': 'error'}
            self.assertEqual(request()[0], 503)
        finally:
            server.shutdown(); server.server_close(); worker.join(2)
