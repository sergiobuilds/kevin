import copy
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demos.household_sim.server import make_server
from devices.vacuum import DEFAULT_ZONES, ValetudoVacuumAdapter


class VacuumTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.adapter = ValetudoVacuumAdapter(f'http://127.0.0.1:{self.server.server_port}',
                                             DEFAULT_ZONES, simulated=True, timeout=1)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def clean(self):
        return self.adapter.apply({'action': 'clean_zone', 'zone': 'entry'}, threading.Event())

    def test_real_http_zone_payload_readback_and_pause(self):
        result = self.clean()
        self.assertEqual('acknowledged', result.status)
        self.assertEqual('zone_cleaning_started', result.detail['outcome'])
        self.assertFalse(result.physically_verified)
        self.assertFalse(result.detail['cleanliness_verified'])
        writes = [t for t in self.server.device.traces if t['method'] == 'PUT']
        self.assertEqual(1, len(writes))
        self.assertEqual('/api/v2/robot/capabilities/ZoneCleaningCapability', writes[0]['path'])
        self.assertEqual({'action': 'clean', 'zones': [{'points': DEFAULT_ZONES['entry']['points']}], 'iterations': 1}, writes[0]['body'])
        stopped = self.adapter.stop()
        self.assertEqual('stopped', stopped.status)
        self.assertEqual('paused', stopped.detail['after']['status'])
        self.assertEqual('pause', stopped.detail['stop_kind'])
        self.assertFalse(stopped.physically_verified)
        self.assertEqual('paused', self.server.device.snapshot()['vacuum']['status'])
        self.assertEqual({'action': 'pause'}, [t for t in self.server.device.traces if t['method'] == 'PUT'][-1]['body'])

    def test_known_zone_only_and_no_caller_coordinates(self):
        for plan in ({'action': 'clean_zone', 'zone': 'unknown'}, {'action': 'start'},
                     {'action': 'clean_zone', 'zone': 'entry', 'points': {}},
                     {'action': 'pause', 'zone': 'entry'}, {'action': 'clean_zone'}):
            self.assertEqual('invalid_plan', self.adapter.apply(plan, threading.Event()).detail['error'])
        self.assertEqual([], self.server.device.traces)

    def test_configuration_is_copied_and_zone_geometry_is_validated(self):
        config = copy.deepcopy(DEFAULT_ZONES)
        other = ValetudoVacuumAdapter(self.adapter.base_url, config, simulated=True)
        config['entry']['points']['pA']['x'] = 999
        self.assertEqual(100, other.zones['entry']['points']['pA']['x'])
        with self.assertRaises(ValueError):
            ValetudoVacuumAdapter(self.adapter.base_url, config)

    def test_missing_capability_or_unknown_state_never_starts(self):
        self.server.device.capabilities.remove('ZoneCleaningCapability')
        self.assertEqual('capability_unavailable', self.clean().detail['error'])
        self.server.device.reset()
        self.server.device.set_fault('unavailable')
        self.assertEqual('unknown_or_unavailable_state', self.clean().detail['error'])
        self.assertFalse(any(t['method'] == 'PUT' for t in self.server.device.traces))

    def test_busy_robot_does_not_restart_the_current_job(self):
        self.assertEqual('acknowledged', self.clean().status)
        result = self.clean()
        self.assertEqual('robot_busy', result.detail['error'])
        self.assertEqual(1, sum(t['method'] == 'PUT' for t in self.server.device.traces))

    def test_ignored_command_fails_even_with_http_200(self):
        self.server.device.set_fault('ignore')
        result = self.clean()
        self.assertEqual('failed', result.status)
        self.assertFalse(result.detail['readback_matches'])
        self.assertEqual('unconfirmed', result.detail['outcome'])
        self.assertFalse(result.detail['cleanliness_verified'])

    def test_pause_does_not_claim_success_when_robot_keeps_cleaning(self):
        self.clean()
        self.server.device.set_fault('ignore')
        result = self.adapter.stop()
        self.assertEqual('failed', result.status)
        self.assertFalse(result.detail['readback_matches'])
        self.assertEqual('cleaning', self.server.device.vacuum['status'])

    def test_repeat_pause_is_idempotent(self):
        self.clean()
        self.adapter.stop()
        before = sum(t['method'] == 'PUT' for t in self.server.device.traces)
        result = self.adapter.stop()
        self.assertEqual('stopped', result.status)
        self.assertFalse(result.detail['command_sent'])
        self.assertEqual(before, sum(t['method'] == 'PUT' for t in self.server.device.traces))

    def test_cancel_before_request_and_network_timeout(self):
        cancel = threading.Event()
        cancel.set()
        result = self.adapter.apply({'action': 'clean_zone', 'zone': 'entry'}, cancel)
        self.assertEqual('stopped', result.status)
        self.assertEqual([], self.server.device.traces)
        self.server.device.set_fault('timeout')
        start = time.monotonic()
        self.assertEqual('failed', self.clean().status)
        self.assertLess(time.monotonic() - start, .7)
        self.assertFalse(any(t['method'] == 'PUT' for t in self.server.device.traces))

    def test_stop_is_serialized_after_inflight_clean_command(self):
        entered, release = threading.Event(), threading.Event()
        original = self.server.device.mutate
        def delayed_start(method, path, body):
            if path.endswith('/ZoneCleaningCapability'):
                entered.set()
                release.wait(.2)
            return original(method, path, body)
        self.server.device.mutate = delayed_start
        result = []
        worker = threading.Thread(target=lambda: result.append(self.clean()))
        worker.start()
        self.assertTrue(entered.wait(1))
        timer = threading.Timer(.05, release.set)
        timer.start()
        stopped = self.adapter.stop()
        worker.join(timeout=1)
        timer.join()
        self.assertFalse(worker.is_alive())
        self.assertEqual('stopped', result[0].status)
        self.assertTrue(result[0].detail['command_may_have_been_sent'])
        self.assertEqual('stopped', stopped.status)
        self.assertEqual('paused', self.server.device.vacuum['status'])
        writes = [t['path'] for t in self.server.device.traces if t['method'] == 'PUT']
        self.assertEqual(['/api/v2/robot/capabilities/ZoneCleaningCapability',
                          '/api/v2/robot/capabilities/BasicControlCapability'], writes)

    def test_timed_out_clean_may_execute_after_pause_so_stop_is_unconfirmed(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        original = self.server.device.mutate
        def delayed_start(method, path, body):
            if path.endswith('/ZoneCleaningCapability'):
                entered.set()
                release.wait(2)
                result = original(method, path, body)
                completed.set()
                return result
            return original(method, path, body)
        self.server.device.mutate = delayed_start
        results = []
        worker = threading.Thread(target=lambda: results.append(self.clean()))
        worker.start()
        self.assertTrue(entered.wait(1))
        try:
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual('failed', results[0].status)
            stopped = self.adapter.stop()
            self.assertEqual('failed', stopped.status)
            self.assertEqual('prior_clean_outcome_unknown', stopped.detail['error'])
            self.assertFalse(stopped.detail['inactive_reported'])
        finally:
            release.set()
            worker.join(2)
        self.assertTrue(completed.wait(1))
        self.assertEqual('cleaning', self.server.device.vacuum['status'])
        # A retry can send pause, but cannot erase an unresolved remote write.
        self.assertEqual('failed', self.adapter.stop().status)


if __name__ == '__main__':
    unittest.main()
