import io
import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from PIL import Image, ImageDraw

import kevin_mcp
import receipt


class FakeController:
    live_configured = True

    def __init__(self):
        self.planner = None
        self.calls = []
        self.state = {'status': 'idle'}

    def command(self, prompt, execute):
        plan = self.planner(prompt, {}) if self.planner else {'mode': 'update', 'preset': 'santa'}
        self.calls.append((prompt, execute, plan))
        self.state = {'status': 'complete', 'plan': plan, 'transport': {'status': 'acknowledged'},
                      'radio': {'status': 'not_started'}}

    def get_state(self):
        return self.state

    def stop(self):
        self.calls.append(('stop',))
        self.state['status'] = 'stopped'
        return {'status': 'stopped'}


class FakeCamera:
    def __init__(self):
        self.n = 0

    def grab(self):
        image = Image.new('RGB', (320, 180), (5, 5, 5))
        if self.n:
            ImageDraw.Draw(image).rectangle((20 * self.n, 60, 20 * self.n + 120, 100), fill=(255, 40, 40))
        self.n += 1
        out = io.BytesIO()
        image.save(out, 'JPEG')
        return out.getvalue()


def rpc(kevin, *messages):
    out = io.StringIO()
    kevin_mcp.serve(kevin, io.StringIO(''.join(json.dumps(m) + '\n' for m in messages)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


class StreamingRpc:
    """Blocking input/output queues exercise the real reader and worker threads."""

    def __init__(self, kevin):
        self.incoming, self.outgoing = queue.Queue(), queue.Queue()
        self.replies = {}
        self.thread = threading.Thread(target=kevin_mcp.serve, args=(kevin, self, self), daemon=True)
        self.thread.start()

    def __iter__(self):
        return self

    def __next__(self):
        item = self.incoming.get()
        if item is None:
            raise StopIteration
        return item

    def write(self, line):
        self.outgoing.put(json.loads(line))

    def flush(self):
        pass

    def call(self, mid, name, arguments=None):
        self.incoming.put(json.dumps({'jsonrpc': '2.0', 'id': mid, 'method': 'tools/call',
                                     'params': {'name': name, 'arguments': arguments or {}}}) + '\n')

    def response(self, mid, timeout=2):
        deadline = time.monotonic() + timeout
        while mid not in self.replies:
            reply = self.outgoing.get(timeout=max(0, deadline - time.monotonic()))
            self.replies[reply['id']] = reply
        return self.replies.pop(mid)

    def close(self):
        self.incoming.put(None)
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise AssertionError('MCP reader did not finish after input EOF')


class McpTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        tmp = Path(temp.name)
        self.controller = FakeController()
        self.kevin = kevin_mcp.Kevin(self.controller, FakeCamera(), receipt.Signer(tmp / 'k.pem'),
                                     tmp / 'receipts', settle=0)

    def test_handshake_and_tool_list(self):
        init, tools = rpc(self.kevin,
                          {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                          {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                          {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        self.assertEqual(init['result']['serverInfo']['name'], 'kevin')
        self.assertEqual({t['name'] for t in tools['result']['tools']}, {'led_scene', 'radio', 'say', 'stop_all'})

    def test_tool_call_drives_device_and_returns_signed_receipt(self):
        (reply,) = rpc(self.kevin, {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                                    'params': {'name': 'led_scene', 'arguments': {'preset': 'santa', 'snow': 'heavy'}}})
        out = reply['result']['structuredContent']
        _, execute, plan = self.controller.calls[0]
        self.assertTrue(execute)
        self.assertEqual((plan['preset'], plan['snow'], plan['music']), ('santa', 'heavy', 'keep'))
        self.assertTrue(out['receipt']['observation']['changed'])
        self.assertTrue(out['receipt']['observation']['moving'])
        # Synthetic frames without provenance cannot become physical evidence.
        self.assertFalse(out['physically_verified'])
        self.assertEqual(out['observation']['source'], 'unspecified')
        self.assertEqual(out['observation']['scope'], 'visual_change_only')
        folder = Path(out['receipt']['path'])
        stored = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
        images = {name: (folder / name).read_bytes() for name in stored['evidence']}
        self.assertTrue(receipt.check(stored, images))

    def test_unknown_tool_is_an_error(self):
        (reply,) = rpc(self.kevin, {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                                    'params': {'name': 'open_door', 'arguments': {}}})
        self.assertEqual(reply['error']['code'], -32602)

    def test_say_receipt_identifies_only_executed_targets(self):
        for changes, music, targets in (
            ({}, 'play', ['radio-crf35b']),
            ({'preset': 'santa'}, 'play', ['led-144x16', 'radio-crf35b']),
        ):
            with self.subTest(targets=targets):
                self.controller.planner = lambda *_: kevin_mcp._plan(changes, music)
                result = self.kevin.call('say', {'text': '테스트 요청'})
                stored = json.loads((Path(result['receipt']['path']) / 'receipt.json').read_text())
                self.assertEqual(stored['action']['targets'], targets)
                self.assertEqual(set(stored['device_reply']), set(targets))
                self.assertEqual(stored['device'], targets[0] if len(targets) == 1 else 'kevin-show')

    def test_stop_all(self):
        (reply,) = rpc(self.kevin, {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
                                    'params': {'name': 'stop_all', 'arguments': {}}})
        self.assertEqual(self.controller.calls[-1], ('stop',))
        self.assertFalse(reply['result']['isError'])

    def test_camera_failure_before_action_does_not_block_control(self):
        self.kevin.camera.grab = Mock(side_effect=RuntimeError('private camera error'))
        result = self.kevin.call('led_scene', {'preset': 'santa'})
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(self.controller.calls), 1)
        self.assertEqual(result['observation']['status'], 'unavailable')
        self.assertEqual(result['observation']['reason'], 'camera_unavailable')
        self.assertNotIn('receipt', result)
        self.assertNotIn('private camera error', json.dumps(result))
        self.assertFalse(result['physically_verified'])

    def test_camera_failure_after_action_preserves_completed_action(self):
        before = self.kevin.camera.grab()
        self.kevin.camera.grab = Mock(side_effect=[before, RuntimeError('after frame failed')])
        result = self.kevin.call('led_scene', {'preset': 'santa'})
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(self.controller.calls), 1)
        self.assertEqual(result['observation']['status'], 'unavailable')
        self.assertEqual(result['observation']['reason'], 'evidence_unavailable')
        self.assertNotIn('receipt', result)

    def test_signing_failure_preserves_completed_action(self):
        self.kevin.signer.sign = Mock(side_effect=RuntimeError('signing unavailable'))
        result = self.kevin.call('led_scene', {'preset': 'santa'})
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(self.controller.calls), 1)
        self.assertEqual(result['observation']['status'], 'unavailable')
        self.assertEqual(result['observation']['reason'], 'evidence_unavailable')
        self.assertNotIn('receipt', result)
        self.kevin.signer.sign.assert_called_once()

    def test_direct_tool_restores_configured_planner_for_following_say(self):
        self.kevin.camera = None
        configured = Mock(return_value=kevin_mcp._plan({'sky': '#112233'}))
        self.controller.planner = configured
        self.kevin.call('led_scene', {'preset': 'santa'})
        self.assertIs(self.controller.planner, configured)
        configured.assert_not_called()
        self.kevin.call('say', {'text': '하늘 바꿔줘'})
        configured.assert_called_once()
        self.assertEqual(self.controller.calls[1][2]['sky'], '#112233')
        self.assertIs(self.controller.planner, configured)

    def test_invalid_arguments_never_touch_camera_or_controller(self):
        self.kevin.camera.grab = Mock(side_effect=AssertionError('invalid request captured a frame'))
        cases = (
            ('radio', {'music': 'unknown'}),
            ('radio', {'music': 'keep', 'volume': True}),
            ('radio', {'music': 'play', 'volume': 101}),
            ('radio', {'music': 'play', 'volume': float('nan')}),
            ('radio', {'music': 'play', 'extra': 'x'}),
            ('led_scene', {'preset': 'santa', 'shell': 'ignored'}),
            ('led_scene', {}),
            ('radio', {'music': 'keep'}),
            ('say', {'text': ''}),
            ('stop_all', {'extra': True}),
        )
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                (reply,) = rpc(self.kevin, {'jsonrpc': '2.0', 'id': 10, 'method': 'tools/call',
                                          'params': {'name': name, 'arguments': arguments}})
                self.assertEqual(reply['error']['code'], -32602)
        self.kevin.camera.grab.assert_not_called()
        self.assertEqual(self.controller.calls, [])

    def test_stop_remains_responsive_while_execution_is_waiting(self):
        entered = threading.Event()
        def pending_command(prompt, execute):
            self.controller.calls.append((prompt, execute))
            self.controller.state = {'status': 'sending'}
            entered.set()
        self.controller.command = pending_command
        self.kevin.camera = None
        stream = StreamingRpc(self.kevin)
        self.addCleanup(stream.close)
        stream.call(20, 'led_scene', {'preset': 'santa'})
        self.assertTrue(entered.wait(1), 'tool did not begin')
        operation = self.kevin.stop_requested
        stream.call(21, 'stop_all')
        stopped = stream.response(21)
        self.assertEqual(stopped['result']['structuredContent']['status'], 'stopped')
        self.assertTrue(operation.is_set())
        action = stream.response(20)
        self.assertEqual(action['result']['structuredContent']['status'], 'stopped')
        self.assertEqual(self.controller.calls[-1], ('stop',))

    def test_stop_during_before_capture_prevents_late_hardware_command(self):
        entered, release = threading.Event(), threading.Event()
        frame = self.kevin.camera.grab()
        def waiting_frame():
            entered.set()
            if not release.wait(2):
                raise AssertionError('test capture was not released')
            return frame
        self.kevin.camera.grab = waiting_frame
        stream = StreamingRpc(self.kevin)
        self.addCleanup(stream.close)
        self.addCleanup(release.set)
        stream.call(30, 'led_scene', {'preset': 'santa'})
        self.assertTrue(entered.wait(1), 'tool did not reach camera')
        stream.call(31, 'stop_all')
        self.assertEqual(stream.response(31)['result']['structuredContent']['status'], 'stopped')
        self.assertEqual(self.controller.calls, [('stop',)])
        release.set()
        self.assertEqual(stream.response(30)['result']['structuredContent']['status'], 'stopped')
        self.assertEqual(self.controller.calls, [('stop',)])

    def test_optional_light_tool_uses_adapter_and_preserves_simulation_source(self):
        from devices.base import Result
        light = Mock()
        light.apply.return_value = Result('acknowledged', detail={'source': 'simulation',
                                        'readback_matches': True, 'untouched_preserved': True})
        light.stop.return_value = Result('stopped')
        self.kevin.light = light
        (listed,) = rpc(self.kevin, {'jsonrpc': '2.0', 'id': 40, 'method': 'tools/list'})
        self.assertIn('light_segment', {tool['name'] for tool in listed['result']['tools']})
        args = {'segment': 1, 'on': True, 'brightness': 80}
        (reply,) = rpc(self.kevin, {'jsonrpc': '2.0', 'id': 41, 'method': 'tools/call',
                                  'params': {'name': 'light_segment', 'arguments': args}})
        result = reply['result']['structuredContent']
        self.assertEqual(result['detail']['source'], 'simulation')
        self.assertFalse(result['physically_verified'])
        self.assertEqual(light.apply.call_args.args[0], args)
        self.assertEqual(self.controller.calls, [])
        self.kevin.call('stop_all', {})
        light.stop.assert_called_once()

    def test_stop_between_preflight_and_dispatch_prevents_late_command(self):
        entered, release = threading.Event(), threading.Event()
        class PausedPreflightController(FakeController):
            @property
            def live_configured(self):
                entered.set()
                if not release.wait(2):
                    raise AssertionError('preflight was not released')
                return True
        controller = PausedPreflightController()
        self.kevin.controller = controller
        self.kevin.camera = None
        stream = StreamingRpc(self.kevin)
        self.addCleanup(stream.close)
        self.addCleanup(release.set)
        stream.call(50, 'led_scene', {'preset': 'santa'})
        self.assertTrue(entered.wait(1))
        stream.call(51, 'stop_all')
        self.assertEqual(stream.response(51)['result']['structuredContent']['status'], 'stopped')
        release.set()
        action = stream.response(50)['result']['structuredContent']
        self.assertEqual(action['status'], 'stopped')
        self.assertEqual(action['action_status'], 'not_started')
        self.assertEqual(controller.calls, [('stop',)])

    def test_stop_during_last_frame_does_not_sign_interrupted_observation(self):
        entered, release = threading.Event(), threading.Event()
        original_grab = self.kevin.camera.grab
        count = 0
        def last_frame_waits():
            nonlocal count
            count += 1
            if count == 3:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('last capture was not released')
            return original_grab()
        self.kevin.camera.grab = last_frame_waits
        self.kevin.signer.sign = Mock(wraps=self.kevin.signer.sign)
        stream = StreamingRpc(self.kevin)
        self.addCleanup(stream.close)
        self.addCleanup(release.set)
        stream.call(60, 'led_scene', {'preset': 'santa'})
        self.assertTrue(entered.wait(1.5))
        stream.call(61, 'stop_all')
        self.assertEqual(stream.response(61)['result']['structuredContent']['status'], 'stopped')
        release.set()
        action = stream.response(60)['result']['structuredContent']
        self.assertEqual(action['status'], 'stopped')
        self.assertEqual(action['action_status'], 'complete')
        self.assertEqual(action['observation']['reason'], 'observation_cancelled')
        self.assertNotIn('receipt', action)
        self.kevin.signer.sign.assert_not_called()
        self.assertEqual(self.controller.calls[-1], ('stop',))


if __name__ == '__main__':
    unittest.main()
