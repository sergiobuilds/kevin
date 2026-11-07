"""Named IR learning/sending via python-broadlink 0.19.0 (MIT).

The configured profile is RM mini 3 (0x2737); no discovery or Wi-Fi setup is
performed. Hub ACKs never prove that a train moved or stopped. Synthetic codes
belong only to the isolated demonstration fixture, not any real train model.
"""
from __future__ import annotations

import hashlib
import copy
import ipaddress
import math
import threading
import time

from devices.base import Capability, Result


class _Cancelled(Exception):
    pass


class _Deadline(Exception):
    pass


def validate_code(data):
    """Trim AES zero padding, then validate a learned IR packet before reuse."""
    from broadlink.remote import data_to_pulses
    if not isinstance(data, (bytes, bytearray)) or not 5 <= len(data) <= 1040:
        raise ValueError('invalid learned IR packet size')
    if data[0] != 0x26 or data[1] != 0:
        raise ValueError('only single-transmission infrared packets are supported')
    end = 4 + int.from_bytes(data[2:4], 'little')
    if not 4 < end <= min(len(data), 1024) or len(data) - end > 15 or any(data[end:]):
        raise ValueError('invalid learned IR packet length')
    code = bytes(data[:end])
    pulses = data_to_pulses(code)
    if not pulses or any(p <= 0 for p in pulses) or sum(pulses) > 500_000:
        raise ValueError('invalid learned IR pulses')
    return code


