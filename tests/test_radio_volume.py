import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from show_transport import RadioPlayer, Cancelled, TransportError


class VolumeTests(unittest.TestCase):
    def test_volume_does_not_create_audio_child(self):
        radio = RadioPlayer('CRF-35B', Path('jingle_bells.wav'))
        with patch('subprocess.Popen', side_effect=AssertionError('started audio')):
            result = radio.set_volume(30, threading.Event())
            self.assertEqual(result['status'], 'stopped')
            self.assertFalse(result['volume_applied'])

    def test_volume_only_requests_volume_and_preserves_paused_state(self):
        radio = RadioPlayer('CRF-35B', Path('jingle_bells.wav'))
        radio._request = Mock(return_value={'status': 'paused', 'volume': 30})
        radio._start = Mock(side_effect=AssertionError('resumed audio'))
        event = threading.Event()
        self.assertEqual(radio.set_volume(30, event)['status'], 'paused')
        radio._request.assert_called_once_with('volume', cancel=event, volume=30)

    def test_invalid_and_cancelled_volume_cannot_send(self):
        radio = RadioPlayer('CRF-35B', Path('jingle_bells.wav'))
        radio._request = Mock(side_effect=AssertionError('sent invalid volume'))
        for value in (True, -1, 101, float('nan'), '30'):
            with self.assertRaises(TransportError):
                radio.set_volume(value, threading.Event())
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            radio.set_volume(30, event)
