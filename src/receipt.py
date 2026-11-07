"""Physical receipts: what KEVIN did, and what the camera saw before and after.

A device reply such as "ACK" says the bytes arrived; it does not say the room
changed. On the 144x16 panel we saw exactly that: a 32-frame GIF was
acknowledged and the panel froze on frame one. A receipt therefore records the
plan, the device reply and camera frames from before and after, measures the
change, and signs the bundle with an Ed25519 key so any agent or person can
check it later.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

RECEIPT_VERSION = 1
CHANGE_THRESHOLD = 24     # per-pixel luminance step that counts as a change
MIN_CHANGED_RATIO = 0.02  # share of pixels that must change to call it "changed"
MIN_MOTION_RATIO = 0.004  # share that must differ between two after-frames
OBSERVATION_SOURCES = ('camera', 'simulation', 'recording', 'unspecified')


class ReceiptError(ValueError):
    pass


# ---------- camera ----------

class Camera:
    """Keep one webcam open (opening takes seconds) and grab settled frames."""

    source = 'camera'

    def __init__(self, index: int = 0, width: int = 1280, height: int = 720):
        self.index, self.width, self.height = index, width, height
        self.cap = None
        self.lock = threading.Lock()

    def _open(self):
        try:
            import cv2
        except ImportError as exc:
            raise ReceiptError('opencv-python-headless is required for camera receipts') from exc
        try:
            if self.cap is None or not self.cap.isOpened():
                self._release()
                # DirectShow is Windows-only. Let OpenCV choose the native
                # backend on macOS/Linux instead of forcing a Windows backend.
                if sys.platform == 'win32':
                    self.cap = cv2.VideoCapture(self.index, getattr(cv2, 'CAP_DSHOW', 0))
                else:
                    self.cap = cv2.VideoCapture(self.index)
                if not self.cap.isOpened():
                    raise ReceiptError('could not open camera')
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                for _ in range(30):  # let auto-exposure settle once
                    self.cap.read()
        except Exception:
            self._release()
            raise ReceiptError('could not open camera') from None
        return cv2

    def grab(self, flush: int = 4) -> bytes:
        if type(flush) is not int or flush < 1:
            raise ReceiptError('camera flush must be a positive integer')
        with self.lock:
            try:
                cv2 = self._open()
                frame, ok = None, False
                for _ in range(flush):  # drop buffered stale frames
                    ok, frame = self.cap.read()
                if not ok or frame is None:
                    raise ReceiptError('camera returned no frame')
                ok, jpg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
                if not ok or jpg is None:
                    raise ReceiptError('could not encode camera frame')
                return jpg.tobytes()
            except ReceiptError:
                self._release()
                raise
            except Exception:
                self._release()
                raise ReceiptError('camera capture failed') from None

    def _release(self):
        cap, self.cap = self.cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def close(self):
        with self.lock:
            self._release()


# ---------- observation ----------

def _gray(data: bytes, roi=None, size=(320, 180)) -> Image.Image:
    try:
        with Image.open(io.BytesIO(data)) as original:
            image = original.convert('L')
    except (OSError, ValueError, TypeError):
        raise ReceiptError('observation contains an invalid image') from None
    if roi is not None:
        if (not isinstance(roi, (tuple, list)) or len(roi) != 4 or
                any(type(v) is not int for v in roi)):
            raise ReceiptError('ROI must contain four integer coordinates: x, y, width, height')
        x, y, w, h = roi
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > image.width or y + h > image.height:
            raise ReceiptError('ROI must be fully inside every observation frame')
        image = image.crop((x, y, x + w, y + h))
    return image.resize(size).filter(ImageFilter.GaussianBlur(1))


def _changed_ratio(a: Image.Image, b: Image.Image) -> float:
    diff = ImageChops.difference(a, b).point(lambda v: 255 if v >= CHANGE_THRESHOLD else 0)
    hist = diff.histogram()
    return hist[255] / float(a.width * a.height)


def observe(before: bytes, after: list[bytes], roi=None, *, source='unspecified') -> dict:
    """Compare the scene before and after an action.

    `changed`: the target area looks different from before.
    `moving`: two after-frames differ. Camera motion, exposure changes and
    unrelated objects can also cause this; it does not verify the requested
    action's meaning or audible output. `source` declares how frames were made.
    """
    if source not in OBSERVATION_SOURCES:
        raise ReceiptError('unknown observation source')
    if not after:
        raise ReceiptError('at least one after-frame is required')
    base = _gray(before, roi)
    frames = [_gray(data, roi) for data in after]
    changed = max(_changed_ratio(base, frame) for frame in frames)
    motion = max((_changed_ratio(a, b) for a, b in zip(frames, frames[1:])), default=0.0)
    return {
        'changed_ratio': round(changed, 4),
        'motion_ratio': round(motion, 4),
        'changed': changed >= MIN_CHANGED_RATIO,
        'moving': len(frames) > 1 and motion >= MIN_MOTION_RATIO,
        'roi': list(roi) if roi else None,
        'source': source,
        'scope': 'visual_change_only',
    }


# ---------- signing ----------

def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


@dataclass
class Signer:
    key_path: Path

    def _key(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        if self.key_path.exists():
            return serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
        key = Ed25519PrivateKey.generate()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        return key

    def sign(self, payload: dict) -> dict:
        from cryptography.hazmat.primitives import serialization
        key = self._key()
        public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {
            'alg': 'Ed25519',
            'public_key': base64.b64encode(public).decode(),
            'value': base64.b64encode(key.sign(_canonical(payload))).decode(),
        }


def check(receipt: dict, images: dict[str, bytes] | None = None) -> bool:
    """Verify the signature; when images are given, require the complete bundle.

    Omitting images checks only the signed manifest, not the image files.
    A signature verifies integrity, not the truth of the observation or signer
    identity against an external trusted key.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not isinstance(receipt, dict):
        return False
    body = {k: v for k, v in receipt.items() if k != 'signature'}
    sig = receipt.get('signature')
    try:
        if not isinstance(sig, dict) or sig.get('alg') != 'Ed25519':
            return False
        evidence = body['evidence']
        if (not isinstance(evidence, dict) or
                not {'before.jpg', 'after1.jpg'}.issubset(evidence) or
                any(not isinstance(name, str) or
                    not re.fullmatch(r'(?:before|after[1-9][0-9]*)\.jpg', name) or
                    not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                    for name, digest in evidence.items())):
            return False
        Ed25519PublicKey.from_public_bytes(base64.b64decode(sig['public_key'], validate=True)).verify(
            base64.b64decode(sig['value'], validate=True), _canonical(body))
        if images is not None:
            if not isinstance(images, dict) or set(images) != set(evidence):
                return False
            for name, data in images.items():
                if hashlib.sha256(data).hexdigest() != evidence[name]:
                    return False
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False
    return True


