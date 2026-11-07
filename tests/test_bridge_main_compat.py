import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import kevin_bridge
from show_control import ShowController


class SlowLed:
    def __init__(self):
        self.sent = []

    def send(self, scene, cancel, brightness=25):
        for _ in range(40):  # ~2s, like a BLE transfer
            if cancel.is_set():
                from show_transport import Cancelled
                raise Cancelled('cancelled')
            time.sleep(.05)
        self.sent.append(scene)
        return {'status': 'acknowledged'}

    def clear(self, cancel):
        return {'status': 'clear_acknowledged'}


class Radio:
    def play(self, volume, cancel):
        return {'status': 'playing'}

    def stop(self):
        return {'status': 'stopped'}

    def status(self):
        return {'status': 'stopped'}


class BridgeTests(unittest.TestCase):
    def test_new_request_replaces_the_one_in_flight(self):
        led = SlowLed()
        c = ShowController(transport=led, radio=Radio(), config={'KEVIN_SHOW_LIVE': '1', 'KEVIN_LED_ADDRESS': 'x', 'KEVIN_RADIO_DEVICE': 'CRF-35B'})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = Mock()
        settings.public.return_value = {'configured': True}
        settings.env = {'KEVIN_AI_CONFIG': tmp.name + '/ai.json', 'KEVIN_PLANNER': 'ollama'}
        bridge = kevin_bridge.Bridge(controller=c, ai=settings, eye=Mock())
        def decide(text, *args):
            return {'reply': 'ok', 'led': {'mode': 'scene', 'preset': 'santa' if '산타' in text else 'christmas_train',
                    'snow': 'keep', 'sky': 'keep'}, 'music': {'mode': 'keep', 'volume': None}}
        with patch('kevin_bridge.kevin_agent.decide', side_effect=decide):
            first = bridge.say('기차')
            time.sleep(.3)
            second = bridge.say('산타')
        self.assertNotIn('busy', first)
        self.assertNotIn('busy', second)
        end = time.time() + 6
        while c.get_state()['status'] not in ('complete', 'error') and time.time() < end:
            time.sleep(.1)
        self.assertEqual(c.get_state()['status'], 'complete')
        self.assertEqual(len(led.sent), 1)
        self.assertEqual(c.get_state()['recipe']['preset'], 'santa')


if __name__ == '__main__':
    unittest.main()
