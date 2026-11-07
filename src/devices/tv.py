"""Foreground-app snapshot/restore over the real ``adb-shell==0.4.4`` client.

Upstream: https://github.com/JeffLIrion/adb_shell (Apache-2.0).
Only configured aliases/components are launchable; callers cannot submit shell
commands, packages or network addresses. Readback verifies the foreground app,
not playback, visible pixels, channel, content or playback position.
"""
from __future__ import annotations

import ipaddress
import math
import re
import shlex
import threading
import time

from devices.base import Capability, Result


FOREGROUND_COMMAND = 'dumpsys window windows'
_COMPONENT = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+/[A-Za-z_.$][A-Za-z0-9_.$]*')
_FOCUS = re.compile(r'mCurrentFocus=Window\{[^\n]*?\s([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)\}')


class _Stopped(Exception):
    pass


class _TimedOut(Exception):
    pass


class _Rejected(Exception):
    pass


class AndroidTVAdapter:
    slug = 'tv'
    capabilities = (Capability(
        'foreground_app', 'Snapshot, launch a configured app, or restore the saved foreground app.',
        {'type': 'object', 'additionalProperties': False, 'required': ['action'],
         'properties': {'action': {'type': 'string', 'enum': ['snapshot', 'launch', 'restore']},
                        'app': {'type': 'string', 'minLength': 1, 'maxLength': 48}}}),)

    def __init__(self, host, port=5555, *, apps, rsa_keys=None, simulated=False, timeout=2):
        # Numeric addresses avoid unbounded system DNS resolution during a stop.
        if not isinstance(host, str):
            raise ValueError('TV host must be a configured numeric IP address')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError('TV host must be a configured numeric IP address') from None
        if address.is_multicast or address.is_unspecified or str(address) == '255.255.255.255':
            raise ValueError('configure a unicast TV address')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('invalid TV port')
        if type(simulated) is not bool or (simulated and not address.is_loopback):
            raise ValueError('simulation is restricted to loopback')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= 10:
            raise ValueError('timeout must be between .1 and 10 seconds')
        if not isinstance(apps, dict) or not apps or len(apps) > 30:
            raise ValueError('configure between 1 and 30 app aliases')
        for alias, component in apps.items():
            if (not isinstance(alias, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,47}', alias)
                    or not isinstance(component, str) or not _COMPONENT.fullmatch(component)):
                raise ValueError('invalid configured alias or activity component')
        packages = [component.split('/')[0] for component in apps.values()]
        if len(set(packages)) != len(packages):
            raise ValueError('one alias per package is required')
        self.host, self.port, self.timeout = str(address), port, timeout
        self.apps, self.rsa_keys, self.simulated = dict(apps), list(rsa_keys or []), simulated
        self._packages = dict(zip(packages, apps))
        self._operation, self._guard = threading.Lock(), threading.Lock()
        self._active_cancel = None
        self._saved_app = None
        self._last_launched = None

    @property
    def source(self):
        return 'simulation' if self.simulated else 'device_reported'

    def _detail(self, **values):
        with self._guard:
            saved = self._saved_app
        return dict(source=self.source, simulated=self.simulated, physically_verified=False,
                    client='adb-shell 0.4.4', transport='adb_tcp',
                    verification_scope='foreground_app_only', playback_position_restored=False,
                    saved_app=saved, **values)

    def _validate(self, plan):
        if not isinstance(plan, dict) or set(plan) - {'action', 'app'}:
            raise ValueError('only action and configured app alias are supported')
        action = plan.get('action')
        if not isinstance(action, str) or action not in ('snapshot', 'launch', 'restore'):
            raise ValueError('action must be snapshot, launch or restore')
        if action == 'launch':
            if not isinstance(plan.get('app'), str) or plan['app'] not in self.apps:
                raise ValueError('app must be a configured alias')
        elif 'app' in plan:
            raise ValueError('app is only allowed for launch')
        return dict(plan)

    def _client(self, cancel, local_cancel, deadline):
        from adb_shell.adb_device import AdbDevice
        from adb_shell.exceptions import TcpTimeoutException
        from adb_shell.transport.tcp_transport import TcpTransport

        def remaining():
            if cancel.is_set() or local_cancel.is_set():
                raise _Stopped()
            left = deadline - time.monotonic()
            if left <= 0:
                raise _TimedOut()
            return left

        class CancellableTcp(TcpTransport):
            # Real upstream socket I/O and ADB framing; only polling/deadline
            # checks are added so a silent device cannot prevent cancellation.
            def connect(self, transport_timeout_s):
                super().connect(min(.2, remaining()))
                remaining()

            def bulk_read(self, numbytes, transport_timeout_s):
                while True:
                    try:
                        data = super().bulk_read(numbytes, min(.05, remaining()))
                        if not data:
                            raise ConnectionError('ADB connection closed')
                        return data
                    except TcpTimeoutException:
                        remaining()

            def bulk_write(self, data, transport_timeout_s):
                while True:
                    try:
                        return super().bulk_write(data, min(.05, remaining()))
                    except TcpTimeoutException:
                        remaining()

        return AdbDevice(CancellableTcp(self.host, self.port),
                         default_transport_timeout_s=self.timeout, banner=b'kevin'), remaining

    def _foreground(self, client, remaining):
        text = client.shell(FOREGROUND_COMMAND, read_timeout_s=remaining(), timeout_s=remaining())
        if not isinstance(text, str) or len(text) > 256_000:
            raise _Rejected('invalid_foreground_response')
        match = _FOCUS.search(text)
        if match is None:
            raise _Rejected('foreground_unavailable')
        package = match.group(1).split('/')[0]
        if package not in self._packages:
            raise _Rejected('unconfigured_foreground')
        return self._packages[package]

    def _execute(self, plan, cancel):
        if not self._operation.acquire(blocking=False):
            return Result('failed', detail=self._detail(error='busy'))
        local_cancel, client = threading.Event(), None
        with self._guard:
            self._active_cancel = local_cancel
        try:
            if cancel.is_set():
                raise _Stopped()
            if plan['action'] == 'restore':
                with self._guard:
                    if self._saved_app is None:
                        raise _Rejected('no_snapshot')
            if plan['action'] == 'snapshot':
                with self._guard:
                    self._saved_app = self._last_launched = None
            deadline = time.monotonic() + self.timeout
            client, remaining = self._client(cancel, local_cancel, deadline)
            client.connect(rsa_keys=self.rsa_keys, auth_timeout_s=remaining(), read_timeout_s=remaining())
            before = self._foreground(client, remaining)
            remaining()
            if plan['action'] == 'status':
                return Result('acknowledged', detail=self._detail(current_app=before, readback_matches=True))
            if plan['action'] == 'snapshot':
                with self._guard:
                    self._saved_app = before
                return Result('acknowledged', detail=self._detail(current_app=before, readback_matches=True))
            with self._guard:
                target = plan['app'] if plan['action'] == 'launch' else self._saved_app
                last_launched = self._last_launched
            if plan['action'] == 'restore' and before not in (target, last_launched):
                raise _Rejected('foreground_changed_externally')
            if before != target:
                remaining()
                reply = client.shell('am start -n ' + shlex.quote(self.apps[target]),
                                     read_timeout_s=remaining(), timeout_s=remaining())
                if not isinstance(reply, str) or re.search(r'(?im)^\s*(?:error|exception)', reply):
                    raise _Rejected('launch_rejected')
            after = self._foreground(client, remaining)
            remaining()
            matches = after == target
            if matches:
                with self._guard:
                    self._last_launched = target
            return Result('acknowledged' if matches else 'failed', detail=self._detail(
                action=plan['action'], before_app=before, requested_app=target, current_app=after,
                readback_matches=matches, error=None if matches else 'readback_mismatch'))
        except _Stopped:
            return Result('stopped', detail=self._detail(error='cancelled',
                message='Pending control stopped; an app already launched may remain open.'))
        except _TimedOut:
            return Result('failed', detail=self._detail(error='timeout', readback_matches=False))
        except _Rejected as exc:
            return Result('failed', detail=self._detail(error=str(exc), readback_matches=False))
        except Exception:
            return Result('failed', detail=self._detail(error='connection_or_response_failed', readback_matches=False))
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            with self._guard:
                if self._active_cancel is local_cancel:
                    self._active_cancel = None
            self._operation.release()

    def apply(self, plan, cancel):
        try:
            plan = self._validate(plan)
        except ValueError as exc:
            return Result('failed', detail=self._detail(error='invalid_plan', message=str(exc)))
        return self._execute(plan, cancel)

    def status(self):
        result = self._execute({'action': 'status'}, threading.Event())
        return dict(result.detail, status='connected' if result.status == 'acknowledged' else result.status,
                    apps=list(self.apps))

    def stop(self):
        with self._guard:
            if self._active_cancel is not None:
                self._active_cancel.set()
        return Result('stopped', detail=self._detail(
            message='Pending control cancelled; the TV and current app are left unchanged.'))

    def reset(self):
        """Forget saved app state only; never send a TV command."""
        if not self._operation.acquire(blocking=False):
            return Result('failed', detail=self._detail(error='busy'))
        try:
            with self._guard:
                self._saved_app = self._last_launched = None
            return Result('acknowledged', detail=self._detail(snapshot_cleared=True))
        finally:
            self._operation.release()
