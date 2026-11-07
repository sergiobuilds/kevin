"""One-segment WLED control through the upstream ``wled==1.1.1`` client.

Source: https://github.com/frenck/python-wled (MIT).
Protocol: https://kno.wled.ge/interfaces/json-api/
Only a trusted constructor supplies the network destination. Device replies
are read back, but are never represented as physical observation.
"""
from __future__ import annotations

import asyncio
import copy
import math
import re
import threading
import time

from devices.base import Capability, Result


class _Cancelled(Exception):
    pass


class _Deadline(Exception):
    pass


class _PortSession:
    """Map the client's fixed port 80 to a configured local test-server port.

    python-wled still owns payload construction, HTTP processing and models.
    Redirects are disabled so a device cannot change the configured target.
    """

    def __init__(self, session, host, port):
        self.session, self.host, self.port = session, host.lower(), port

    def request(self, method, url, **kwargs):
        if url.host.lower() != self.host or url.scheme != 'http':
            raise ValueError('unexpected WLED destination')
        return self.session.request(method, url.with_port(self.port),
                                    allow_redirects=False, **kwargs)


def _snapshot(device):
    return {
        'on': device.state.on,
        'brightness': device.state.brightness,
        'segments': [
            {'id': sid, 'name': s.name or f'Segment {sid}', 'on': s.on,
             'brightness': s.brightness,
             'color': list(s.color.primary[:3]) if s.color else [0, 0, 0]}
            for sid, s in sorted(device.state.segments.items())
        ],
    }


