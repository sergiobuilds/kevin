import contextlib
import importlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import wave

from PIL import Image, ImageSequence

import oba_media_bridge as bridge
import pixels


DEMO_ROOT = Path(__file__).resolve().parents[1] / '.runtime' / 'bridge-demo'


class MediaBridgeTests(unittest.TestCase):
    def setUp(self):
        DEMO_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=DEMO_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def gif(self, durations=(120, 120), *, loop=0, size=(144, 16)):
        frames = []
        for index in range(len(durations)):
            frame = Image.new('RGB', size, '#061329')
            frame.putpixel((index % size[0], 0), (244, 59, 59))
            frames.append(frame)
        source = self.root / 'source.gif'
        options = {} if loop is None else {'loop': loop}
        frames[0].save(source, save_all=True, append_images=frames[1:],
                       duration=list(durations), disposal=2, **options)
        return source

    def test_santa_visibly_traverses_panel(self):
        scene = pixels.validate(bridge.santa_scene())
        self.assertEqual((len(scene['frames']), scene['duration']), (32, 120))
        positions = []
        for frame in scene['frames']:
            red = [x for row in frame for x, color in enumerate(row) if color == '#f43b3b']
            if red:
                positions.append(sum(red) / len(red))
        self.assertGreater(len(positions), 20)
        self.assertGreater(positions[-1] - positions[0], 125)
        self.assertTrue(all(a < b for a, b in zip(positions, positions[1:])))
        self.assertEqual(len({json.dumps(frame) for frame in scene['frames']}), 32)

    def test_project_and_gif_roundtrip_preserve_every_frame_color_and_delay(self):
        output = self.root / 'santa'
        result = bridge.export_bundle(bridge.santa_scene(), output, santa=True)
        project = json.loads((output / 'project.json').read_text())
        self.assertEqual((project['format'], project['version']), ('kevin-scene', 1))
        scene = pixels.validate(project['scene'])
        self.assertEqual(scene, bridge.santa_scene())
        self.assertLess((output / 'project.json').stat().st_size, 2_000_000)
        self.assertEqual(bridge.import_media(output / 'santa.gif'), scene)
        self.assertFalse(result['physical_verified'])
        with Image.open(output / 'santa.gif') as gif:
            self.assertEqual((gif.size, gif.n_frames, gif.info['loop']), ((144, 16), 32, 0))
            self.assertEqual([frame.info['duration'] for frame in ImageSequence.Iterator(gif)],
                             [120] * 32)

    def test_preview_is_exact_nearest_neighbor_enlargement(self):
        output = self.root / 'preview'
        bridge.export_bundle(bridge.santa_scene(), output, santa=True)
        with Image.open(output / 'santa.gif') as gif, Image.open(output / 'preview.gif') as preview:
            self.assertEqual((preview.size, preview.n_frames), ((864, 96), gif.n_frames))
            for frame, enlarged in zip(ImageSequence.Iterator(gif), ImageSequence.Iterator(preview)):
                expected = frame.convert('RGB').resize((864, 96), Image.Resampling.NEAREST)
                self.assertEqual(enlarged.convert('RGB').tobytes(), expected.tobytes())
                self.assertEqual(enlarged.info['duration'], 120)

    def test_original_wav_is_audible_pcm_and_matches_one_visual_cycle(self):
        output = self.root / 'audio'
        bridge.export_bundle(bridge.santa_scene(), output, santa=True)
        with wave.open(str(output / 'santa.wav'), 'rb') as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate(),
                              audio.getcomptype()), (1, 2, 22050, 'NONE'))
            self.assertEqual(audio.getnframes(), round(22050 * 3.84))
            raw = audio.readframes(audio.getnframes())
            samples = struct.unpack('<%dh' % (len(raw) // 2), raw)
            self.assertGreater(max(samples), 1000)
            self.assertLess(min(samples), -1000)
            self.assertLess(max(abs(value) for value in samples), 32767)
            self.assertEqual(samples[0], 0)
            self.assertLess(abs(samples[-1]), 100)
        self.assertEqual((output / 'santa.wav').read_bytes(), bridge.original_wav(3840))

    def test_import_png_keeps_colors_and_composites_transparency_on_black(self):
        source = self.root / 'source.png'
        image = Image.new('RGBA', (144, 16), (120, 30, 220, 0))
        image.putpixel((70, 8), (18, 52, 86, 255))
        image.save(source)
        scene = bridge.import_media(source)
        self.assertEqual(scene['duration'], 200)
        self.assertEqual(scene['frames'][0][8][70], '#123456')
        self.assertEqual(scene['frames'][0][0][0], '#000000')
        bridge.export_bundle(scene, self.root / 'png')
        self.assertEqual(bridge.import_media(self.root / 'png' / 'display.gif'), scene)

    def test_import_resizes_with_nearest_neighbor(self):
        source = self.root / 'tiny.png'
        image = Image.new('RGB', (2, 1), 'red')
        image.putpixel((1, 0), (0, 0, 255))
        image.save(source)
        frame = bridge.import_media(source)['frames'][0]
        self.assertTrue(all(row == ['#ff0000'] * 72 + ['#0000ff'] * 72 for row in frame))

    def test_import_uniform_gif_keeps_all_frames(self):
        scene = bridge.import_media(self.gif((400,) * 32))
        self.assertEqual((len(scene['frames']), scene['duration']), (32, 400))
        bridge.export_bundle(scene, self.root / 'imported')
        self.assertEqual(bridge.import_media(self.root / 'imported' / 'display.gif'), scene)

    def test_rejects_variable_missing_zero_out_of_range_timing_and_finite_loops(self):
        for durations, loop in (((100, 400), 0), ((0, 0), 0), ((30, 30), 0),
                                ((5010, 5010), 0), ((120, 120), 2), ((120, 120), None)):
            with self.subTest(durations=durations, loop=loop):
                source = self.gif(durations, loop=loop)
                with self.assertRaises(pixels.PixelError):
                    bridge.import_media(source)
        source = self.gif()
        # Remove all graphic-control extensions, including their duration fields.
        data = source.read_bytes()
        while b'\x21\xf9\x04' in data:
            offset = data.index(b'\x21\xf9\x04')
            data = data[:offset] + data[offset + 8:]
        source.write_bytes(data)
        with self.assertRaisesRegex(pixels.PixelError, 'explicit'):
            bridge.import_media(source)

    def test_rejects_excess_frames_dimensions_bytes_and_unsupported_formats(self):
        with self.assertRaisesRegex(pixels.PixelError, '1–32'):
            bridge.import_media(self.gif((120,) * 33))
        source = self.root / 'large.png'
        Image.new('1', (2001, 2000)).save(source)
        with self.assertRaisesRegex(pixels.PixelError, '4MP'):
            bridge.import_media(source)
        source.write_bytes(b'x' * (4_000_000 + 1))
        with self.assertRaisesRegex(pixels.PixelError, '4MB'):
            bridge.import_media(source)
        source = self.root / 'source.bmp'
        Image.new('RGB', (144, 16)).save(source)
        with self.assertRaisesRegex(pixels.PixelError, 'PNG and GIF'):
            bridge.import_media(source)
        source = self.root / 'animated.png'
        Image.new('RGB', (144, 16), 'red').save(
            source, save_all=True, append_images=[Image.new('RGB', (144, 16), 'blue')],
            duration=120, loop=0)
        with self.assertRaisesRegex(pixels.PixelError, 'animated PNG'):
            bridge.import_media(source)

    def test_rejects_gif_color_loss_frame_merging_and_sub_tick_timing_before_writing(self):
        gradient = [[f'#{x:02x}{y:02x}00' for x in range(144)] for y in range(16)]
        bad_scenes = (
            ({'frames': [gradient], 'duration': 200}, 'colors'),
            ({'frames': [pixels.blank(), pixels.blank()], 'duration': 120}, 'merged'),
            ({'frames': [pixels.blank()], 'duration': 123}, '10 ms'),
        )
        for scene, reason in bad_scenes:
            with self.subTest(reason=reason):
                output = self.root / reason
                with self.assertRaisesRegex(pixels.PixelError, reason):
                    bridge.export_bundle(scene, output)
                self.assertFalse(output.exists())

    def test_existing_folder_is_never_overwritten(self):
        output = self.root / 'existing'
        output.mkdir()
        sentinel = output / 'project.json'
        sentinel.write_text('someone else owns this')
        with self.assertRaises(FileExistsError):
            bridge.export_bundle(bridge.santa_scene(), output, santa=True)
        self.assertEqual(sentinel.read_text(), 'someone else owns this')
        self.assertEqual(list(output.iterdir()), [sentinel])

    def test_module_import_and_preparation_commands_never_access_devices(self):
        player = Mock(side_effect=AssertionError('unexpected audio playback'))
        ble = Mock(side_effect=AssertionError('unexpected BLE access'))
        modules = {'winsound': types.SimpleNamespace(PlaySound=player),
                   'bleak': types.SimpleNamespace(BleakClient=ble, BleakScanner=ble)}
        with patch.dict(sys.modules, modules), \
                patch('socket.socket', side_effect=AssertionError('unexpected network/device socket')):
            importlib.reload(bridge)
            with patch.object(bridge, 'play_audio', side_effect=AssertionError('implicit playback')):
                with contextlib.redirect_stdout(io.StringIO()):
                    bridge.main(['santa', str(self.root / 'offline')])
                    bridge.main(['import', str(self.root / 'offline' / 'santa.gif'),
                                 str(self.root / 'offline-import')])
        player.assert_not_called()
        ble.assert_not_called()

    def test_play_audio_dispatch_requires_explicit_subcommand(self):
        with patch.object(bridge, 'play_audio') as playback, \
                contextlib.redirect_stdout(io.StringIO()):
            bridge.main(['play-audio', 'example.wav'])
        playback.assert_called_once_with(Path('example.wav'))

    def test_windows_audio_uses_filename_and_default_output_without_fallback_sound(self):
        source = self.root / 'original.wav'
        source.write_bytes(bridge.original_wav(100))
        winsound = types.SimpleNamespace(PlaySound=Mock(), SND_FILENAME=0x20000, SND_NODEFAULT=2)
        with patch.object(sys, 'platform', 'win32'), patch.dict(sys.modules, {'winsound': winsound}):
            bridge.play_audio(source)
        winsound.PlaySound.assert_called_once_with(str(source.resolve()), 0x20002)

    def test_audio_rejects_non_windows_and_invalid_pcm_without_playback(self):
        with patch.object(sys, 'platform', 'linux'):
            with self.assertRaisesRegex(RuntimeError, 'Windows'):
                bridge.play_audio(self.root / 'does-not-exist.wav')
        source = self.root / 'eight-bit.wav'
        with wave.open(str(source), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(1)
            audio.setframerate(22050)
            audio.writeframes(b'\x80' * 100)
        winsound = types.SimpleNamespace(PlaySound=Mock())
        with patch.object(sys, 'platform', 'win32'), patch.dict(sys.modules, {'winsound': winsound}):
            with self.assertRaisesRegex(ValueError, '16-bit PCM'):
                bridge.play_audio(source)
        winsound.PlaySound.assert_not_called()


if __name__ == '__main__':
    unittest.main()
