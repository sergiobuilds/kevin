"""Real python-broadlink authentication, AES and UDP against a synthetic peer."""
import concurrent.futures
import importlib.util
import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demos.ir_sim.server import make_server
from devices.ir import BroadlinkIRAdapter, validate_code


@unittest.skipUnless(importlib.util.find_spec('broadlink'), 'optional requirements-demo.txt not installed')
class IRAdapterTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server()
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.adapter = BroadlinkIRAdapter('127.0.0.1', self.server.server_port, simulated=True, timeout=.5)

    def tearDown(self):
        self.adapter.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def learn(self, name):
        self.server.device.arm(name)
        return self.adapter.apply({'op': 'learn', 'name': name}, threading.Event())

    def pair(self):
        self.assertEqual(self.learn('stop').status, 'acknowledged')
        self.assertEqual(self.learn('forward').status, 'acknowledged')

    def wait_for(self, predicate, timeout=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.005)
        self.fail('timed out waiting for fixture state')

    def test_real_auth_learn_send_and_paired_stop_roundtrip(self):
        self.pair()
        learned = self.adapter.status()['codes']
        result = self.adapter.apply({'op': 'send', 'name': 'forward', 'hold_seconds': .05}, threading.Event())
        self.assertEqual(result.status, 'sent_unverified')
        self.assertFalse(result.physically_verified)
        self.assertEqual(result.detail['physical_state'], 'unknown')
        self.assertTrue(result.detail['forward_acknowledged'])
        self.assertTrue(result.detail['stop_acknowledged'])
        snapshot = self.server.device.snapshot()
        self.assertEqual(snapshot['state']['train'], 'stopped')
        self.assertEqual([x['name'] for x in snapshot['sent']], ['forward', 'stop'])
        self.assertEqual(snapshot['sent'][0]['code_sha256'], learned['forward']['sha256'])
        self.assertEqual(snapshot['sent'][1]['code_sha256'], learned['stop']['sha256'])
        self.assertEqual([t['operation'] for t in snapshot['traces']],
                         ['auth', 'learn', 'check', 'learn', 'check', 'send', 'send'])
        self.assertTrue(all(t['checksum_valid'] for t in snapshot['traces']))
        self.assertTrue(all(t['encrypted_payload_bytes'] % 16 == 0 for t in snapshot['traces']))

    def test_forward_requires_distinct_learned_stop_before_any_send(self):
        self.assertEqual(self.learn('forward').status, 'acknowledged')
        before = len(self.server.device.snapshot()['traces'])
        result = self.adapter.apply({'op': 'send', 'name': 'forward'}, threading.Event())
        self.assertEqual(result.detail['error'], 'distinct_stop_code_required')
        self.assertEqual(len(self.server.device.snapshot()['traces']), before)
        self.server.device.arm('forward')
        duplicate = self.adapter.apply({'op': 'learn', 'name': 'stop'}, threading.Event())
        self.assertEqual(duplicate.detail['error'], 'distinct_stop_code_required')
        self.assertEqual(self.adapter.status()['learned_names'], ['forward'])

    def test_invalid_and_unknown_commands_do_not_touch_network(self):
        plans = [{'op': 'send', 'name': 'power'}, {'op': 'learn', 'name': 'forward', 'hold_seconds': .2},
                 {'op': 'send', 'name': 'forward', 'hold_seconds': 2},
                 {'op': 'send', 'name': 'forward', 'hold_seconds': True},
                 {'op': 'send', 'name': 'forward', 'hold_seconds': float('nan')},
                 {'op': 'send', 'name': 'stop', 'host': '192.168.0.1'}, {'op': 'send', 'name': 'stop'}]
        for plan in plans:
            with self.subTest(plan=plan):
                self.assertEqual(self.adapter.apply(plan, threading.Event()).status, 'failed')
        self.assertEqual(self.server.device.snapshot()['traces'], [])

    def test_truncated_learned_packet_is_never_stored(self):
        self.server.device.set_fault('bad_code')
        result = self.learn('stop')
        self.assertEqual(result.detail['error'], 'invalid_learned_code')
        self.assertEqual(self.adapter.status()['learned_names'], [])

    def test_timeout_and_missing_code_are_bounded(self):
        for fault in ('timeout', 'no_code'):
            self.adapter.reset()
            self.server.device.reset()
            self.server.device.set_fault(fault)
            start = time.monotonic()
            result = self.learn('stop')
            self.assertLess(time.monotonic() - start, .9)
            self.assertEqual(result.detail['error'], 'timeout')
            self.assertFalse(result.physically_verified)

    def test_rejected_and_corrupt_responses_are_not_success(self):
        for fault, error in (('reject', 'unsupported_by_hub'), ('bad_checksum', 'invalid_response')):
            self.adapter.reset()
            self.server.device.reset()
            self.server.device.set_fault(fault)
            result = self.learn('stop')
            self.assertEqual(result.status, 'failed')
            self.assertEqual(result.detail['error'], error)

    def test_pre_cancelled_operation_sends_no_packets(self):
        cancel = threading.Event()
        cancel.set()
        result = self.adapter.apply({'op': 'learn', 'name': 'stop'}, cancel)
        self.assertEqual(result.status, 'stopped')
        self.assertEqual(self.server.device.snapshot()['traces'], [])

    def test_cancelling_learning_does_not_publish_a_code(self):
        self.server.device.set_fault('no_code')
        self.server.device.arm('stop')
        cancel = threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.adapter.apply, {'op': 'learn', 'name': 'stop'}, cancel)
            self.wait_for(lambda: self.server.device.snapshot()['learning'])
            start = time.monotonic()
            cancel.set()
            result = pending.result(timeout=1)
            self.assertLess(time.monotonic() - start, .4)
        self.assertEqual(result.status, 'stopped')
        self.assertEqual(self.adapter.status()['learned_names'], [])

    def test_stop_during_forward_transmits_paired_stop_without_repeating_forward(self):
        self.pair()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.adapter.apply, {'op': 'send', 'name': 'forward', 'hold_seconds': .4}, threading.Event())
            self.wait_for(lambda: self.server.device.snapshot()['state']['train'] == 'moving')
            self.adapter.stop()
            result = pending.result(timeout=1)
        self.assertEqual(result.status, 'stopped')
        self.assertTrue(result.detail['stop_acknowledged'])
        self.assertEqual([x['name'] for x in self.server.device.snapshot()['sent']], ['forward', 'stop'])
        self.assertEqual(self.server.device.snapshot()['state']['train'], 'stopped')

    def test_failed_stop_remains_unknown_and_can_be_retried_explicitly(self):
        self.pair()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.adapter.apply, {'op': 'send', 'name': 'forward', 'hold_seconds': .4}, threading.Event())
            self.wait_for(lambda: self.server.device.snapshot()['state']['train'] == 'moving')
            self.server.device.set_fault('timeout')
            self.adapter.stop()
            result = pending.result(timeout=1)
        self.assertEqual(result.status, 'stopped')
        self.assertEqual(result.detail['error'], 'stop_unconfirmed')
        self.assertTrue(self.adapter.status()['stop_transmission_unconfirmed'])
        self.server.device.set_fault('none')
        retried = self.adapter.stop()
        self.assertEqual(retried.status, 'sent_unverified')
        self.assertEqual(self.server.device.snapshot()['state']['train'], 'stopped')

    def test_hub_ack_does_not_claim_target_motion_when_fixture_ignores_ir(self):
        self.pair()
        self.server.device.set_fault('ignore')
        result = self.adapter.apply({'op': 'send', 'name': 'forward', 'hold_seconds': .05}, threading.Event())
        self.assertEqual(result.status, 'sent_unverified')
        self.assertFalse(result.physically_verified)
        self.assertTrue(result.detail['forward_acknowledged'])
        self.assertTrue(all(not x['applied'] for x in self.server.device.snapshot()['sent']))

    def test_reset_refuses_active_work_and_clears_learned_codes_when_idle(self):
        self.learn('stop')
        self.server.device.set_fault('no_code')
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.learn, 'forward')
            self.wait_for(lambda: self.adapter.status()['busy'])
            self.assertEqual(self.adapter.reset().detail['error'], 'busy')
            self.adapter.stop()
            pending.result(timeout=1)
        self.assertEqual(self.adapter.reset().status, 'acknowledged')
        self.assertEqual(self.adapter.status()['learned_names'], [])

    def test_status_exposes_hashes_without_codes_credentials_or_addresses(self):
        self.pair()
        status = self.adapter.status()
        text = json.dumps(status)
        self.assertNotIn('127.0.0.1', text)
        self.assertNotIn('session_key', text)
        self.assertEqual(status['source'], 'simulation')
        for metadata in status['codes'].values():
            self.assertEqual(set(metadata), {'sha256', 'bytes'})

    def test_packet_validation_and_loopback_boundary(self):
        for packet in (b'', b'\x26\x00\x10\x00\x01', b'\x26\x00\x01\x00\x00',
                       b'\x00\x00\x01\x00\x01', b'\x26\x00\x01\x00\x01\x01',
                       b'\x26\x02\x01\x00\x01', b'\x26\x00\x03\x00\x00\xff\xff'):
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                validate_code(packet)
        with self.assertRaises(ValueError):
            BroadlinkIRAdapter('192.168.1.1', simulated=True)
        with self.assertRaises(ValueError):
            make_server('0.0.0.0')


if __name__ == '__main__':
    unittest.main()
