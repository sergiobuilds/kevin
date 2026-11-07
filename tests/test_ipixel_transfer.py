import asyncio
import base64
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
import ipixel_transfer
import pixels

class TransferTests(unittest.TestCase):
    def test_prepare_and_default_is_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'scene.gif'
            path.write_bytes(base64.b64decode(pixels.export_image(pixels.generate('snow village'), 'gif')))
            data, kind, frames = ipixel_transfer.prepare(path)
            self.assertEqual((kind, frames), ('gif', 13))
            self.assertTrue(data.startswith(b'GIF'))
            self.assertEqual(ipixel_transfer.main([str(path)]), 0)
            self.assertEqual(ipixel_transfer.main([str(path), '--send']), 1)

    def test_scene_send_defaults_to_simulation_and_requires_explicit_selection(self):
        scene = pixels.generate('snow village')
        result = ipixel_transfer.send_scene(scene)
        self.assertEqual((result['status'], result['format'], result['frames']), ('simulated', 'gif', 13))
        with self.assertRaises(ipixel_transfer.TransferError):
            ipixel_transfer.send_scene(scene, 'ipixel', 'selected', False)
        with self.assertRaises(ipixel_transfer.TransferError):
            ipixel_transfer.send_scene(scene, 'ipixel', '', True)
        events = []
        class Fake:
            def __init__(self, address): events.append('selected')
            async def connect(self): events.append('connect')
            def get_device_info(self): return SimpleNamespace(width=144, height=16)
            async def send_image(self, path):
                self_test.assertTrue(path.read_bytes().startswith(b'GIF'))
                events.append('send')
            async def disconnect(self): events.append('disconnect')
        self_test = self
        result = ipixel_transfer.send_scene(scene, 'ipixel', 'selected', True, Fake)
        self.assertEqual(result['status'], 'sent_unverified')
        self.assertEqual(events, ['selected', 'connect', 'send', 'disconnect'])
        class Broken(Fake):
            async def send_image(self, path): raise RuntimeError('sensitive identifier')
        with self.assertRaises(ipixel_transfer.TransferError) as err:
            ipixel_transfer.send_scene(scene, 'ipixel', 'selected', True, Broken)
        self.assertNotIn('sensitive identifier', str(err.exception))
        self.assertEqual(events[-1], 'disconnect')

    def test_contract_and_disconnect_on_failure(self):
        events = []
        class Fake:
            def __init__(self, address): events.append(('selected', address))
            async def connect(self): events.append('connect')
            def get_device_info(self): return SimpleNamespace(width=144, height=16)
            async def send_image(self, path): events.append(('send', path))
            async def disconnect(self): events.append('disconnect')
        asyncio.run(ipixel_transfer.transfer('selected', 'scene.gif', Fake))
        self.assertEqual(events, [('selected', 'selected'), 'connect', ('send', 'scene.gif'), 'disconnect'])
        class Wrong(Fake):
            def get_device_info(self): return SimpleNamespace(width=64, height=16)
        events.clear()
        with self.assertRaises(ipixel_transfer.TransferError):
            asyncio.run(ipixel_transfer.transfer('selected', 'scene.gif', Wrong))
        self.assertEqual(events[-1], 'disconnect')
        self.assertFalse(any(isinstance(x, tuple) and x[0] == 'send' for x in events))
