import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

import receipt


def jpg(color, box=None, box_color=(255, 255, 255)):
    image = Image.new('RGB', (640, 360), color)
    if box:
        ImageDraw.Draw(image).rectangle(box, fill=box_color)
    out = io.BytesIO()
    image.save(out, 'JPEG', quality=92)
    return out.getvalue()


class ObserveTests(unittest.TestCase):
    def test_same_scene_is_not_a_change(self):
        dark = jpg((10, 10, 10))
        obs = receipt.observe(dark, [jpg((10, 10, 10)), jpg((10, 10, 10))])
        self.assertFalse(obs['changed'])
        self.assertFalse(obs['moving'])

    def test_lit_sign_counts_as_changed_but_frozen(self):
        # The failure seen on the real panel: it lit up, then never moved.
        lit = jpg((10, 10, 10), (100, 150, 540, 210))
        obs = receipt.observe(jpg((10, 10, 10)), [lit, lit])
        self.assertTrue(obs['changed'])
        self.assertFalse(obs['moving'])

    def test_animation_counts_as_moving(self):
        a = jpg((10, 10, 10), (100, 150, 300, 210))
        b = jpg((10, 10, 10), (300, 150, 540, 210))
        obs = receipt.observe(jpg((10, 10, 10)), [a, b])
        self.assertTrue(obs['changed'])
        self.assertTrue(obs['moving'])

    def test_roi_ignores_changes_outside_the_device(self):
        person = jpg((10, 10, 10), (0, 0, 80, 80))
        obs = receipt.observe(jpg((10, 10, 10)), [person], roi=(100, 140, 450, 80))
        self.assertFalse(obs['changed'])

    def test_rejects_invalid_or_outside_roi_instead_of_padding_black_pixels(self):
        frame = jpg((10, 10, 10))
        for roi in ([], (1, 2, 3), (-1, 0, 10, 10), (0, 0, 0, 10),
                    (639, 0, 2, 10), (0, 359, 10, 2), (0, 0, 1.5, 10),
                    (False, 0, 10, 10)):
            with self.subTest(roi=roi), self.assertRaises(receipt.ReceiptError):
                receipt.observe(frame, [frame], roi=roi)
        self.assertFalse(receipt.observe(frame, [frame], roi=(0, 0, 640, 360))['changed'])

    def test_roi_must_fit_after_frames_too(self):
        small = io.BytesIO()
        Image.new('RGB', (100, 100)).save(small, 'JPEG')
        with self.assertRaises(receipt.ReceiptError):
            receipt.observe(jpg((10, 10, 10)), [small.getvalue()], roi=(100, 140, 450, 80))

    def test_invalid_image_is_an_observation_error(self):
        with self.assertRaisesRegex(receipt.ReceiptError, 'invalid image'):
            receipt.observe(jpg((10, 10, 10)), [b'not a camera frame'])

    def test_simulated_change_keeps_explicit_source_and_limited_claim(self):
        obs = receipt.observe(jpg((0, 0, 0)), [jpg((255, 255, 255))], source='simulation')
        self.assertTrue(obs['changed'])
        self.assertEqual(obs['source'], 'simulation')
        self.assertEqual(obs['scope'], 'visual_change_only')
        with self.assertRaises(receipt.ReceiptError):
            receipt.observe(jpg((0, 0, 0)), [jpg((255, 255, 255))], source='verified')


