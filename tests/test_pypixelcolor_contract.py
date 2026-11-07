"""Optional integration test: real pypixelcolor encoder, no Bluetooth required."""
import asyncio
import base64
import tempfile
import random
import zlib
import unittest
from pathlib import Path
from unittest.mock import patch
import pixels
import ipixel_transfer

try:
    from pypixelcolor import AsyncClient
    from pypixelcolor.lib.device_info import DeviceInfo
    from pypixelcolor.commands.send_image import send_image
    from pypixelcolor.lib.transport.send_plan import send_plan
    from pypixelcolor.lib.transport.ack_manager import AckManager
    from pypixelcolor.lib.constants import WRITE_UUID
except ImportError:
    AsyncClient = None

@unittest.skipUnless(AsyncClient, 'install optional pypixelcolor==0.5.0 to test its encoder')
class ActualLibraryContract(unittest.TestCase):
    def test_real_encoder_with_mock_transport(self):
        info = DeviceInfo(135, 'unknown', 'unknown', 144, 16, False, 255)
        events = []
        class Session:
            async def connect(self): events.append('connect')
            async def disconnect(self): events.append('disconnect')
            def get_device_info(self): return info
            async def execute_command(self, func, *args, **kwargs):
                plan = func(*args, device_info=info, **kwargs)
                self_test.assertGreater(len(plan.windows), 0)
                events.append('encoded')
                class Result: data = None
                return Result()
        self_test = self
        def client(address):
            c = AsyncClient(address)
            c._session = Session()
            return c
        with tempfile.TemporaryDirectory() as tmp:
            for fmt in ('png', 'gif'):
                path = Path(tmp) / ('scene.' + fmt)
                path.write_bytes(base64.b64decode(pixels.export_image(pixels.generate('snow village'), fmt)))
                asyncio.run(ipixel_transfer.transfer('test-only', path, client))
        self.assertEqual(events, ['connect', 'encoded', 'disconnect'] * 2)

    def test_connect_failure_after_ble_connect_closes_session(self):
        events = []
        class Session:
            is_connected = False
            async def connect(self):
                self.is_connected = True
                events.append('connected')
                raise RuntimeError('device info failed')
            async def disconnect(self):
                events.append('disconnected')
                self.is_connected = False
        def client(address):
            c = AsyncClient(address)
            c._session = Session()
            return c
        with self.assertRaisesRegex(RuntimeError, 'device info failed'):
            asyncio.run(ipixel_transfer.transfer('test-only', 'unused.gif', client))
        self.assertEqual(events, ['connected', 'disconnected'])

    def test_real_send_plan_with_fake_ble_ack_and_timeout(self):
        # Exercise the library transport, not a reimplementation of its packet format.
        info = DeviceInfo(135, 'unknown', 'unknown', 144, 16, False, 255)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'scene.gif'
            rng = random.Random(173)
            colors = ['#%02x%02x%02x' % (i, 255-i, (i*37)%256) for i in range(256)]
            frames = [[[colors[rng.randrange(256)] for x in range(144)] for y in range(16)] for t in range(13)]
            path.write_bytes(base64.b64decode(pixels.export_image({'frames':frames, 'duration':200}, 'gif')))
            plan = send_image(path, device_info=info)
            windows = list(plan.windows)
            self.assertTrue(windows)
            async def exercise(acknowledge):
                ack = AckManager()
                chunks = []
                boundaries = []
                for window in windows: boundaries.append((boundaries[-1] if boundaries else 0) + len(window.data))
                class FakeBle:
                    async def write_gatt_char(self, characteristic, data, response):
                        self_test.assertEqual(characteristic, WRITE_UUID)
                        self_test.assertTrue(response)
                        chunks.append(bytes(data))
                        sent = sum(map(len, chunks))
                        if acknowledge and sent in boundaries:
                            ack.make_notify_handler()(None, bytes([5, 0, 0, 0, 3 if sent == boundaries[-1] else 0]))
                if acknowledge:
                    result = await send_plan(FakeBle(), plan, ack, ack_timeout=.01)
                    self_test.assertTrue(result.success)
                    self_test.assertEqual(b''.join(chunks), b''.join(w.data for w in windows))
                    self_test.assertTrue(all(len(chunk) <= plan.chunk_size for chunk in chunks))
                else:
                    with self_test.assertRaisesRegex(RuntimeError, 'no ack'):
                        await send_plan(FakeBle(), plan, ack, ack_timeout=.01)
            self_test = self
            # Deterministic high-entropy frames exercise more than one 12 KiB window.
            self.assertGreater(len(windows), 1)
            # Public 0.5.0 library output: each window carries a length and the
            # same CRC32 over the full re-encoded file, in little-endian order.
            payload = b''.join(w.data[15:] for w in windows)
            expected_crc = (zlib.crc32(payload) & 0xffffffff).to_bytes(4, 'little')
            for window in windows:
                self.assertEqual(int.from_bytes(window.data[5:9], 'little'), len(payload))
                self.assertEqual(window.data[9:13], expected_crc)
            asyncio.run(exercise(True))
            asyncio.run(exercise(False))

    def test_real_client_session_and_transport_with_fake_ble(self):
        # One-window fixture exercises device-info response, image transfer and
        # disconnect through actual pypixelcolor; bytes are simulated, not captured.
        events = []
        class FakeBle:
            device_type = 135
            def __init__(self, address, disconnected_callback):
                self.callback = None
                self.disconnected_callback = disconnected_callback
                self.bytes_in_window = 0
                self.window_size = None
            async def connect(self): events.append('connect')
            async def disconnect(self):
                events.append('disconnect')
                self.disconnected_callback(self)
            async def start_notify(self, characteristic, callback): self.callback = callback
            async def stop_notify(self, characteristic): self.callback = None
            async def write_gatt_char(self, characteristic, data, response):
                data = bytes(data)
                if len(data) == 8 and data[:4] == bytes([8, 0, 1, 128]):
                    events.append('device_info')
                    self.callback(None, bytes([0, 0, 0, 0, self.device_type]))
                    return
                if self.window_size is None:
                    self.window_size = int.from_bytes(data[:2], 'little')
                self.bytes_in_window += len(data)
                if self.bytes_in_window == self.window_size:
                    events.append('window_ack')
                    self.callback(None, bytes([5, 0, 0, 0, 3]))
                    self.bytes_in_window = 0
                    self.window_size = None
        scene = pixels.generate('snow village')
        with patch('pypixelcolor.lib.device_session.BleakClient', FakeBle):
            result = ipixel_transfer.send_scene(scene, 'ipixel', 'simulated', True)
        self.assertEqual(result['status'], 'sent_unverified')
        self.assertEqual(events, ['connect', 'device_info', 'window_ack', 'disconnect'])
        events.clear()
        class WrongSize(FakeBle): device_type = 129
        with patch('pypixelcolor.lib.device_session.BleakClient', WrongSize):
            with self.assertRaises(ipixel_transfer.TransferError):
                ipixel_transfer.send_scene(scene, 'ipixel', 'simulated', True)
        self.assertEqual(events, ['connect', 'device_info', 'disconnect'])
