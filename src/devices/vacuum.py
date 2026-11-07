"""Known-zone control using Valetudo's published capability REST API.

Inspected upstream fd91c6aa7ea123a6a29e8127ce8e4b90be90fb60 (Apache-2.0).
Pause is BasicControlCapability's action, not a separate PauseCapability.
"""
from __future__ import annotations

import copy
import math
import re
import threading

from devices.base import Capability, Result
from devices.wallpad import _HTTPAdapter, _AdapterError


API = '/api/v2/robot'
DEFAULT_ZONES = {'entry': {'points': {
    'pA': {'x': 100, 'y': 100}, 'pB': {'x': 200, 'y': 100},
    'pC': {'x': 200, 'y': 200}, 'pD': {'x': 100, 'y': 200}}, 'iterations': 1}}


class ValetudoVacuumAdapter(_HTTPAdapter):
    slug = 'vacuum'
    capabilities = (Capability('known_zone', 'Start a configured zone or pause the robot; does not verify cleanliness.',
        {'type': 'object', 'additionalProperties': False, 'required': ['action'],
         'properties': {'action': {'type': 'string', 'enum': ['clean_zone', 'pause']},
                        'zone': {'type': 'string'}}}),)

    def __init__(self, base_url, zones, *, token='', simulated=False, timeout=2):
        super().__init__(base_url, token=token, simulated=simulated, timeout=timeout)
        if not isinstance(zones, dict) or not zones:
            raise ValueError('configure at least one calibrated zone')
        for name, zone in zones.items():
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) or not isinstance(zone, dict):
                raise ValueError('invalid zone name or definition')
            points = zone.get('points')
            if not isinstance(points, dict) or set(points) != {'pA', 'pB', 'pC', 'pD'}:
                raise ValueError('zone requires four points')
            for point in points.values():
                if (not isinstance(point, dict) or set(point) != {'x', 'y'} or
                    any(type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 1000000 for n in point.values())):
                    raise ValueError('invalid calibrated zone coordinate')
            a, b, c, d = (points[k] for k in ('pA', 'pB', 'pC', 'pD'))
            if not (a['x'] == d['x'] < b['x'] == c['x'] and a['y'] == b['y'] < c['y'] == d['y']):
                raise ValueError('configure an ordered rectangular zone')
            if type(zone.get('iterations', 1)) is not int or not 1 <= zone.get('iterations', 1) <= 3:
                raise ValueError('iterations must be between 1 and 3')
        self.zones = copy.deepcopy(zones)
        self._unresolved_clean = False

    def _detail(self, **values):
        return super()._detail(cleanliness_verified=False, **values)

    def _state(self, context):
        rows = self._request(context, API + '/state/attributes')
        if not isinstance(rows, list):
            raise _AdapterError('invalid_state_response')
        values = [r for r in rows if isinstance(r, dict) and r.get('__class') == 'StatusStateAttribute']
        if len(values) != 1 or values[0].get('value') not in (
            'error', 'docked', 'idle', 'returning', 'cleaning', 'paused', 'manual_control', 'moving'):
            raise _AdapterError('unknown_or_unavailable_state')
        return {'status': values[0]['value'], 'flag': values[0].get('flag', 'none')}

    def apply(self, plan, cancel):
        return self._apply(plan, cancel)

    def _apply(self, plan, cancel, *, wait_for_slot=0):
        if (not isinstance(plan, dict) or set(plan) - {'action', 'zone'} or
            plan.get('action') not in ('clean_zone', 'pause') or
            (plan.get('action') == 'clean_zone' and (not isinstance(plan.get('zone'), str) or plan['zone'] not in self.zones)) or
            (plan.get('action') == 'pause' and 'zone' in plan)):
            return Result('failed', detail=self._detail(error='invalid_plan'))
        plan = dict(plan)
        def operation(context):
            caps = self._request(context, API + '/capabilities')
            required = 'ZoneCleaningCapability' if plan['action'] == 'clean_zone' else 'BasicControlCapability'
            if not isinstance(caps, list) or required not in caps:
                raise _AdapterError('capability_unavailable')
            before = self._state(context)
            if before['status'] == 'error':
                raise _AdapterError('robot_error')
            if plan['action'] == 'clean_zone':
                if before['status'] not in ('docked', 'idle', 'paused'):
                    raise _AdapterError('robot_busy')
                zone = self.zones[plan['zone']]
                properties = self._request(context, API + '/capabilities/ZoneCleaningCapability/properties')
                iterations = zone.get('iterations', 1)
                try:
                    supported = (properties['zoneCount']['min'] <= 1 <= properties['zoneCount']['max'] and
                                 properties['iterationCount']['min'] <= iterations <= properties['iterationCount']['max'])
                except (KeyError, TypeError):
                    supported = False
                if not supported:
                    raise _AdapterError('zone_limits_unsupported')
                payload = {'action': 'clean', 'zones': [{'points': copy.deepcopy(zone['points'])}], 'iterations': iterations}
                path = API + '/capabilities/ZoneCleaningCapability'
            else:
                if before['status'] in ('paused', 'docked', 'idle'):
                    return Result('acknowledged', detail=self._detail(before=before, after=before,
                        request=plan, command_sent=False, readback_matches=True, outcome='already_inactive'))
                payload = {'action': 'pause'}
                path = API + '/capabilities/BasicControlCapability'
            self._check(context)
            try:
                self._request(context, path, method='PUT', body=payload)
            except Exception:
                if plan['action'] == 'clean_zone' and context['sent'] and not context.get('write_response_received'):
                    # A socket timeout is not a remote rollback. The robot may
                    # accept this clean after a subsequent pause/readback.
                    self._unresolved_clean = True
                raise
            after = self._state(context)
            matches = (after['status'] == 'cleaning' and after['flag'] == 'zone') if plan['action'] == 'clean_zone' else after['status'] in ('paused', 'idle', 'docked')
            return Result('acknowledged' if matches else 'failed', detail=self._detail(
                request=plan, before=before, after=after, command_sent=True, readback_matches=matches,
                outcome=('zone_cleaning_started' if plan['action'] == 'clean_zone' else 'pause_reported') if matches else 'unconfirmed',
                error=None if matches else 'readback_mismatch'))
        return self._execute(operation, cancel, wait_for_slot=wait_for_slot)

    def status(self):
        result = self._execute(lambda c: Result('acknowledged', detail=self._detail(state=self._state(c), zones=list(self.zones))), threading.Event())
        return dict(result.detail, status='connected' if result.status == 'acknowledged' else result.status)

    def stop(self):
        # Serialize pause after any in-flight command; never race a delayed
        # clean request against pause and accidentally restart after stopping.
        self._cancel_active()
        result = self._apply({'action': 'pause'}, threading.Event(), wait_for_slot=self.timeout + .1)
        if self._unresolved_clean:
            result.status = 'failed'
            result.detail.update(error='prior_clean_outcome_unknown',
                                 outcome='unconfirmed', readback_matches=False,
                                 command_may_have_been_sent=True)
        confirmed = result.status == 'acknowledged'
        if result.status == 'acknowledged':
            result.status = 'stopped'
        elif result.status == 'stopped':
            result.status = 'failed'  # A second cancellation is not pause confirmation.
        result.detail['stop_kind'] = 'pause'
        result.detail['inactive_reported'] = confirmed
        return result