class CameraTests(unittest.TestCase):
    def fake_cv(self, *, opened=True, readable=True, encoded=True):
        cap = Mock()
        cap.isOpened.return_value = opened
        cap.read.return_value = (readable, object() if readable else None)
        cv = SimpleNamespace(
            VideoCapture=Mock(return_value=cap), CAP_DSHOW=700,
            CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4, IMWRITE_JPEG_QUALITY=1,
            imencode=Mock(return_value=(encoded, SimpleNamespace(tobytes=lambda: b'jpeg'))))
        return cv, cap

    def test_backend_is_native_except_windows_and_camera_is_reused(self):
        for platform in ('darwin', 'linux', 'win32'):
            cv, cap = self.fake_cv()
            camera = receipt.Camera(2)
            with self.subTest(platform=platform), patch.dict('sys.modules', cv2=cv), \
                    patch.object(receipt.sys, 'platform', platform):
                self.assertEqual(camera.grab(), b'jpeg')
                self.assertEqual(camera.grab(), b'jpeg')
                if platform == 'win32':
                    cv.VideoCapture.assert_called_once_with(2, cv.CAP_DSHOW)
                else:
                    cv.VideoCapture.assert_called_once_with(2)
                self.assertEqual(camera.source, 'camera')
                camera.close()
                camera.close()
                cap.release.assert_called_once()

    def test_open_read_and_encode_failures_release_camera(self):
        for options, message in (({'opened': False}, 'open camera'),
                                 ({'readable': False}, 'no frame'),
                                 ({'encoded': False}, 'encode')):
            cv, cap = self.fake_cv(**options)
            camera = receipt.Camera()
            with self.subTest(options=options), patch.dict('sys.modules', cv2=cv):
                with self.assertRaisesRegex(receipt.ReceiptError, message):
                    camera.grab()
                self.assertIsNone(camera.cap)
                cap.release.assert_called_once()

    def test_failed_camera_can_be_reopened_on_next_attempt(self):
        cv, first = self.fake_cv(readable=False)
        _, second = self.fake_cv()
        cv.VideoCapture.side_effect = [first, second]
        camera = receipt.Camera()
        with patch.dict('sys.modules', cv2=cv):
            with self.assertRaises(receipt.ReceiptError):
                camera.grab()
            self.assertEqual(camera.grab(), b'jpeg')
            self.assertIs(camera.cap, second)
            camera.close()

    def test_backend_exception_is_redacted_and_released(self):
        cv, cap = self.fake_cv()
        cap.read.side_effect = RuntimeError('private driver detail')
        with patch.dict('sys.modules', cv2=cv):
            with self.assertRaisesRegex(receipt.ReceiptError, '^could not open camera$'):
                receipt.Camera().grab()
        cap.release.assert_called_once()


class SignatureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.signer = receipt.Signer(self.tmp / 'key.pem')
        self.before = jpg((10, 10, 10))
        self.after = [jpg((10, 10, 10), (100, 150, 300, 210)), jpg((10, 10, 10), (300, 150, 540, 210))]
        self.rec = receipt.make_receipt(
            device='led-144x16', action={'tool': 'led_scene', 'arguments': {'preset': 'santa'}},
            plan={'preset': 'santa'}, device_reply={'status': 'acknowledged'},
            before=self.before, after=self.after, started=time.time(),
            out_dir=self.tmp / 'receipts', signer=self.signer, source='simulation')
        self.folder = Path(self.rec['path'])

    def images(self):
        return {name: (self.folder / name).read_bytes() for name in self.rec['evidence']}

    def stored(self):
        return json.loads((self.folder / 'receipt.json').read_text(encoding='utf-8'))

    def test_signed_receipt_checks_out(self):
        self.assertTrue(receipt.check(self.stored(), self.images()))
        self.assertEqual(receipt.main([str(self.folder)]), 0)

    def test_edited_observation_is_detected(self):
        forged = self.stored()
        forged['observation']['moving'] = False
        self.assertFalse(receipt.check(forged, self.images()))

    def test_swapped_photo_is_detected(self):
        (self.folder / 'after1.jpg').write_bytes(self.before)
        self.assertFalse(receipt.check(self.stored(), self.images()))
        self.assertEqual(receipt.main([str(self.folder)]), 1)

    def test_key_is_reused(self):
        again = self.signer.sign({'x': 1})['public_key']
        self.assertEqual(again, self.rec['signature']['public_key'])

    def test_missing_or_extra_images_do_not_verify_as_complete_bundle(self):
        images = self.images()
        images.pop('after2.jpg')
        self.assertFalse(receipt.check(self.stored(), images))
        self.assertFalse(receipt.check(self.stored(), {}))
        images = self.images()
        images['extra.jpg'] = self.before
        self.assertFalse(receipt.check(self.stored(), images))
        # Omitting the second argument intentionally verifies the manifest only.
        self.assertTrue(receipt.check(self.stored()))

    def test_cli_reports_missing_image_as_incomplete_bundle(self):
        (self.folder / 'after2.jpg').unlink()
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(receipt.main([str(self.folder)]), 1)
        self.assertIn('incomplete', output.getvalue())

    def test_source_is_signed_and_cannot_be_relabelled_as_real_camera(self):
        stored = self.stored()
        self.assertEqual(stored['observation']['source'], 'simulation')
        stored['observation']['source'] = 'camera'
        self.assertFalse(receipt.check(stored, self.images()))

    def test_signed_unsafe_evidence_filename_is_rejected(self):
        stored = self.stored()
        stored.pop('signature')
        stored['evidence']['../outside.jpg'] = stored['evidence']['before.jpg']
        stored['signature'] = self.signer.sign(stored)
        self.assertFalse(receipt.check(stored))

    def test_invalid_capture_creates_no_receipt_folder(self):
        destination = self.tmp / 'invalid'
        with self.assertRaises(receipt.ReceiptError):
            receipt.make_receipt(device='lamp', action={}, plan={}, device_reply={},
                                 before=self.before, after=[b'bad'], started=time.time(),
                                 out_dir=destination, signer=self.signer)
        self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