# ---------- receipt ----------

def make_receipt(*, device: str, action: dict, plan: dict | None, device_reply: dict | None,
                 before: bytes, after: list[bytes], started: float, roi=None,
                 out_dir: Path, signer: Signer, source='unspecified') -> dict:
    observation = observe(before, after, roi, source=source)
    rid = time.strftime('%Y%m%d-%H%M%S', time.localtime(started)) + '-' + uuid.uuid4().hex[:6]
    folder = out_dir / rid
    folder.mkdir(parents=True, exist_ok=True)
    evidence = {}
    for name, data in [('before.jpg', before)] + [(f'after{i + 1}.jpg', d) for i, d in enumerate(after)]:
        (folder / name).write_bytes(data)
        evidence[name] = hashlib.sha256(data).hexdigest()
    body = {
        'kevin_receipt': RECEIPT_VERSION,
        'id': rid,
        'device': device,
        'action': action,
        'plan': plan,
        'device_reply': device_reply,
        'observation': observation,
        'evidence': evidence,
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(started)),
        'finished_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }
    receipt = dict(body, signature=signer.sign(body))
    (folder / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    return dict(receipt, path=str(folder))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Check a KEVIN physical receipt folder.')
    parser.add_argument('folder', type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = json.loads((args.folder / 'receipt.json').read_text(encoding='utf-8'))
        # Validate the signed filenames before opening any evidence paths.
        if not check(receipt):
            print('INVALID receipt signature or evidence manifest')
            return 1
        images = {name: (args.folder / name).read_bytes() for name in receipt['evidence']}
    except (OSError, ValueError, KeyError, TypeError):
        print('INVALID or incomplete receipt bundle')
        return 1
    ok = check(receipt, images)
    obs = receipt['observation']
    print(('OK ' if ok else 'TAMPERED ') + receipt['id'] +
          f"  changed={obs['changed']} ({obs['changed_ratio']})  moving={obs['moving']} ({obs['motion_ratio']})")
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
