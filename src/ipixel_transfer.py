"""Explicit opt-in iPixel candidate transport; hardware compatibility is unverified."""
import argparse
import asyncio
import base64
import io
from pathlib import Path
import sys
import tempfile
from PIL import Image
import pixels

class TransferError(Exception):
    pass

def prepare(path):
    raw = Path(path).read_bytes()
    if len(raw) > 4_000_000:
        raise TransferError('input exceeds 4 MB')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ('PNG', 'GIF') or image.size != (144, 16):
                raise TransferError('select a 144×16 PNG or GIF exported by Kevin')
            kind = image.format.lower()
        scene = pixels.import_image(base64.b64encode(raw).decode())
        return base64.b64decode(pixels.export_image(scene, kind)), kind, len(scene['frames'])
    except (pixels.PixelError, OSError) as exc:
        raise TransferError('invalid PNG/GIF: ' + str(exc)) from exc

async def transfer(address, path, client_factory):
    client = client_factory(address)
    try:
        await client.connect()
        info = client.get_device_info()
        if (info.width, info.height) != (144, 16):
            raise TransferError('device reports a different resolution; transmission stopped')
        await client.send_image(path)
    finally:
        try:
            await client.disconnect()
        finally:
            # pypixelcolor 0.5.0 sets AsyncClient._connected only after device-info
            # retrieval. If that step fails, its session may still be connected.
            session = getattr(client, '_session', None)
            if getattr(session, 'is_connected', False):
                await session.disconnect()

def send_scene(scene, mode='simulator', address=None, confirmed=False, client_factory=None):
    """Encode current frames. BLE is never touched without an explicit target and opt-in."""
    scene = pixels.validate(scene)
    kind = 'gif' if len(scene['frames']) > 1 else 'png'
    data = base64.b64decode(pixels.export_image(scene, kind))
    if mode == 'simulator':
        return {'status': 'simulated', 'format': kind, 'frames': len(scene['frames']),
                'bytes': len(data)}
    if mode != 'ipixel' or confirmed is not True or not isinstance(address, str) or not address.strip() or len(address) > 200:
        raise TransferError('실기기 모드, 기기 식별자와 전송 선택이 필요합니다')
    if client_factory is None:
        try:
            from pypixelcolor import AsyncClient
            client_factory = AsyncClient
        except ImportError as exc:
            raise TransferError('선택 패키지 pypixelcolor==0.5.0 설치가 필요합니다') from exc
    try:
        with tempfile.TemporaryDirectory(prefix='kevin-transfer-') as tmp:
            path = Path(tmp) / ('scene.' + kind)
            path.write_bytes(data)
            asyncio.run(transfer(address.strip(), path, client_factory))
    except Exception as exc:
        # Do not return device identifiers or platform-specific BLE details to the browser.
        raise TransferError('전송 실패; 기기 상태는 알 수 없습니다. 연결과 해상도를 확인하세요.') from exc
    return {'status': 'sent_unverified', 'format': kind, 'frames': len(scene['frames']), 'bytes': len(data)}

def main(argv=None):
    parser = argparse.ArgumentParser(description='Prepare a 144×16 PNG/GIF; actual iPixel compatibility is unverified.')
    parser.add_argument('file')
    parser.add_argument('--send', action='store_true', help='explicitly enable BLE transmission')
    parser.add_argument('--device', help='selected device identifier, provided only at execution time')
    args = parser.parse_args(argv)
    try:
        data, kind, count = prepare(args.file)
        if not args.send:
            print(f'Simulation: validated {kind.upper()}, 144×16, {count} frame(s); no BLE connection.')
            return 0
        if not args.device:
            raise TransferError('--send requires an explicitly selected --device')
        try:
            from pypixelcolor import AsyncClient
        except ImportError as exc:
            raise TransferError('install optional pypixelcolor==0.5.0 first') from exc
        with tempfile.TemporaryDirectory(prefix='kevin-transfer-') as tmp:
            path = Path(tmp) / ('scene.' + kind)
            path.write_bytes(data)
            asyncio.run(transfer(args.device, path, AsyncClient))
        print('Library call completed; physical display and playback must be checked separately.')
        return 0
    except Exception as exc:
        print('Transfer failed: ' + str(exc), file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
