import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demos.wled_sim.server import make_server


class WLEDSimulatorTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, body=None):
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=2) as response:
            return json.load(response)

    def test_explicit_simulation_and_reset(self):
        self.assertTrue(self.request('/json')['simulated'])
        self.request('/json/state', {'seg': [{'id': 0, 'bri': 200}], 'v': True})
        self.assertEqual(200, self.request('/json/state')['seg'][0]['bri'])
        self.server.device.set_fault('ignore')
        self.server.device.reset()
        snapshot = self.server.device.snapshot()
        self.assertEqual('none', snapshot['fault'])
        self.assertEqual(24, snapshot['state']['seg'][0]['bri'])
        self.assertEqual([], snapshot['traces'])

    def test_invalid_batch_is_atomic(self):
        before = self.server.device.snapshot()['state']
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.request('/json/state', {'seg': [{'id': 0, 'bri': 200}, {'id': 99, 'bri': 40}]})
        self.assertEqual(400, context.exception.code)
        self.assertEqual(before, self.server.device.snapshot()['state'])

    def test_snapshots_are_not_mutable_device_references(self):
        snapshot = self.server.device.snapshot()
        snapshot['state']['seg'][0]['bri'] = 0
        snapshot['traces'].append({'fake': True})
        self.assertEqual(24, self.server.device.snapshot()['state']['seg'][0]['bri'])
        self.assertEqual([], self.server.device.traces)


if __name__ == '__main__':
    unittest.main()
