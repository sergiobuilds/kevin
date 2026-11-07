import concurrent.futures
import importlib.util
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import demo_server
import receipt
from devices.base import Result


@unittest.skipUnless(importlib.util.find_spec('wled'), 'optional requirements-demo.txt not installed')
class DemoServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.static = cls.root / 'static'
        cls.static.mkdir()
        (cls.static / 'index.html').write_text('<h1>Simulation</h1>')
        (cls.static / 'demo.js').write_text('console.log("simulation");')
        (cls.static / 'demo.css').write_text('body { color: white; }')
        (cls.static / 'review.html').write_text('<iframe src="/"></iframe>')
        # Physical device settings must never change the server's destinations.
        with patch.dict('os.environ', KEVIN_SHOW_LIVE='1', KEVIN_LED_ADDRESS='never-contact',
                        KEVIN_RADIO_DEVICE='CRF-35B', KEVIN_CAMERA='0'):
            cls.server = demo_server.make_server('127.0.0.1', 0, out_dir=cls.root / 'runtime',
                                                 static_dir=cls.static, adapter_timeout=.25)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tmp.cleanup()

    def setUp(self):
        self.post({'action': 'reset'})

    def request(self, path, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read(), dict(response.headers)

    def post(self, body, headers=None):
        content_headers = {'Content-Type': 'application/json', 'Origin': self.base}
        content_headers.update(headers or {})
        code, data, _ = self.request('/api/action', json.dumps(body).encode(), content_headers)
        return code, json.loads(data)

    def state(self):
        code, data, _ = self.request('/api/state')
        self.assertEqual(code, 200)
        return json.loads(data)

    def test_actual_wled_client_changes_only_requested_segment(self):
        before = self.state()['device']['state']['seg']
        code, result = self.post({'action': 'light', 'segment': 1, 'brightness': 201, 'on': True})
        self.assertEqual(code, 200)
        self.assertEqual(result['status'], 'acknowledged')
        self.assertEqual(result['result']['detail']['client'], 'python-wled 1.1.1')
        self.assertTrue(result['result']['detail']['readback_matches'])
        self.assertTrue(result['result']['detail']['untouched_preserved'])
        after = self.state()['device']['state']['seg']
        self.assertEqual(after[1]['bri'], 201)
        self.assertEqual([after[0], after[2]], [before[0], before[2]])
        traces = self.state()['device']['traces']
        self.assertTrue(any(t['method'] == 'POST' and t['applied'] for t in traces))
        self.assertTrue(any(t['method'] == 'GET' for t in traces))
        self.assertFalse(result['physically_verified'])

    def test_dark_scene_uses_client_and_signed_simulation_images(self):
        _, result = self.post({'action': 'observe', 'scenario': 'dark'})
        self.assertEqual(result['status'], 'verified')
        self.assertTrue(result['result']['command_sent'])
        self.assertTrue(result['result']['observed_bright'])
        self.assertTrue(result['receipt']['verified'])
        self.assertEqual(result['receipt']['observation']['source'], 'simulation')
        self.assertFalse(result['physically_verified'])
        _, raw, _ = self.request(result['receipt']['url'])
        stored = json.loads(raw)
        images = {}
        for key in ('before.jpg', 'after1.jpg'):
            url = result['receipt']['url'].replace('receipt.json', key)
            code, image, headers = self.request(url)
            self.assertEqual(code, 200)
            self.assertEqual(headers['Content-Type'], 'image/jpeg')
            images[key] = image
        self.assertTrue(receipt.check(stored, images))

    def test_already_bright_sends_no_device_requests(self):
        _, result = self.post({'action': 'observe', 'scenario': 'already_bright'})
        self.assertEqual(result['status'], 'already_bright')
        self.assertFalse(result['result']['command_sent'])
        self.assertEqual(self.state()['device']['traces'], [])
        self.assertFalse(result['receipt']['observation']['changed'])

    def test_stale_observation_preserves_state_without_any_command(self):
        before = self.state()['device']['state']
        _, result = self.post({'action': 'observe', 'scenario': 'stale'})
        self.assertEqual(result['status'], 'stale')
        self.assertFalse(result['result']['command_sent'])
        self.assertEqual(self.state()['device']['state'], before)
        self.assertEqual(self.state()['device']['traces'], [])
        self.assertIsNone(result['receipt'])

    def test_ignored_command_and_unlit_image_are_not_success(self):
        self.post({'action': 'fault', 'fault': 'ignore'})
        _, result = self.post({'action': 'observe', 'scenario': 'dark'})
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['result']['device']['detail']['readback_matches'])
        self.assertFalse(result['result']['observed_bright'])
        self.post({'action': 'reset'})
        _, result = self.post({'action': 'observe', 'scenario': 'still_dark'})
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['result']['device']['detail']['readback_matches'])
        self.assertFalse(result['result']['observed_bright'])

    def test_timeout_does_not_reuse_previous_command_sent_flag(self):
        self.post({'action': 'light', 'segment': 1, 'brightness': 201})
        self.post({'action': 'fault', 'fault': 'timeout'})
        _, result = self.post({'action': 'observe', 'scenario': 'dark'})
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['result']['command_sent'])
        self.assertEqual(result['result']['device']['detail']['error'], 'timeout')

    def test_stop_is_processed_while_an_action_waits_and_lights_are_preserved(self):
        self.post({'action': 'fault', 'fault': 'timeout'})
        before = self.state()['device']['state']
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.post, {'action': 'light', 'segment': 1, 'brightness': 201})
            deadline = time.monotonic() + 2
            while not self.state()['busy'] and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(self.state()['busy'])
            start = time.monotonic()
            _, stopped = self.post({'action': 'stop'})
            self.assertLess(time.monotonic() - start, .2)
            self.assertEqual(stopped['status'], 'stopped')
            _, result = pending.result(timeout=2)
        self.assertEqual(result['status'], 'stopped')
        self.assertIsNone(result['receipt'])
        self.assertEqual(self.state()['device']['state'], before)

    def test_stop_during_operation_registration_cancels_the_new_token(self):
        app = self.server.app
        acquired, continue_start, stop_started = threading.Event(), threading.Event(), threading.Event()
        original_lock = app._operation

        class GatedLock:
            def acquire(self, blocking=False):
                value = original_lock.acquire(blocking=blocking)
                if value:
                    acquired.set()
                    continue_start.wait(2)
                return value

            def release(self):
                original_lock.release()

            def locked(self):
                return original_lock.locked()

        def wait_for_cancel(_plan, cancel):
            return Result('stopped' if cancel.wait(1) else 'acknowledged')

        def stop():
            stop_started.set()
            return app.action({'action': 'stop'})

        with patch.object(app, '_operation', GatedLock()), \
                patch.object(app.adapter, 'apply', side_effect=wait_for_cancel), \
                concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(app.action, {'action': 'light', 'segment': 0, 'brightness': 220})
            self.assertTrue(acquired.wait(1))
            stopping = pool.submit(stop)
            self.assertTrue(stop_started.wait(1))
            try:
                # stop waits for registration, then receives the new token.
                with self.assertRaises(concurrent.futures.TimeoutError):
                    stopping.result(timeout=.05)
            finally:
                continue_start.set()
            self.assertEqual(stopping.result(timeout=2)['status'], 'stopped')
            result = pending.result(timeout=2)
        self.assertEqual(result['status'], 'stopped')
        self.assertIsNone(result['receipt'])
        self.assertEqual(app.state()['last_result']['status'], 'stopped')

    def test_cancel_after_adapter_ack_skips_receipt_and_reports_action_separately(self):
        app = self.server.app

        def cancel_after_ack(_plan, cancel):
            cancel.set()
            return Result('acknowledged', detail={'readback_matches': True})

        with patch.object(app.adapter, 'apply', side_effect=cancel_after_ack), \
                patch.object(demo_server, 'make_receipt') as writer:
            result = app.action({'action': 'light', 'segment': 0, 'brightness': 220})
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['action_status'], 'acknowledged')
        self.assertIsNone(result['receipt'])
        writer.assert_not_called()

    def test_late_receipt_cannot_overwrite_stop_with_success(self):
        app = self.server.app
        written, release = threading.Event(), threading.Event()
        original_make_receipt = demo_server.make_receipt

        def delayed_receipt(*args, **kwargs):
            value = original_make_receipt(*args, **kwargs)
            written.set()
            release.wait(2)
            return value

        with patch.object(demo_server, 'make_receipt', side_effect=delayed_receipt), \
                concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.post, {'action': 'light', 'segment': 1, 'brightness': 201})
            self.assertTrue(written.wait(1))
            try:
                _, stopped = self.post({'action': 'stop'})
                self.assertEqual(stopped['status'], 'stopped')
            finally:
                release.set()
            _, result = pending.result(timeout=2)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['action_status'], 'acknowledged')
        self.assertIsNone(result['receipt'])
        self.assertIsNone(result['after_url'])
        self.assertEqual(self.state()['last_result']['status'], 'stopped')

    def test_foreign_origin_content_type_and_oversized_body_are_rejected(self):
        body = {'action': 'light', 'segment': 0, 'brightness': 220}
        self.assertEqual(self.post(body, {'Origin': 'https://foreign.example'})[0], 403)
        self.assertEqual(self.post(body, {'Origin': 'null'})[0], 403)
        self.assertEqual(self.post(body, {'Origin': 'http://['})[0], 403)
        self.assertEqual(self.post(body, {'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.post(body, {'Sec-Fetch-Site': 'cross-site'})[0], 403)
        code, _, _ = self.request('/api/action', b' ' * (demo_server.MAX_BODY + 1),
                                  {'Content-Type': 'application/json'})
        self.assertEqual(code, 413)
        self.assertEqual(self.state()['device']['traces'], [])

    def test_invalid_device_or_destination_input_is_rejected_without_network_calls(self):
        for body in ({'action': 'light', 'segment': True, 'brightness': 5},
                     {'action': 'light', 'segment': 3, 'brightness': 5},
                     {'action': 'light', 'segment': 0, 'brightness': 256},
                     {'action': 'light', 'segment': 0, 'url': 'http://private-device'},
                     {'action': 'light', 'segment': 0, 'on': 'true'},
                     {'action': 'observe', 'scenario': 'dark', 'path': '../camera.jpg'}):
            with self.subTest(body=body):
                self.assertEqual(self.post(body)[0], 400)
        self.assertEqual(self.state()['device']['traces'], [])

    def test_static_and_evidence_routes_never_serve_arbitrary_files_or_keys(self):
        for route in ('/', '/demo.js', '/demo.css', '/review.html'):
            self.assertEqual(self.request(route)[0], 200)
        for route in ('/../src/receipt.py', '/.runtime/demo-signing-key.pem', '/src/receipt.py',
                      '/evidence/../../demo-signing-key.pem', '/evidence/%2e%2e/demo-signing-key.pem'):
            self.assertEqual(self.request(route)[0], 404)
        _, result = self.post({'action': 'observe', 'scenario': 'dark'})
        rid = result['receipt']['id']
        self.assertEqual(self.request(f'/evidence/{rid}/demo-signing-key.pem')[0], 404)
        self.assertEqual(self.request(f'/evidence/{rid}/after2.jpg')[0], 404)


if __name__ == '__main__':
    unittest.main()
