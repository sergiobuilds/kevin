import io
import json
import tempfile
import time
import unittest
from pathlib import Path

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


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.signer = receipt.Signer(self.tmp / 'key.pem')
        self.before = jpg((10, 10, 10))
        self.after = [jpg((10, 10, 10), (100, 150, 300, 210)), jpg((10, 10, 10), (300, 150, 540, 210))]
        self.rec = receipt.make_receipt(
            device='led-144x16', action={'tool': 'led_scene', 'arguments': {'preset': 'santa'}},
            plan={'preset': 'santa'}, device_reply={'status': 'acknowledged'},
            before=self.before, after=self.after, started=time.time(),
            out_dir=self.tmp / 'receipts', signer=self.signer)
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


if __name__ == '__main__':
    unittest.main()
