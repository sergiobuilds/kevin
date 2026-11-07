# Wallpad lights and known vacuum zones

These adapters issue real HTTP requests using Python's standard library. The
local demo uses an explicitly labelled protocol fixture. No physical wallpad,
RS485 wiring, vacuum, map calibration or cleanliness result was tested.

## 04 — Existing Home Assistant wallpad bridge

`HAWallpadAdapter(base_url, entities, token='', simulated=False, timeout=2,
max_state_age=60)` supports only named, configured `light.*` entities. A command
is `{"channel":"living_1","on":true}`. It reads `/api/states`, requires known
on/off values and recent `last_reported` for every configured light, posts one
entity to `/api/services/light/turn_on` or `turn_off`, then reads states again.
It checks the requested state and preserves the other configured light states
and attributes. An already-matching state causes no service call.

`last_updated` records change time, so it is not used as an observation clock.
An unchanged light with fresh `last_reported` is accepted. A bridge without
recent reports, missing `last_reported`, unknown state, or stale state is refused.
The configured maximum age must fit the actual bridge's reporting interval.
Service completion and HA state are device-reported evidence, not confirmation
that the lamp physically lit. Independent human changes during the comparison
also cause a preservation failure; KEVIN does not overwrite them to restore a
snapshot.

This route requires an already working HA wallpad integration, such as a
separately configured RS485 bridge. It does not copy or guess vendor RS485 frames,
and does not expose locks, gas or heating controls. The adapter's token is trusted
local configuration; it is never included in results or demo traces. Only the
configured origin is contacted, with redirects disabled. HA token permissions
are not narrowed by this local entity allowlist.

Sources and reuse:

Home Assistant Core is Apache-2.0. This adapter uses its documented REST contract;
no upstream source code is copied or imported.

- [HA REST API](https://contributors.home-assistant.io/core/internals/rest-api/):
  service calls and state reads. `POST /api/states` is deliberately not used as
  evidence of device control.
- [HA State serialization](https://github.com/home-assistant/core/blob/09cd298b2663bcb6d0d0d33c5be566ac7a8bb3d9/homeassistant/core.py):
  `last_reported` versus `last_updated` semantics, inspected 2026-11-07.
- [hacollector](https://github.com/bongdang/hacollector): prior bridge candidate;
  no source code from that project is imported or copied here, and no particular
  wallpad model compatibility is claimed.

## 07 — Valetudo known-zone cleaning

`ValetudoVacuumAdapter(base_url, zones, token='', simulated=False, timeout=2)`
accepts `{"action":"clean_zone","zone":"entry"}` or `{"action":"pause"}`.
Zone names resolve to trusted configured rectangular `pA/pB/pC/pD` coordinates
and iteration counts. An LLM cannot provide new coordinates in an action.
`DEFAULT_ZONES` is a synthetic example for the demo, never a real calibrated map.

The adapter checks the capabilities list, current state and zone/iteration
limits before `PUT /api/v2/robot/capabilities/ZoneCleaningCapability`:

```json
{"action":"clean","zones":[{"points":{"pA":{"x":100,"y":100},"pB":{"x":200,"y":100},"pC":{"x":200,"y":200},"pD":{"x":100,"y":200}}}],"iterations":1}
```

It rereads `/api/v2/robot/state/attributes` and requires `StatusStateAttribute`
with `value: cleaning` and `flag: zone`. That means **zone cleaning started**, not
that the intended physical coordinates were correct, the task finished, or dirt
was removed. An already active robot is not restarted by a duplicate command.

Pause uses `PUT /api/v2/robot/capabilities/BasicControlCapability` with
`{"action":"pause"}`. There is no separate `PauseCapability` in the inspected
API. `stop()` cancels pending control, waits for any in-flight request to finish,
then sends pause and reads back an inactive state. It does not equate vendor
`stop`, pause and return-to-dock. Failure to confirm inactivity remains failure.

Primary sources: Valetudo **Apache-2.0**, inspected revision
`fd91c6aa7ea123a6a29e8127ce8e4b90be90fb60`. We implemented the published HTTP
contract; no firmware or upstream source is copied into this repository.

- [ZoneCleaningCapabilityRouter](https://github.com/Hypfer/Valetudo/blob/fd91c6aa7ea123a6a29e8127ce8e4b90be90fb60/backend/lib/webserver/capabilityRouters/ZoneCleaningCapabilityRouter.js)
- [BasicControlCapabilityRouter](https://github.com/Hypfer/Valetudo/blob/fd91c6aa7ea123a6a29e8127ce8e4b90be90fb60/backend/lib/webserver/capabilityRouters/BasicControlCapabilityRouter.js)
- [RobotRouter](https://github.com/Hypfer/Valetudo/blob/fd91c6aa7ea123a6a29e8127ce8e4b90be90fb60/backend/lib/webserver/RobotRouter.js)
- [StatusStateAttribute](https://github.com/Hypfer/Valetudo/blob/fd91c6aa7ea123a6a29e8127ce8e4b90be90fb60/backend/lib/entities/state/attributes/StatusStateAttribute.js)

Real use requires a supported, installed Valetudo system, a validated current
map and zone geometry, appropriate local API access and hardware verification.
The adapter does not install firmware, change Wi-Fi, configure authentication,
detect dirt or calibrate image coordinates.

## Local protocol fixture and verification

`demos.household_sim.server.make_server('127.0.0.1', 0)` returns a HTTP server
with `device.snapshot()`, `reset()`, `set_fault()` and bounded traces. Default
channels are `living_1`, `living_2`, `hall`; the vacuum starts docked. Faults are
`none`, `ignore`, `timeout`, `stale` (HA reports), `unavailable` and `collateral`
(HA changes an extra light). These controls are in-process, not arbitrary remote
administrative endpoints. The fixture is a protocol subset, not upstream HA or
Valetudo execution.

Both adapters separate `simulation` from `device_reported` by trusted
constructor configuration and always return `physically_verified=False`.
Vacuum results also always include `cleanliness_verified=False`. HTTP 200 alone
is insufficient for success. Requests have bounded socket/overall deadlines,
no automatic command retries and cancellation checks before subsequent effects.
Cancellation cannot recall a request already sent to a device.

Connections require a configured numeric IP origin, avoiding unbounded DNS
resolution. HTTPS still verifies the server certificate, which must match that
IP. Socket waits are capped at .35 seconds, so slow/unresponsive peers fail
closed instead of delaying cancellation. The local fixture binds only to
loopback IPv4 and is not an exposed LAN service.

No new package dependencies. Run the loopback integration tests with:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_device_wallpad.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_device_vacuum.py' -v
```
