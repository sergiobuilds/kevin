"""Configured wallpad light channels through Home Assistant's REST API.

This reuses an existing HA bridge; it does not implement an unknown RS485
protocol. Only configured light entities can be changed.
https://contributors.home-assistant.io/core/internals/rest-api/
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import ipaddress
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from devices.base import Capability, Result


class _AdapterError(Exception):
    pass


class _Cancelled(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _HTTPAdapter:
    """Bounded, non-retrying JSON transport shared with the Valetudo adapter."""

    def __init__(self, base_url, *, token='', simulated=False, timeout=2):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
            raise ValueError('base_url must be a configured HTTP(S) origin')
        try:
            ipaddress.ip_address(parsed.hostname)
            parsed.port  # Validate malformed/out-of-range ports before a request.
        except ValueError:
            raise ValueError('base_url must use a configured numeric IP and valid port') from None
        if type(simulated) is not bool or not isinstance(token, str) or '\n' in token or '\r' in token:
            raise ValueError('invalid connection configuration')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= 30:
            raise ValueError('timeout must be between .1 and 30 seconds')
        self.base_url, self.token = base_url.rstrip('/'), token
        self.simulated, self.timeout = simulated, timeout
        self._slot, self._guard = threading.Lock(), threading.Lock()
        self._active = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _detail(self, **values):
        return dict(source='simulation' if self.simulated else 'device_reported',
                    simulated=self.simulated, physically_verified=False, **values)

    @staticmethod
    def _check(context):
        if context['cancel'].is_set() or context['local'].is_set():
            raise _Cancelled()
        if time.monotonic() >= context['deadline']:
            raise _AdapterError('timeout')

    def _request(self, context, path, *, method='GET', body=None):
        self._check(context)
        headers = {'Accept': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        data = None
        if body is not None:
            data = json.dumps(body, allow_nan=False).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.base_url + path, data, headers, method=method)
        try:
            # Short socket waits let cancellation interrupt stalled peers. There
            # are no automatic command retries or redirect credential forwards.
            # Serialize the final cancellation check and transmission start
            # against stop(). The bounded header wait holds this guard for at
            # most .35 seconds; response-body waits never hold it.
            with self._guard:
                self._check(context)
                timeout = min(.35, context['deadline'] - time.monotonic())
                if method != 'GET':
                    context['sent'] = True
                response = self._opener.open(request, timeout=timeout)
                if method != 'GET':
                    context['write_response_received'] = True
            with response:
                pieces, size = [], 0
                while True:
                    self._check(context)
                    part = response.read1(16384)
                    if not part:
                        break
                    size += len(part)
                    if size > 131072:
                        raise _AdapterError('response_too_large')
                    pieces.append(part)
                raw = b''.join(pieces)
                self._check(context)
                if not raw or 'application/json' not in response.headers.get('Content-Type', ''):
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise _AdapterError('authentication_failed' if exc.code in (401, 403) else 'http_error') from None
        except (TimeoutError, urllib.error.URLError):
            self._check(context)
            raise _AdapterError('timeout_or_connection_failed') from None
        except (ValueError, UnicodeError):
            raise _AdapterError('invalid_response') from None

    def _execute(self, operation, cancel, *, wait_for_slot=0):
        if not self._slot.acquire(timeout=wait_for_slot):
            return Result('failed', detail=self._detail(error='busy'))
        context = {'cancel': cancel, 'local': threading.Event(),
                   'deadline': time.monotonic() + self.timeout, 'sent': False}
        with self._guard:
            self._active = context['local']
        try:
            self._check(context)
            return operation(context)
        except _Cancelled:
            return Result('stopped', detail=self._detail(error='cancelled', command_may_have_been_sent=context['sent']))
        except _AdapterError as exc:
            return Result('failed', detail=self._detail(error=str(exc), command_may_have_been_sent=context['sent']))
        except Exception:
            return Result('failed', detail=self._detail(error='invalid_response', command_may_have_been_sent=context['sent']))
        finally:
            with self._guard:
                self._active = None
            self._slot.release()

    def _cancel_active(self):
        # Signal intent immediately even if a bounded request is awaiting its
        # headers under the dispatch guard. Event.set() is thread-safe.
        active = self._active
        if active is not None:
            active.set()
        with self._guard:
            if self._active is not None:
                self._active.set()


class HAWallpadAdapter(_HTTPAdapter):
    slug = 'wallpad'
    capabilities = (Capability('light_channel', 'Set one configured wallpad light channel.',
        {'type': 'object', 'additionalProperties': False, 'required': ['channel', 'on'],
         'properties': {'channel': {'type': 'string'}, 'on': {'type': 'boolean'}}}),)

    def __init__(self, base_url, entities, *, token='', simulated=False, timeout=2, max_state_age=60):
        super().__init__(base_url, token=token, simulated=simulated, timeout=timeout)
        if (not isinstance(entities, dict) or not entities or len(entities) > 32 or
            any(not isinstance(k, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', k) or
                not isinstance(v, str) or not re.fullmatch(r'light\.[a-z0-9_]+', v) for k, v in entities.items()) or
                len(set(entities.values())) != len(entities)):
            raise ValueError('configure unique light.* entities and named channels')
        if type(max_state_age) not in (int, float) or not math.isfinite(max_state_age) or not 1 <= max_state_age <= 3600:
            raise ValueError('max_state_age must be between 1 and 3600 seconds')
        self.entities, self.max_state_age = dict(entities), max_state_age

    def _states(self, context):
        response = self._request(context, '/api/states')
        if not isinstance(response, list):
            raise _AdapterError('invalid_state_response')
        rows = {}
        for item in response:
            if isinstance(item, dict) and item.get('entity_id') in self.entities.values():
                if item['entity_id'] in rows:
                    raise _AdapterError('duplicate_entity_state')
                rows[item['entity_id']] = item
        result = {}
        now = datetime.now(timezone.utc)
        for channel, entity in self.entities.items():
            row = rows.get(entity)
            if row is None or row.get('state') not in ('on', 'off'):
                raise _AdapterError('unknown_or_unavailable_state')
            # last_updated means "changed", not "observed". Require HA's
            # last_reported timestamp rather than rejecting an unchanged lamp.
            try:
                reported = datetime.fromisoformat(row['last_reported'].replace('Z', '+00:00'))
                age = (now - reported).total_seconds()
            except (KeyError, ValueError, TypeError):
                raise _AdapterError('state_freshness_unknown') from None
            if age < -5 or age > self.max_state_age:
                raise _AdapterError('stale_state')
            result[channel] = {'entity_id': entity, 'on': row['state'] == 'on',
                               'attributes': copy.deepcopy(row.get('attributes') or {}),
                               'last_reported': row['last_reported']}
        return result

    @staticmethod
    def _values(states):
        return {key: {'on': value['on'], 'attributes': value['attributes']} for key, value in states.items()}

    def apply(self, plan, cancel):
        if (not isinstance(plan, dict) or set(plan) != {'channel', 'on'} or
            not isinstance(plan.get('channel'), str) or plan['channel'] not in self.entities or type(plan.get('on')) is not bool):
            return Result('failed', detail=self._detail(error='invalid_plan'))
        plan = dict(plan)
        def operation(context):
            before = self._states(context)
            target = plan['channel']
            if before[target]['on'] == plan['on']:
                return Result('acknowledged', detail=self._detail(before=before, after=before,
                    readback_matches=True, untouched_preserved=True, command_sent=False, request=plan))
            self._check(context)
            self._request(context, '/api/services/light/' + ('turn_on' if plan['on'] else 'turn_off'),
                          method='POST', body={'entity_id': self.entities[target]})
            after = self._states(context)
            matches = after[target]['on'] == plan['on']
            before_other = {k: v for k, v in self._values(before).items() if k != target}
            after_other = {k: v for k, v in self._values(after).items() if k != target}
            preserved = before_other == after_other
            return Result('acknowledged' if matches and preserved else 'failed', detail=self._detail(
                before=before, after=after, request=plan, command_sent=True,
                readback_matches=matches, untouched_preserved=preserved,
                error=None if matches and preserved else 'readback_mismatch'))
        return self._execute(operation, cancel)

    def status(self):
        result = self._execute(lambda c: Result('acknowledged', detail=self._detail(state=self._states(c))), threading.Event())
        return dict(result.detail, status='connected' if result.status == 'acknowledged' else result.status)

    def stop(self):
        self._cancel_active()
        return Result('stopped', detail=self._detail(message='Pending control cancelled; light states are preserved.'))