class BroadlinkIRAdapter:
    slug = 'ir'
    capabilities = (Capability(
        'named_ir', 'Learn named IR buttons; forward requires a learned stop and always sends it afterwards.',
        {'type': 'object', 'additionalProperties': False, 'required': ['op', 'name'],
         'properties': {'op': {'type': 'string', 'enum': ['learn', 'send']},
                        'name': {'type': 'string', 'enum': ['stop', 'forward']},
                        'hold_seconds': {'type': 'number', 'minimum': .05, 'maximum': 1}}}),)

    def __init__(self, host, port=80, mac=b'\x02\x00\x00\x00\x00\x06', *, simulated=False, timeout=1.5):
        # Literal unicast IPv4 avoids a DNS/discovery side effect or arbitrary
        # network destination being supplied as an LLM action argument.
        address = ipaddress.IPv4Address(host)
        if address.is_multicast or address.is_unspecified or str(address) == '255.255.255.255':
            raise ValueError('configure a unicast IPv4 address')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('invalid UDP port')
        if not isinstance(mac, bytes) or len(mac) != 6:
            raise ValueError('configure a six-byte device MAC')
        if type(simulated) is not bool or (simulated and not address.is_loopback):
            raise ValueError('simulation is restricted to loopback')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= 10:
            raise ValueError('timeout must be between .1 and 10 seconds')
        self.host, self.port, self.mac = str(address), port, mac
        self.simulated, self.timeout = simulated, timeout
        self._client = None
        self._authenticated = False
        self._guard = threading.RLock()
        self._operation = threading.Lock()
        self._active_cancel = None
        self._codes = {}
        self._last = None
        self._motion_possible = False

    @property
    def source(self):
        return 'simulation' if self.simulated else 'hub_reported'

    def _detail(self, **values):
        return dict(source=self.source, simulated=self.simulated, physically_verified=False,
                    client='python-broadlink 0.19.0', profile='rmmini-0x2737', **values)

    @staticmethod
    def _validate(plan):
        if not isinstance(plan, dict) or set(plan) - {'op', 'name', 'hold_seconds'}:
            raise ValueError('only op, name and hold_seconds are supported')
        if plan.get('op') not in ('learn', 'send') or plan.get('name') not in ('stop', 'forward'):
            raise ValueError('unsupported named IR operation')
        hold = plan.get('hold_seconds', .3)
        if (type(hold) not in (int, float) or not math.isfinite(hold) or not .05 <= hold <= 1 or
                ('hold_seconds' in plan and (plan['op'], plan['name']) != ('send', 'forward'))):
            raise ValueError('hold_seconds applies only to forward and must be .05..1')
        return dict(plan, hold_seconds=hold)

    def _check(self, cancel, local_cancel, deadline):
        if cancel.is_set() or local_cancel.is_set():
            raise _Cancelled()
        if time.monotonic() >= deadline:
            raise _Deadline()

    def _connect(self, cancel, local_cancel, deadline):
        from broadlink.remote import rmmini
        self._check(cancel, local_cancel, deadline)
        if self._client is None:
            self._client = rmmini((self.host, self.port), self.mac, 0x2737,
                                  timeout=min(.2, self.timeout))
        if not self._authenticated:
            self._client.auth()
            self._authenticated = True
        self._check(cancel, local_cancel, deadline)

    def _finish(self, result, cancel, local_cancel):
        with self._guard:
            if cancel.is_set() or local_cancel.is_set():
                result.detail['action_status'] = result.status
                result.status = 'stopped'
                result.detail['cancelled'] = True
            self._last = {'status': result.status, 'detail': dict(result.detail)}
            self._active_cancel = None
        return result

    def apply(self, plan, cancel):
        try:
            plan = self._validate(plan)
        except (ValueError, TypeError) as exc:
            return Result('failed', detail=self._detail(error='invalid_plan', message=str(exc)))
        with self._guard:
            if not self._operation.acquire(blocking=False):
                return Result('failed', detail=self._detail(error='busy'))
            local_cancel = self._active_cancel = threading.Event()
        deadline = time.monotonic() + self.timeout
        result = None
        try:
            self._check(cancel, local_cancel, deadline)
            name = plan['name']
            if plan['op'] == 'send':
                with self._guard:
                    code = self._codes.get(name)
                    stop_code = self._codes.get('stop')
                if code is None:
                    result = Result('failed', detail=self._detail(error='command_not_learned', name=name))
                elif name == 'forward' and (stop_code is None or stop_code == code):
                    result = Result('failed', detail=self._detail(error='distinct_stop_code_required', name=name))
                else:
                    self._connect(cancel, local_cancel, deadline)
                    result = self._send(name, code, stop_code, plan['hold_seconds'], cancel, local_cancel, deadline)
            else:
                self._connect(cancel, local_cancel, deadline)
                result = self._learn(name, cancel, local_cancel, deadline)
        except _Cancelled:
            result = Result('stopped', detail=self._detail(error='cancelled'))
        except _Deadline:
            result = Result('failed', detail=self._detail(error='timeout'))
        except ImportError:
            result = Result('failed', detail=self._detail(error='optional_broadlink_dependency_missing'))
        except Exception as exc:
            from broadlink import exceptions as errors
            if isinstance(exc, errors.NetworkTimeoutError):
                reason = 'timeout'
            elif isinstance(exc, errors.DataValidationError):
                reason = 'invalid_response'
            elif isinstance(exc, errors.CommandNotSupportedError):
                reason = 'unsupported_by_hub'
            elif isinstance(exc, ValueError):
                reason = 'invalid_learned_code'
            else:
                reason = 'connection_or_response_failed'
            result = Result('failed', detail=self._detail(error=reason))
        finally:
            try:
                if result is None:
                    result = Result('failed', detail=self._detail(error='incomplete'))
                result = self._finish(result, cancel, local_cancel)
            finally:
                self._operation.release()
        return result

    def _learn(self, name, cancel, local_cancel, deadline):
        from broadlink import exceptions as errors
        self._client.enter_learning()
        while True:
            self._check(cancel, local_cancel, deadline)
            try:
                code = validate_code(self._client.check_data())
            except (errors.AuthenticationError, errors.ReadError):
                # On this profile no captured packet yet is reported as -1/-10.
                if local_cancel.wait(.025):
                    raise _Cancelled()
                continue
            self._check(cancel, local_cancel, deadline)
            with self._guard:
                self._check(cancel, local_cancel, deadline)
                other = 'forward' if name == 'stop' else 'stop'
                if self._codes.get(other) == code:
                    return Result('failed', detail=self._detail(error='distinct_stop_code_required'))
                self._codes[name] = code
            return Result('acknowledged', detail=self._detail(
                operation='learn', name=name, code_sha256=hashlib.sha256(code).hexdigest(),
                code_bytes=len(code), physical_state='unknown'))

    def _send(self, name, code, stop_code, hold, cancel, local_cancel, deadline):
        detail = self._detail(operation='send', name=name, physical_state='unknown',
                              code_sha256=hashlib.sha256(code).hexdigest(), code_bytes=len(code),
                              forward_acknowledged=False, stop_attempted=False, stop_acknowledged=False)
        if name == 'stop':
            self._check(cancel, local_cancel, deadline)
            self._client.send_data(code)
            with self._guard:
                self._motion_possible = False
            detail.update(stop_attempted=True, stop_acknowledged=True)
            return Result('sent_unverified', detail=detail)
        self._check(cancel, local_cancel, deadline)
        # Once transmission starts the target could be moving, even if the ACK
        # is lost. Always attempt the paired stop without retrying forward.
        with self._guard:
            self._motion_possible = True
        error = None
        try:
            self._client.send_data(code)
            detail['forward_acknowledged'] = True
            end = min(time.monotonic() + hold, deadline)
            while time.monotonic() < end:
                self._check(cancel, local_cancel, deadline)
                local_cancel.wait(min(.025, max(0, end - time.monotonic())))
            self._check(cancel, local_cancel, deadline)
        except Exception as exc:
            error = exc
        finally:
            detail['stop_attempted'] = True
            try:
                # One bounded stop attempt is permitted after cancellation.
                self._client.send_data(stop_code)
                detail['stop_acknowledged'] = True
                with self._guard:
                    self._motion_possible = False
            except Exception:
                detail['error'] = 'stop_unconfirmed'
        if isinstance(error, _Cancelled):
            return Result('stopped', detail=detail)
        if error is not None or not detail['stop_acknowledged']:
            detail.setdefault('error', 'send_or_wait_failed')
            return Result('failed', detail=detail)
        return Result('sent_unverified', detail=detail)

    def status(self):
        with self._guard:
            return self._detail(status='ready', busy=self._operation.locked(),
                learned_names=sorted(self._codes),
                codes={name: {'sha256': hashlib.sha256(code).hexdigest(), 'bytes': len(code)}
                       for name, code in self._codes.items()},
                stop_transmission_unconfirmed=self._motion_possible, last_result=copy.deepcopy(self._last))

    def reset(self):
        with self._guard:
            if self._operation.locked():
                return Result('failed', detail=self._detail(error='busy'))
            if self._motion_possible and not self.simulated:
                return Result('failed', detail=self._detail(error='stop_transmission_unconfirmed'))
            self._codes.clear()
            self._last = None
            self._client = None
            self._authenticated = False
            self._motion_possible = False
        return Result('acknowledged', detail=self._detail(operation='reset'))

    def stop(self):
        with self._guard:
            active = self._active_cancel
            if active is not None:
                active.set()
                return Result('stopped', detail=self._detail(
                    stop_pending=self._motion_possible,
                    message='Pending work cancelled; an in-flight forward operation attempts its paired stop.'))
            retry_stop = self._motion_possible and 'stop' in self._codes
        if retry_stop:
            return self.apply({'op': 'send', 'name': 'stop'}, threading.Event())
        return Result('stopped', detail=self._detail(stop_pending=False, physical_state='unknown'))
