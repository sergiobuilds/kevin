import io
import json
import tempfile
import unittest
from pathlib import Path

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


class McpTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
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
        folder = Path(out['receipt']['path'])
        stored = json.loads((folder / 'receipt.json').read_text(encoding='utf-8'))
        images = {name: (folder / name).read_bytes() for name in stored['evidence']}
        self.assertTrue(receipt.check(stored, images))

    def test_unknown_tool_is_an_error(self):
        (reply,) = rpc(self.kevin, {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                                    'params': {'name': 'open_door', 'arguments': {}}})
        self.assertEqual(reply['error']['code'], -32602)

    def test_stop_all(self):
        (reply,) = rpc(self.kevin, {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
                                    'params': {'name': 'stop_all', 'arguments': {}}})
        self.assertEqual(self.controller.calls[-1], ('stop',))
        self.assertFalse(reply['result']['isError'])


if __name__ == '__main__':
    unittest.main()
