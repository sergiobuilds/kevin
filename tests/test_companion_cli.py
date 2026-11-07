"""Terminal control behavior without browser or hardware."""
import time
import threading
import unittest

from companion_cli import Game


class BackendFixture:
    def __init__(self):
        self.calls = []

    def request(self, action, body=None):
        self.calls.append((action, body))
        return {'job_id': 'fixture', 'status': 'queued'}


class TerminalGameTests(unittest.TestCase):
    def test_space_opens_portrait_and_enter_uses_preview(self):
        backend = BackendFixture()
        game = Game(backend)
        game.state = {'live_configured': True}
        game.key(' ')
        self.assertEqual(game.mode, 'talk')
        for char in '캐럴 틀어줘':
            game.key(char)
        game.key('\r')
        for _ in range(50):
            if backend.calls:
                break
            time.sleep(.005)
        self.assertEqual(backend.calls, [('command', {'prompt': '캐럴 틀어줘', 'execute': False})])

    def test_stop_invalidates_late_command(self):
        game = Game(BackendFixture())
        game.mode, game.waiting, game.live = 'planning', True, True
        epoch = game.epoch
        game.stop()
        game.results.put((epoch, 'command', {'job_id': 'late'}, None))
        game.poll_at = time.monotonic() + 100
        game.update()
        self.assertEqual(game.mode, 'stopped')
        self.assertNotEqual(game.job, 'late')
        self.assertFalse(game.live)

    def test_live_requires_server_configuration_and_is_consumed(self):
        game = Game(BackendFixture())
        game.key('\x0c')
        self.assertFalse(game.live)
        game.state = {'live_configured': True}
        game.key('\x0c')
        self.assertTrue(game.live)
        game.submit('시작')
        self.assertFalse(game.live)
        self.assertTrue(game.executing_live)
        self.assertIn('LIVE', game.frame(100, 35, 0))

    def test_stop_is_sent_after_inflight_command_dispatch(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        calls = []
        class SlowBackend:
            def request(self, action, body=None):
                if action == 'command':
                    entered.set()
                    release.wait(1)
                calls.append(action)
                if action == 'stop':
                    stopped.set()
                return {'job_id': 'fixture'}
        game = Game(SlowBackend())
        game.submit('시작')
        self.assertTrue(entered.wait(1))
        game.stop()
        self.assertFalse(stopped.is_set())
        release.set()
        self.assertTrue(stopped.wait(1))
        self.assertEqual(calls, ['command', 'stop'])

    def test_output_stays_terminal_and_sanitizes_untrusted_text(self):
        game = Game(BackendFixture())
        game.message = 'hello\x1b]52;c;BAD\x07'
        frame = game.frame(100, 35, 0)
        self.assertIn('\x1b[H', frame)
        self.assertIn('▀', frame)
        self.assertNotIn('\x1b]52', frame)
