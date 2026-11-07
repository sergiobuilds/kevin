import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demos.household_sim.server import DEFAULT_ENTITIES, make_server
from devices.wallpad import HAWallpadAdapter


class WallpadTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.adapter = HAWallpadAdapter(self.base, DEFAULT_ENTITIES, token='fixture-token', simulated=True, timeout=1)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def apply(self, channel='living_1', on=True):
        return self.adapter.apply({'channel': channel, 'on': on}, threading.Event())

    def test_real_http_one_channel_service_and_independent_readback(self):
        before = self.server.device.snapshot()['wallpad']
        result = self.apply()
        self.assertEqual('acknowledged', result.status)
        self.assertFalse(result.physically_verified)
        self.assertEqual('simulation', result.detail['source'])
        self.assertTrue(result.detail['readback_matches'])
        self.assertTrue(result.detail['untouched_preserved'])
        after = self.server.device.snapshot()['wallpad']
        self.assertTrue(after['light.living_1'])
        self.assertEqual(before['light.living_2'], after['light.living_2'])
        self.assertEqual(before['light.hall'], after['light.hall'])
        traces = self.server.device.traces
        self.assertEqual(['GET', 'POST', 'GET'], [t['method'] for t in traces])
        self.assertEqual('/api/services/light/turn_on', traces[1]['path'])
        self.assertEqual({'entity_id': 'light.living_1'}, traces[1]['body'])
        self.assertNotIn('fixture-token', json.dumps(traces))

    def test_last_updated_is_not_mistaken_for_last_observed(self):
        # The fixture deliberately reports old last_updated and fresh
        # last_reported, just like an unchanged light with a fresh heartbeat.
        self.assertEqual('acknowledged', self.apply().status)

    def test_repeat_is_idempotent_without_second_service_call(self):
        self.apply()
        result = self.apply()
        self.assertEqual('acknowledged', result.status)
        self.assertFalse(result.detail['command_sent'])
        self.assertEqual(1, sum(t['method'] == 'POST' for t in self.server.device.traces))

    def test_stale_unknown_and_unavailable_states_never_write(self):
        for fault in ('stale', 'unavailable'):
            with self.subTest(fault=fault):
                self.server.device.reset()
                self.server.device.set_fault(fault)
                self.assertEqual('failed', self.apply().status)
                self.assertFalse(any(t['method'] == 'POST' for t in self.server.device.traces))
        self.server.device.reset()
        self.server.device.lights.pop('light.hall')
        self.assertEqual('unknown_or_unavailable_state', self.apply().detail['error'])

    def test_missing_freshness_never_writes(self):
        original = self.server.device.get
        def missing_timestamp(path):
            value = original(path)
            if path == '/api/states':
                for row in value:
                    row.pop('last_reported')
            return value
        self.server.device.get = missing_timestamp
        self.assertEqual('state_freshness_unknown', self.apply().detail['error'])
        self.assertFalse(any(t['method'] == 'POST' for t in self.server.device.traces))

    def test_ignored_and_collateral_changes_fail_readback(self):
        for fault in ('ignore', 'collateral'):
            with self.subTest(fault=fault):
                self.server.device.reset()
                self.server.device.set_fault(fault)
                result = self.apply()
                self.assertEqual('failed', result.status)
                if fault == 'ignore':
                    self.assertFalse(result.detail['readback_matches'])
                else:
                    self.assertFalse(result.detail['untouched_preserved'])
                self.assertFalse(result.physically_verified)

    def test_invalid_targets_and_arguments_never_connect(self):
        for plan in ({'channel': 'gas', 'on': True}, {'channel': 'living_1', 'on': 1},
                     {'channel': 'living_1', 'on': True, 'url': 'http://example.com'}, []):
            self.assertEqual('invalid_plan', self.adapter.apply(plan, threading.Event()).detail['error'])
        self.assertEqual([], self.server.device.traces)
        with self.assertRaises(ValueError):
            HAWallpadAdapter(self.base, {'lock': 'lock.front_door'})

    def test_numeric_target_and_loopback_fixture_bind_are_required(self):
        for origin in ('http://homeassistant.local', 'http://example.com', 'http://127.0.0.1:99999'):
            with self.assertRaises(ValueError):
                HAWallpadAdapter(origin, DEFAULT_ENTITIES)
        for host in ('0.0.0.0', '192.168.1.10', 'localhost'):
            with self.assertRaises(ValueError):
                make_server(host)

    def test_cancel_and_timeout_are_bounded_without_followup_write(self):
        cancelled = threading.Event()
        cancelled.set()
        self.assertEqual('stopped', self.adapter.apply({'channel': 'living_1', 'on': True}, cancelled).status)
        self.assertEqual([], self.server.device.traces)
        self.server.device.set_fault('timeout')
        result = []
        worker = threading.Thread(target=lambda: result.append(self.apply()))
        worker.start()
        deadline = time.monotonic() + 1
        while not self.server.device.traces and time.monotonic() < deadline:
            time.sleep(.005)
        start = time.monotonic()
        self.adapter.stop()
        worker.join(timeout=.6)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - start, .6)
        self.assertEqual('stopped', result[0].status)
        self.assertFalse(any(t['method'] == 'POST' for t in self.server.device.traces))
        self.server.device.set_fault('none')
        self.assertEqual('acknowledged', self.apply().status)


if __name__ == '__main__':
    unittest.main()
