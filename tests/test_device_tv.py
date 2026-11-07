"""Real adb-shell framing and TCP; the remote TV state is explicitly simulated."""
import threading
import time
import unittest
from unittest.mock import patch

from devices.tv import AndroidTVAdapter
from demos.tv_sim.server import APPS, make_server, start_fixture


class TVTests(unittest.TestCase):
    def setUp(self):
        self.server, self.adapter = start_fixture(timeout=.5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def apply(self, action, **args):
        return self.adapter.apply(dict(action=action, **args), threading.Event())

    def test_real_adb_transport_snapshots_launches_and_restores_foreground(self):
        first = self.apply('snapshot')
        launched = self.apply('launch', app='youtube')
        restored = self.apply('restore')
        for result in (first, launched, restored):
            self.assertEqual(result.status, 'acknowledged', result.detail)
            self.assertTrue(result.detail['readback_matches'])
            self.assertEqual(result.detail['source'], 'simulation')
            self.assertEqual(result.detail['transport'], 'adb_tcp')
            self.assertEqual(result.detail['client'], 'adb-shell 0.4.4')
            self.assertEqual(result.detail['verification_scope'], 'foreground_app_only')
            self.assertFalse(result.physically_verified)
            self.assertFalse(result.detail['playback_position_restored'])
        self.assertEqual(first.detail['saved_app'], 'home')
        self.assertEqual(launched.detail['current_app'], 'youtube')
        self.assertEqual(restored.detail['current_app'], 'home')
        traces = self.server.device.snapshot()['traces']
        commands = [t['command'] for t in traces if t['kind'] == 'adb']
        self.assertEqual(commands.count('CNXN'), 3)
        self.assertIn('OPEN', commands)
        self.assertIn('OKAY', commands)
        launches = [t for t in traces if t['kind'] == 'shell' and t['command'].startswith('am ')]
        self.assertEqual([t['app'] for t in launches], ['youtube', 'home'])
        self.assertTrue(all(t['applied'] for t in launches))

    def test_status_reads_current_foreground_without_saving_or_launching(self):
        state = self.adapter.status()
        self.assertEqual(state['status'], 'connected')
        self.assertEqual(state['current_app'], 'home')
        self.assertIsNone(state['saved_app'])
        self.assertEqual(state['apps'], ['home', 'youtube'])
        self.assertFalse(any(t.get('applied') for t in self.server.device.snapshot()['traces']))

    def test_restore_without_snapshot_never_connects(self):
        result = self.apply('restore')
        self.assertEqual(result.detail['error'], 'no_snapshot')
        self.assertEqual(self.server.device.snapshot()['traces'], [])

    def test_empty_snapshot_clears_previous_saved_app_and_cannot_restore(self):
        self.assertEqual(self.apply('snapshot').status, 'acknowledged')
        self.server.device.set_fault('empty')
        result = self.apply('snapshot')
        self.assertEqual(result.detail['error'], 'foreground_unavailable')
        self.assertIsNone(result.detail['saved_app'])
        self.server.device.set_fault('none')
        self.assertEqual(self.apply('restore').detail['error'], 'no_snapshot')

    def test_invalid_actions_aliases_and_shell_injection_have_no_transport_effect(self):
        invalid = [None, [], {}, {'action': []}, {'action': 'shell', 'command': 'id'},
                   {'action': 'launch', 'app': 'missing'},
                   {'action': 'launch', 'app': 'com.android.tv.launcher'},
                   {'action': 'launch', 'app': 'youtube; echo bad'},
                   {'action': 'launch', 'app': 'youtube', 'host': 'elsewhere'},
                   {'action': 'snapshot', 'app': 'home'}, {'action': 'restore', 'app': 'home'}]
        for plan in invalid:
            with self.subTest(plan=plan):
                result = self.adapter.apply(plan, threading.Event())
                self.assertEqual(result.status, 'failed')
                self.assertEqual(result.detail['error'], 'invalid_plan')
        self.assertEqual(self.server.device.snapshot()['traces'], [])

    def test_configuration_rejects_injection_and_ambiguous_packages(self):
        cases = [dict(host='example.invalid'), dict(host=True), dict(host=None),
                 dict(host='0.0.0.0'), dict(host='224.0.0.1'),
                 dict(host='192.0.2.10', simulated=True),
                 dict(port=True), dict(timeout=float('nan')),
                 dict(simulated='true'), dict(apps={'bad': 'com.test/.Main;echo bad'}),
                 dict(apps={'a': 'com.test/.One', 'b': 'com.test/.Two'})]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                AndroidTVAdapter(**dict({'host': '127.0.0.1', 'apps': APPS}, **changes))

    def test_ignored_launch_is_not_acknowledged_as_success(self):
        self.server.device.set_fault('ignore')
        result = self.apply('launch', app='youtube')
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.detail['error'], 'readback_mismatch')
        self.assertFalse(result.detail['readback_matches'])
        self.assertEqual(result.detail['current_app'], 'home')

    def test_offline_peer_fails_without_launch_or_physical_claim(self):
        self.server.device.set_fault('offline')
        result = self.apply('launch', app='youtube')
        self.assertEqual(result.status, 'failed')
        self.assertFalse(result.physically_verified)
        self.assertEqual(self.server.device.snapshot()['current_app'], 'home')
        self.assertFalse(any(t['kind'] == 'shell' for t in self.server.device.snapshot()['traces']))

    def test_silent_peer_has_bounded_total_timeout(self):
        self.server.device.set_fault('timeout')
        started = time.monotonic()
        result = self.apply('snapshot')
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.detail['error'], 'timeout')
        self.assertLess(time.monotonic() - started, 1)

    def test_cancel_before_execution_never_connects(self):
        cancel = threading.Event()
        cancel.set()
        result = self.adapter.apply({'action': 'launch', 'app': 'youtube'}, cancel)
        self.assertEqual(result.status, 'stopped')
        self.assertEqual(self.server.device.snapshot()['traces'], [])

    def test_stop_interrupts_silent_adb_peer_and_busy_status_does_not_overlap(self):
        self.server.device.set_fault('timeout')
        result = []
        worker = threading.Thread(target=lambda: result.append(self.apply('snapshot')))
        worker.start()
        self.addCleanup(worker.join, 1)
        deadline = time.monotonic() + 1
        while not self.server.device.snapshot()['traces'] and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(self.server.device.snapshot()['traces'])
        self.assertEqual(self.adapter.status()['error'], 'busy')
        started = time.monotonic()
        self.adapter.stop()
        worker.join(.3)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, .3)
        self.assertEqual(result[0].status, 'stopped')

    def test_unconfigured_foreground_is_not_saved_or_used_as_shell_input(self):
        original = self.server.device.shell
        def unknown(command):
            if command == 'dumpsys window windows':
                return 'mCurrentFocus=Window{123 u0 com.unknown.app/.Main}\n'
            return original(command)
        with patch.object(self.server.device, 'shell', side_effect=unknown):
            result = self.apply('snapshot')
        self.assertEqual(result.detail['error'], 'unconfigured_foreground')
        self.assertIsNone(result.detail['saved_app'])

    def test_restore_preserves_an_external_app_change(self):
        apps = dict(APPS, settings='com.android.tv.settings/.MainActivity')
        server = make_server(apps=apps)
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        adapter = AndroidTVAdapter(*server.server_address, apps=apps, simulated=True, timeout=.5)
        self.assertEqual(adapter.apply({'action': 'snapshot'}, threading.Event()).status, 'acknowledged')
        self.assertEqual(adapter.apply({'action': 'launch', 'app': 'youtube'}, threading.Event()).status, 'acknowledged')
        server.device.set_current_app('settings')
        result = adapter.apply({'action': 'restore'}, threading.Event())
        self.assertEqual(result.detail['error'], 'foreground_changed_externally')
        self.assertEqual(server.device.snapshot()['current_app'], 'settings')

    def test_reset_forgets_snapshot_without_sending_a_command(self):
        self.apply('snapshot')
        traces = self.server.device.snapshot()['traces']
        result = self.adapter.reset()
        self.assertTrue(result.detail['snapshot_cleared'])
        self.assertIsNone(result.detail['saved_app'])
        self.assertEqual(self.server.device.snapshot()['traces'], traces)
        self.assertEqual(self.apply('restore').detail['error'], 'no_snapshot')


if __name__ == '__main__':
    unittest.main()
