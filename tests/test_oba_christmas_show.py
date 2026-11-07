import contextlib
import importlib
import io
import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import wave

from PIL import Image, ImageSequence

import oba_christmas_show as show
import oba_media_bridge as bridge


class ChristmasShowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scene = show.christmas_scene()
        cls.temp = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temp.name) / 'show'
        cls.result = show.generate(cls.output)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_full_width_richness_and_bounded_palette(self):
        frames = self.scene['frames']
        self.assertEqual((len(frames), self.scene['duration']), (32, 120))
        palette = {color for frame in frames for row in frame for color in row}
        self.assertGreater(len(palette), 30)
        self.assertLessEqual(len(palette), 256)
        for frame in frames:
            self.assertTrue(all(len({frame[y][x] for y in range(16)}) >= 5
                                for x in range(144)))
            regions = {tuple(color for row in frame for color in row[x:x + 24])
                       for x in range(0, 144, 24)}
            self.assertEqual(len(regions), 6)
            # Blue remains the dominant sky even with dense warm fairy lights.
            sky = [color for row in frame[:6] for color in row]
            blue = sum(int(color[5:7], 16) > int(color[1:3], 16) * 2
                       for color in sky)
            self.assertGreater(blue, len(sky) * .6)
            self.assertGreater(sum(color in (show.WHITE, show.GOLD, show.SNOW)
                                   for row in frame for color in row), 200)

    def test_motion_crosses_whole_panel_without_fullfield_strobe(self):
        frames = self.scene['frames']
        self.assertEqual(len({json.dumps(frame) for frame in frames}), 32)
        # Every frame has the snowy engine roof, lit cab and green carriage;
        # sample their wrapped physical coordinates through a full-width trip.
        for index, frame in enumerate(frames):
            position = index * 144 // 32
            self.assertEqual(frame[7][(position + 23) % 144], show.SNOW)
            self.assertEqual(frame[9][(position + 26) % 144], show.WHITE)
            self.assertEqual(frame[12][position % 144], show.GREEN)
            self.assertEqual(frame[11][(position + 43) % 144], show.WHITE)
        # The leading locomotive crosses the right edge and reappears at left.
        self.assertGreater((22 * 144 // 32) + 46, 143)
        self.assertEqual(frames[22][11][(22 * 144 // 32 + 43) % 144], show.WHITE)
        for a, b in zip(frames, frames[1:] + frames[:1]):
            changed = sum(c != d for row_a, row_b in zip(a, b)
                          for c, d in zip(row_a, row_b))
            self.assertGreater(changed, 25)
            self.assertLess(changed, 144 * 16 * .35)

    def test_large_decorated_tree_and_seven_ribboned_gifts(self):
        frame = self.scene['frames'][0]
        self.assertEqual(frame[1][78], show.WHITE)
        self.assertEqual(frame[0][78], show.GOLD)
        self.assertEqual(frame[14][78], '#a05c37')
        tree = [color for row in frame[3:13] for color in row[70:87]]
        self.assertGreater(tree.count('#087b45') + tree.count(show.GREEN), 50)
        self.assertGreaterEqual(tree.count(show.RED), 7)
        self.assertGreaterEqual(tree.count(show.GOLD), 8)
        for x, top, width, ribbon in ((52, 9, 6, '#51e5e4'), (60, 7, 6, show.GOLD),
                                      (88, 8, 7, show.GOLD), (98, 10, 6, show.WHITE),
                                      (111, 7, 8, show.GOLD), (122, 9, 6, show.WHITE),
                                      (133, 6, 8, show.GOLD)):
            self.assertEqual(frame[top + 1][x + width // 2], ribbon)
            self.assertEqual(frame[top][x], show.WHITE)

    def test_bundle_exact_gif_project_and_enlarged_preview(self):
        project = json.loads((self.output / 'project.json').read_text())
        self.assertEqual((project['format'], project['version']), ('kevin-scene', 1))
        self.assertEqual(project['scene'], self.scene)
        adaptation = project['reference_adaptation']
        self.assertEqual(adaptation, self.result['reference_adaptation'])
        self.assertEqual(adaptation['source_url'], show.REFERENCE_URL)
        self.assertEqual(adaptation['screenshot'], show.REFERENCE_SCREENSHOT)
        self.assertIn('screenshot only', adaptation['basis'])
        self.assertIn('no exact source-video match', adaptation['limitations'])
        self.assertEqual(bridge.import_media(self.output / 'display.gif'), self.scene)
        with Image.open(self.output / 'display.gif') as gif, \
                Image.open(self.output / 'preview.gif') as preview:
            self.assertEqual((gif.size, gif.n_frames, gif.info['loop']), ((144, 16), 32, 0))
            self.assertEqual((preview.size, preview.n_frames, preview.info['loop']),
                             ((864, 96), 32, 0))
            for frame, enlarged in zip(ImageSequence.Iterator(gif),
                                       ImageSequence.Iterator(preview)):
                self.assertEqual(frame.info['duration'], 120)
                self.assertEqual(enlarged.info['duration'], 120)
                self.assertEqual(enlarged.convert('RGB').tobytes(), frame.convert('RGB').resize(
                    (864, 96), Image.Resampling.NEAREST).tobytes())
        self.assertFalse(self.result['physical_verified'])
        self.assertIn('procedural', self.result['interpretation'])
        self.assertIn('no arbitrary AI generation', self.result['interpretation'])

    def test_finite_modest_pcm_and_recognizable_opening_notes(self):
        with wave.open(str(self.output / 'jingle_bells.wav'), 'rb') as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate(),
                              audio.getcomptype()), (1, 2, 22050, 'NONE'))
            seconds = audio.getnframes() / audio.getframerate()
            self.assertGreaterEqual(seconds, 20)
            self.assertLessEqual(seconds, 30)
            raw = audio.readframes(audio.getnframes())
        samples = struct.unpack('<%dh' % (len(raw) // 2), raw)
        self.assertGreater(max(samples), 2000)
        self.assertLess(min(samples), -2000)
        self.assertLess(max(map(abs, samples)), 12000)
        self.assertEqual(samples[0], 0)
        self.assertLess(abs(samples[-1]), 30)
        # Compare spectral energy at the expected melody and alternative notes.
        beat = 0
        for note, beats in show.CHORUS[:11]:
            start = round((beat * show.BEAT_SECONDS + .025) * show.SAMPLE_RATE)
            window = samples[start:start + 2048]
            def energy(frequency):
                real = sum(value * math.cos(math.tau * frequency * i / show.SAMPLE_RATE)
                           for i, value in enumerate(window))
                imag = sum(value * math.sin(math.tau * frequency * i / show.SAMPLE_RATE)
                           for i, value in enumerate(window))
                return real * real + imag * imag
            frequencies = {'C': 523.251, 'D': 587.33, 'E': 659.255, 'F': 698.456, 'G': 783.991}
            strongest = max(frequencies, key=lambda name: energy(frequencies[name]))
            self.assertEqual(strongest, note)
            beat += beats

    def test_existing_output_is_refused_without_touching_it(self):
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        with self.assertRaises(FileExistsError):
            show.generate(self.output)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            show.main(['generate', str(self.output)])
        self.assertEqual(error.exception.code, 1)

    def test_import_and_cli_never_access_devices_or_network(self):
        forbidden = Mock(side_effect=AssertionError('device access forbidden'))
        modules = {'winsound': types.SimpleNamespace(PlaySound=forbidden),
                   'bleak': types.SimpleNamespace(BleakClient=forbidden, BleakScanner=forbidden)}
        with patch.dict(sys.modules, modules), \
                patch('socket.socket', forbidden), patch.object(bridge, 'play_audio', forbidden), \
                patch('subprocess.run', forbidden), contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(show)
            show.main(['generate', str(Path(self.temp.name) / 'offline')])
        forbidden.assert_not_called()


if __name__ == '__main__':
    unittest.main()
