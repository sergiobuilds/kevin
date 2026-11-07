"""Real upstream WLED client against an isolated loopback protocol peer."""
import importlib.util
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demos.wled_sim.server import make_server
from devices.light import WLEDLightAdapter


@unittest.skipUnless(importlib.util.find_spec('wled'), 'optional requirements-demo.txt not installed')
class LightAdapterTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.adapter = WLEDLightAdapter('127.0.0.1', self.server.server_port, simulated=True, timeout=1)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def apply(self, plan):
        return self.adapter.apply(plan, threading.Event())

    def test_actual_upstream_segment_roundtrip_preserves_other_segments(self):
        before = self.server.device.snapshot()['state']
        result = self.apply({'segment': 1, 'brightness': 55, 'color': [12, 34, 56], 'on': False})
        self.assertEqual('acknowledged', result.status)
        self.assertFalse(result.physically_verified)
        self.assertEqual('simulation', result.detail['source'])
        self.assertTrue(result.detail['readback_matches'])
        self.assertTrue(result.detail['untouched_preserved'])
        after = self.server.device.snapshot()['state']
        self.assertEqual(before['seg'][0], after['seg'][0])
        self.assertEqual(before['seg'][2], after['seg'][2])
        self.assertEqual(before['on'], after['on'])
        self.assertEqual([12, 34, 56], after['seg'][1]['col'][0])
        self.assertFalse(after['seg'][1]['on'])
        traces = self.server.device.traces
        write = next(i for i, t in enumerate(traces) if t['method'] == 'POST')
        self.assertEqual('/json/state', traces[write]['path'])
        self.assertEqual({'seg': [{'bri': 55, 'on': False, 'col': [[12, 34, 56]], 'id': 1}], 'v': True},
                         traces[write]['body'])
        self.assertTrue(any(t['method'] == 'GET' and t['path'] == '/json/si' for t in traces[write + 1:]))

    def test_accepted_but_ignored_write_is_failed(self):
        self.server.device.set_fault('ignore')
        result = self.apply({'segment': 0, 'brightness': 220})
        self.assertEqual('failed', result.status)
        self.assertEqual('readback_mismatch', result.detail['error'])
        self.assertFalse(result.detail['readback_matches'])
        self.assertTrue(result.detail['untouched_preserved'])
        write = next(t for t in self.server.device.traces if t['method'] == 'POST')
        self.assertEqual(200, write['status'])
        self.assertFalse(write['applied'])

    def test_repeat_is_an_idempotent_set_not_toggle(self):
        first = self.apply({'segment': 2, 'on': False})
        second = self.apply({'segment': 2, 'on': False})
        self.assertEqual('acknowledged', first.status)
        self.assertEqual('acknowledged', second.status)
        self.assertEqual(first.detail['after'], second.detail['after'])
        writes = [t['body'] for t in self.server.device.traces if t['method'] == 'POST']
        self.assertEqual(2, len(writes))
        self.assertEqual(writes[0], writes[1])

    def test_peer_changing_a_different_segment_is_not_success(self):
        apply = self.server.device.apply
        def misbehaving_peer(body):
            result = apply(body)
            with self.server.device.lock:
                self.server.device.state['seg'][2]['on'] = False
            return result
        self.server.device.apply = misbehaving_peer
        result = self.apply({'segment': 0, 'brightness': 200})
        self.assertEqual('failed', result.status)
        self.assertTrue(result.detail['readback_matches'])
        self.assertFalse(result.detail['untouched_preserved'])

    def test_invalid_input_does_not_make_network_request(self):
        for plan in ({'segment': True, 'on': True}, {'segment': -1, 'on': True},
                     {'segment': 0, 'on': 1}, {'segment': 0, 'brightness': 256},
                     {'segment': 0, 'brightness': float('nan')}, {'segment': 0},
                     {'segment': 0, 'color': [0, 0, True]},
                     {'segment': 0, 'on': True, 'host': 'example.com'}):
            with self.subTest(plan=plan):
                result = self.apply(plan)
                self.assertEqual('invalid_plan', result.detail['error'])
        self.assertEqual([], self.server.device.traces)

    def test_unknown_segment_reads_but_never_writes(self):
        result = self.apply({'segment': 9, 'on': True})
        self.assertEqual('unknown_segment', result.detail['error'])
        self.assertFalse(any(t['method'] == 'POST' for t in self.server.device.traces))

    def test_cancelled_before_start_never_contacts_peer(self):
        cancel = threading.Event()
        cancel.set()
        result = self.adapter.apply({'segment': 0, 'on': True}, cancel)
        self.assertEqual('stopped', result.status)
        self.assertEqual([], self.server.device.traces)

    def test_deadline_bounds_client_retries(self):
        self.server.device.set_fault('timeout')
        start = time.monotonic()
        result = self.apply({'segment': 0, 'on': True})
        self.assertEqual('failed', result.status)
        self.assertLess(time.monotonic() - start, 1.4)
        self.assertFalse(result.physically_verified)
        self.assertFalse(any(t['method'] == 'POST' for t in self.server.device.traces))

    def test_stop_cancels_wait_and_adapter_can_be_reused(self):
        self.server.device.set_fault('timeout')
        result = []
        worker = threading.Thread(target=lambda: result.append(self.apply({'segment': 0, 'on': False})))
        worker.start()
        limit = time.monotonic() + 1
        while not self.server.device.traces and time.monotonic() < limit:
            time.sleep(.005)
        self.assertTrue(self.server.device.traces)
        start = time.monotonic()
        self.adapter.stop()
        self.adapter.stop()
        worker.join(timeout=.5)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - start, .5)
        self.assertEqual('stopped', result[0].status)
        self.server.device.set_fault('none')
        self.assertEqual('acknowledged', self.apply({'segment': 0, 'on': False}).status)

    def test_status_source_is_configuration_not_peer_claim(self):
        configured_real = WLEDLightAdapter('127.0.0.1', self.server.server_port, timeout=1)
        state = configured_real.status()
        self.assertEqual('device_reported', state['source'])
        self.assertFalse(state['simulated'])
        self.assertFalse(state['physically_verified'])
        self.assertEqual(3, len(state['state']['segments']))


if __name__ == '__main__':
    unittest.main()
