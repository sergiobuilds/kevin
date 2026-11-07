"""Explicit-target BLE and owned Windows audio; construction never accesses devices."""

import asyncio
import base64
from importlib.metadata import version
import inspect
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid

from PIL import Image
import pixels


class TransportError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled('show cancelled')


def prepared_image_plan(data, device_info):
    """Use exact prepared bytes, avoiding the library's image re-encoding."""
    from pypixelcolor.commands.send_image import _build_send_plan
    with Image.open(io.BytesIO(data)) as image:
        if image.size != (device_info.width, device_info.height):
            raise TransportError('panel geometry mismatch')
        if image.format not in ('PNG', 'GIF'):
            raise TransportError('PNG or GIF required')
        is_gif = image.format == 'GIF'
        image.verify()
    return _build_send_plan(data, is_gif, plan_name='show_prepared_image', save_slot=0)


def sized_session(address):
    # Kept lazy so preview and ordinary hub startup do not require BLE packages.
    if version('pypixelcolor') != '0.5.0':
        raise TransportError('pypixelcolor==0.5.0 required')
    from pypixelcolor.lib.device_session import DeviceSession
    from pypixelcolor.lib.transport.send_plan import send_plan

    class SizedSession(DeviceSession):
        async def disconnect(self):
            if self._client is not None and not self._connected:
                try:
                    await self._client.disconnect()
                finally:
                    self._client = None
                    self._ack_mgr = None
                    self._device_info = None
                return
            await super().disconnect()

        async def execute_command(self, command, *args, **kwargs):
            if not self._connected or not self._client or not self._ack_mgr:
                raise TransportError('BLE session unavailable')
            if 'device_info' in inspect.signature(command).parameters:
                kwargs['device_info'] = self._device_info
            plan = command(*args, **kwargs)
            mtu = getattr(self._client, 'mtu_size', 23)
            payload = mtu - 3 if isinstance(mtu, int) and mtu >= 23 else 20
            plan.chunk_size = min(plan.chunk_size, 244, payload)
            return await send_plan(self._client, plan, self._ack_mgr)

    return SizedSession(address)


