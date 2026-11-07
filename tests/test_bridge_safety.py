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