class WLEDLightAdapter:
    slug = 'light'
    capabilities = (Capability(
        'segment', 'Set power, brightness or RGB color of one existing segment.',
        {'type': 'object', 'additionalProperties': False, 'required': ['segment'],
         'properties': {
             'segment': {'type': 'integer', 'minimum': 0, 'maximum': 255},
             'on': {'type': 'boolean'},
             'brightness': {'type': 'integer', 'minimum': 0, 'maximum': 255},
             'color': {'type': 'array', 'minItems': 3, 'maxItems': 3,
                       'items': {'type': 'integer', 'minimum': 0, 'maximum': 255}},
         }}),)

    def __init__(self, host, port=80, *, simulated=False, timeout=3):
        if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9.-]+', host):
            raise ValueError('host must be a configured hostname or IPv4 address')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('invalid WLED port')
        if type(simulated) is not bool:
            raise ValueError('simulated must be explicit boolean configuration')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .05 <= timeout <= 30:
            raise ValueError('timeout must be between .05 and 30 seconds')
        self.host, self.port, self.simulated, self.timeout = host, port, simulated, timeout
        self._operation = threading.Lock()
        self._guard = threading.Lock()
        self._active_cancel = None

    @property
    def source(self):
        return 'simulation' if self.simulated else 'device_reported'

    def _detail(self, **values):
        return dict(source=self.source, simulated=self.simulated,
                    physically_verified=False, client='python-wled 1.1.1', **values)

    @staticmethod
    def _validate(plan):
        if not isinstance(plan, dict) or set(plan) - {'segment', 'on', 'brightness', 'color'}:
            raise ValueError('only segment, on, brightness and color are supported')
        if type(plan.get('segment')) is not int or not 0 <= plan['segment'] <= 255:
            raise ValueError('segment must be an integer between 0 and 255')
        if len(plan) == 1:
            raise ValueError('provide at least one segment change')
        if 'on' in plan and type(plan['on']) is not bool:
            raise ValueError('on must be a boolean')
        if 'brightness' in plan and (type(plan['brightness']) is not int or not 0 <= plan['brightness'] <= 255):
            raise ValueError('brightness must be an integer between 0 and 255')
        if 'color' in plan and (not isinstance(plan['color'], (list, tuple)) or len(plan['color']) != 3 or
                               any(type(c) is not int or not 0 <= c <= 255 for c in plan['color'])):
            raise ValueError('color must contain three integer RGB channels')
        return copy.deepcopy(plan)

    async def _bounded(self, operation, cancel, local_cancel):
        task = asyncio.create_task(operation())
        deadline = time.monotonic() + self.timeout
        try:
            while not task.done():
                if cancel.is_set() or local_cancel.is_set():
                    raise _Cancelled()
                if time.monotonic() >= deadline:
                    raise _Deadline()
                await asyncio.wait({task}, timeout=.025)
            if cancel.is_set() or local_cancel.is_set():
                raise _Cancelled()
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _run(self, operation, cancel):
        if not self._operation.acquire(blocking=False):
            return Result('failed', detail=self._detail(error='busy'))
        local_cancel = threading.Event()
        with self._guard:
            self._active_cancel = local_cancel
        try:
            if cancel.is_set():
                raise _Cancelled()
            return asyncio.run(self._bounded(operation, cancel, local_cancel))
        except _Cancelled:
            return Result('stopped', detail=self._detail(
                error='cancelled', message='Control cancelled; already-sent changes may remain.'))
        except _Deadline:
            return Result('failed', detail=self._detail(error='timeout', readback_matches=False))
        except Exception:
            # Do not expose host addresses or arbitrary library exception text.
            return Result('failed', detail=self._detail(error='connection_or_response_failed', readback_matches=False))
        finally:
            with self._guard:
                if self._active_cancel is local_cancel:
                    self._active_cancel = None
            self._operation.release()

    async def _with_client(self, operation):
        import aiohttp
        from wled import WLED
        async with aiohttp.ClientSession() as session:
            client = WLED(self.host, request_timeout=self.timeout,
                          session=_PortSession(session, self.host, self.port))
            return await operation(client)

    def _check_cancel(self, cancel):
        with self._guard:
            stopped = self._active_cancel is not None and self._active_cancel.is_set()
        if cancel.is_set() or stopped:
            raise _Cancelled()

    def apply(self, plan, cancel):
        try:
            plan = self._validate(plan)
        except ValueError as exc:
            return Result('failed', detail=self._detail(error='invalid_plan', message=str(exc)))

        async def operation(client):
            before = _snapshot(await client.update())
            self._check_cancel(cancel)
            if not any(s['id'] == plan['segment'] for s in before['segments']):
                return Result('failed', detail=self._detail(error='unknown_segment', before=before))
            # The upstream API builds the segment-only payload. It never writes
            # the master power or another segment on our behalf.
            kwargs = {k: plan[k] for k in ('on', 'brightness') if k in plan}
            if 'color' in plan:
                kwargs['color_primary'] = tuple(plan['color'])
            await client.segment(plan['segment'], **kwargs)
            self._check_cancel(cancel)
            after = _snapshot(await client.update())
            target = next((s for s in after['segments'] if s['id'] == plan['segment']), None)
            matches = target is not None and all(
                target[key] == (list(value) if key == 'color' else value)
                for key, value in plan.items() if key != 'segment')
            untouched = (
                [s for s in before['segments'] if s['id'] != plan['segment']] ==
                [s for s in after['segments'] if s['id'] != plan['segment']]
                and before['on'] == after['on'] and before['brightness'] == after['brightness'])
            return Result('acknowledged' if matches and untouched else 'failed',
                          detail=self._detail(request=plan, before=before, after=after,
                                              readback_matches=matches, untouched_preserved=untouched,
                                              error=None if matches and untouched else 'readback_mismatch'))

        return self._run(lambda: self._with_client(operation), cancel)

    def status(self):
        async def operation(client):
            state = _snapshot(await client.update())
            return Result('acknowledged', detail=self._detail(state=state))
        result = self._run(lambda: self._with_client(operation), threading.Event())
        return dict(result.detail, status='connected' if result.status == 'acknowledged' else result.status)

    def stop(self):
        """Cancel pending control; do not turn off unrelated or manual lighting."""
        with self._guard:
            if self._active_cancel is not None:
                self._active_cancel.set()
        return Result('stopped', detail=self._detail(
            message='Pending control cancelled. Lights are left unchanged; power-off is a separate command.'))