class LedTransport:
    def __init__(self, address, *, session_factory=None):
        self.address = address
        self.session_factory = session_factory or sized_session
        self.lock = threading.Lock()

    async def _transfer(self, data, cancel, brightness):
        from pypixelcolor.commands import COMMANDS
        session = self.session_factory(self.address)
        try:
            await session.connect()
            check_cancel(cancel)
            info = session.get_device_info()
            if (info.width, info.height) != (144, 16) or info.device_type != 135:
                raise TransportError('only the known 144x16 panel type is supported')
            for command, args in ((COMMANDS['set_brightness'], (brightness,)),
                                  (prepared_image_plan, (data,))):
                check_cancel(cancel)
                result = await session.execute_command(command, *args)
                if result.success is not True:
                    raise TransportError('panel command was not acknowledged')
            check_cancel(cancel)
        finally:
            await asyncio.wait_for(session.disconnect(), timeout=5)

    async def _cancellable_transfer(self, data, cancel, brightness):
        async def watch():
            while not cancel.is_set():
                await asyncio.sleep(.025)
        work = asyncio.create_task(self._transfer(data, cancel, brightness))
        watcher = asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait((work, watcher), timeout=90,
                                         return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                return await work
            if cancel.is_set():
                raise Cancelled('show cancelled')
            raise TransportError('panel transfer timeout')
        finally:
            watcher.cancel()
            if not work.done():
                work.cancel()
            await asyncio.gather(work, watcher, return_exceptions=True)

    def send(self, scene, cancel, brightness=25):
        if not self.address:
            raise TransportError('explicit panel configuration required')
        if type(brightness) is not int or not 0 <= brightness <= 25:
            raise TransportError('show brightness must be 0..25')
        scene = pixels.validate(scene)
        if scene['duration'] % 10:
            raise TransportError('exact 10ms frame delays required')
        kind = 'gif' if len(scene['frames']) > 1 else 'png'
        data = base64.b64decode(pixels.export_image(scene, kind))
        try:
            with self.lock:
                check_cancel(cancel)
                asyncio.run(self._cancellable_transfer(data, cancel, brightness))
        except Cancelled:
            raise
        except Exception as error:
            # Platform errors can contain the selected address. Never return them.
            raise TransportError('LED transfer failed; check connection, panel type and ACK') from error
        return {'status': 'acknowledged', 'physical_verified': False,
                'frames': len(scene['frames']), 'bytes': len(data)}

    def clear(self, cancel):
        black = [['#000000'] * 144 for _ in range(16)]
        result = self.send({'frames': [black], 'duration': 120}, cancel)
        return dict(result, status='clear_acknowledged')


class RadioPlayer:
    """One owned persistent child, with interruptible replies and no default output."""
    def __init__(self, device, audio_path):
        self.device = device
        self.audio_path = Path(audio_path).resolve()
        self.lock = threading.RLock()
        self.process = None
        self.pending = {}
        self.snapshot = {'status': 'not_started', 'audible_verified': False}

    def _read(self, process):
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                    with self.lock:
                        waiter = self.pending.get((process.pid, message.get('id')))
                    if waiter is not None:
                        waiter.put(message)
                except (ValueError, TypeError):
                    continue
        finally:
            with self.lock:
                for (pid, _), waiter in list(self.pending.items()):
                    if pid == process.pid:
                        waiter.put({'ok': False})
            process.stdout.close()

    def _start(self, cancel):
        check_cancel(cancel)
        if self.device != 'CRF-35B' or sys.platform != 'win32':
            raise TransportError('configured CRF-35B requires interactive Windows hub')
        if self.process is None or self.process.poll() is not None:
            self.process = subprocess.Popen(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                 'Bypass', '-File', str(Path(__file__).with_name('radio_player.ps1')),
                 '-Device', self.device, '-AudioPath', str(self.audio_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', bufsize=1)
            threading.Thread(target=self._read, args=(self.process,), daemon=True).start()

    def _request(self, command, *, cancel=None, timeout=1, **fields):
        with self.lock:
            if cancel is not None:
                check_cancel(cancel)
            process = self.process
            if process is None or process.poll() is not None:
                if command == 'play':
                    raise TransportError('owned audio child exited before playback')
                return {'status': 'stopped', 'audible_verified': False}
            request_id = uuid.uuid4().hex
            waiter = queue.Queue()
            key = (process.pid, request_id)
            self.pending[key] = waiter
            try:
                process.stdin.write(json.dumps(dict(id=request_id, command=command, **fields)) + '\n')
                process.stdin.flush()
            except (OSError, ValueError):
                self.pending.pop(key, None)
                raise TransportError('owned audio child unavailable') from None
        try:
            reply = waiter.get(timeout=timeout)
            if reply.get('ok') is not True:
                raise TransportError('radio command failed; check configured audio output')
            result = reply['result']
            with self.lock:
                if self.process is process:
                    self.snapshot = result
            return result
        except queue.Empty:
            raise TransportError('radio command timeout; playback unverified') from None
        finally:
            with self.lock:
                self.pending.pop(key, None)

    def play(self, volume, cancel):
        with self.lock:
            self._start(cancel)
        result = self._request('play', cancel=cancel, timeout=18, volume=volume)
        check_cancel(cancel)
        return result

    def _terminate(self, process):
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=.3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=.3)
        if process.stdin:
            process.stdin.close()

    def stop(self):
        # A child still opening a device must not delay emergency stop.
        try:
            result = self._request('stop', timeout=.2)
        except TransportError:
            with self.lock:
                process, self.process = self.process, None
            self._terminate(process)
            result = {'status': 'stopped_child_terminated', 'audible_verified': False}
        with self.lock:
            self.snapshot = result
        return result

    def status(self):
        try:
            return self._request('status', timeout=.2)
        except TransportError:
            return {'status': 'unknown', 'audible_verified': False}

    def close(self):
        try:
            self._request('quit', timeout=.2)
        except TransportError:
            pass
        finally:
            with self.lock:
                process, self.process = self.process, None
            self._terminate(process)
