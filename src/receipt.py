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


class ReceiptError(ValueError):
    pass


# ---------- camera ----------

class Camera:
    """Keep one webcam open (opening takes seconds) and grab settled frames."""

    def __init__(self, index: int = 0, width: int = 1280, height: int = 720):
        self.index, self.width, self.height = index, width, height
        self.cap = None
        self.lock = threading.Lock()

    def _open(self):
        try:
            import cv2
        except ImportError as exc:
            raise ReceiptError('opencv-python-headless is required for camera receipts') from exc
        if self.cap is None or not self.cap.isOpened():
            self.cap = cv2.VideoCapture(self.index, getattr(cv2, 'CAP_DSHOW', 0))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            for _ in range(30):  # let auto-exposure settle once
                self.cap.read()
        return cv2

    def grab(self, flush: int = 4) -> bytes:
        with self.lock:
            cv2 = self._open()
            frame, ok = None, False
            for _ in range(max(1, flush)):  # drop buffered stale frames
                ok, frame = self.cap.read()
            if not ok or frame is None:
                raise ReceiptError('camera returned no frame')
            ok, jpg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
            if not ok:
                raise ReceiptError('could not encode camera frame')
            return jpg.tobytes()

    def close(self):
        with self.lock:
            if self.cap is not None:
                self.cap.release()
                self.cap = None


# ---------- observation ----------

def _gray(data: bytes, roi=None, size=(320, 180)) -> Image.Image:
    image = Image.open(io.BytesIO(data)).convert('L')
    if roi:
        x, y, w, h = roi
        image = image.crop((x, y, x + w, y + h))
    return image.resize(size).filter(ImageFilter.GaussianBlur(1))


def _changed_ratio(a: Image.Image, b: Image.Image) -> float:
    diff = ImageChops.difference(a, b).point(lambda v: 255 if v >= CHANGE_THRESHOLD else 0)
    hist = diff.histogram()
    return hist[255] / float(a.width * a.height)


def observe(before: bytes, after: list[bytes], roi=None) -> dict:
    """Compare the scene before and after an action.

    `changed`: the target area looks different from before.
    `moving`:  two after-frames differ, i.e. an animation is actually playing.
    """
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
    """True when the signature matches and every attached image hash matches."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    body = {k: v for k, v in receipt.items() if k != 'signature'}
    sig = receipt.get('signature') or {}
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(sig['public_key'])).verify(
            base64.b64decode(sig['value']), _canonical(body))
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False
    for name, data in (images or {}).items():
        if hashlib.sha256(data).hexdigest() != body['evidence'].get(name):
            return False
    return True


# ---------- receipt ----------

def make_receipt(*, device: str, action: dict, plan: dict | None, device_reply: dict | None,
                 before: bytes, after: list[bytes], started: float, roi=None,
                 out_dir: Path, signer: Signer) -> dict:
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
        'observation': observe(before, after, roi),
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
    receipt = json.loads((args.folder / 'receipt.json').read_text(encoding='utf-8'))
    images = {name: (args.folder / name).read_bytes() for name in receipt['evidence']}
    ok = check(receipt, images)
    obs = receipt['observation']
    print(('OK ' if ok else 'TAMPERED ') + receipt['id'] +
          f"  changed={obs['changed']} ({obs['changed_ratio']})  moving={obs['moving']} ({obs['motion_ratio']})")
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
