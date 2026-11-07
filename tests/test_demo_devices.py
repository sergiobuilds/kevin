"""Integration checks: browser endpoint → adapter → actual loopback protocol."""
import importlib.util
import concurrent.futures
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import demo_server


@unittest.skipUnless(all(importlib.util.find_spec(name) for name in ('wled', 'adb_shell', 'broadlink')),
                     'optional requirements-demo.txt not installed')
class ExtendedDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = demo_server.make_server('127.0.0.1', 0, out_dir=cls.temp.name, adapter_timeout=.5)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)
        cls.temp.cleanup()

    def post(self, body):
        request = Request(self.url + '/api/action', json.dumps(body).encode(),
                          {'Content-Type': 'application/json', 'Origin': self.url})
        try:
            reply = urlopen(request, timeout=8)
        except HTTPError as exc:
            reply = exc
        with reply:
            return reply.status, json.load(reply)

    def run_device(self, device, plan):
        code, result = self.post({'action': 'device', 'device': device, 'plan': plan})
        self.assertEqual(code, 200, result)
        self.assertEqual(result['source'], 'simulation')
        self.assertFalse(result['physically_verified'])
        self.assertIsNone(result['receipt'])
        return result

    def setUp(self):
        self.post({'action': 'reset'})

    def test_all_four_tracks_through_http(self):
        self.assertEqual(self.run_device('wallpad', {'channel': 'living_1', 'on': True})['status'], 'acknowledged')
        for plan in ({'action': 'snapshot'}, {'action': 'launch', 'app': 'youtube'}, {'action': 'restore'}):
            self.assertEqual(self.run_device('tv', plan)['status'], 'acknowledged')
        for name in ('stop', 'forward'):
            self.assertEqual(self.run_device('ir', {'op': 'learn', 'name': name})['status'], 'acknowledged')
        sent = self.run_device('ir', {'op': 'send', 'name': 'forward', 'hold_seconds': .1})
        self.assertIn(sent['status'], ('acknowledged', 'sent_unverified'))
        for plan in ({'action': 'clean_zone', 'zone': 'entry'}, {'action': 'pause'}):
            self.assertEqual(self.run_device('vacuum', plan)['status'], 'acknowledged')

    def test_invalid_plan_never_becomes_success(self):
        for body in ({'action': 'device', 'device': 'tv', 'plan': {'action': 'launch', 'app': 'x;reboot'}},
                     {'action': 'device', 'device': 'wallpad', 'plan': {'channel': 'gas', 'on': True}},
                     {'action': 'device', 'device': 'vacuum', 'plan': {'action': 'clean_zone', 'zone': 'unknown'}},
                     {'action': 'device', 'device': 'ir', 'plan': {'op': 'send', 'name': 'forward', 'code': '00'}},
                     {'action': 'device', 'device': 'tv', 'plan': [], 'host': 'external.invalid'}):
            code, result = self.post(body)
            self.assertTrue(code == 400 or result.get('status') == 'failed', result)

    def test_ignored_wallpad_and_vacuum_commands_fail_readback(self):
        for device, plan in (('wallpad', {'channel': 'living_1', 'on': True}),
                             ('vacuum', {'action': 'clean_zone', 'zone': 'entry'})):
            self.post({'action': 'reset'})
            self.post({'action': 'device_fault', 'device': device, 'fault': 'ignore'})
            self.assertEqual(self.run_device(device, plan)['status'], 'failed')

    def test_reset_discards_tv_restore_and_ir_codes(self):
        self.run_device('tv', {'action': 'snapshot'})
        self.run_device('ir', {'op': 'learn', 'name': 'stop'})
        self.post({'action': 'reset'})
        self.assertEqual(self.run_device('tv', {'action': 'restore'})['status'], 'failed')
        self.assertEqual(self.run_device('ir', {'op': 'send', 'name': 'stop'})['status'], 'failed')

    def test_stop_failure_is_visible_and_faults_do_not_cross_devices(self):
        self.run_device('vacuum', {'action': 'clean_zone', 'zone': 'entry'})
        self.post({'action': 'device_fault', 'device': 'vacuum', 'fault': 'ignore'})
        self.assertEqual(self.run_device('wallpad', {'channel': 'living_1', 'on': True})['status'], 'acknowledged')
        code, stopped = self.post({'action': 'stop'})
        self.assertEqual(code, 200)
        self.assertEqual(stopped['status'], 'failed')
        self.assertEqual(stopped['device_results']['vacuum']['status'], 'failed')
        fixture = self.server.app.devices.snapshot()['vacuum']
        self.assertEqual(fixture['vacuum']['status'], 'cleaning')

    def test_new_action_waits_until_stop_response_finishes(self):
        self.run_device('tv', {'action': 'snapshot'})
        stopping, release = threading.Event(), threading.Event()

        def slow_stop():
            stopping.set()
            release.wait(2)
            return {}

        with patch.object(self.server.app.devices, 'stop', side_effect=slow_stop), \
                concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.post, {'action': 'stop'})
            self.assertTrue(stopping.wait(1))
            try:
                result = self.run_device('tv', {'action': 'launch', 'app': 'youtube'})
                self.assertEqual(result['status'], 'busy')
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=2)[1]['status'], 'stopped')
