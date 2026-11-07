# Track 06 — named IR learning and a bounded train demonstration

This implementation uses **python-broadlink 0.19.0** to authenticate, encode and
encrypt real Broadlink RM mini messages, send them over loopback UDP, learn an
IR packet, and send that learned packet again. It does not contact a transmitter
or contain a captured MYKA FORT 90995 code. The two fixture pulse sequences are
synthetic, labelled `simulation`, and deliberately have no claimed consumer
device meaning.

## Reused implementation and sources

- [python-broadlink](https://github.com/mjg59/python-broadlink), package
  [`broadlink==0.19.0`](https://pypi.org/project/broadlink/0.19.0/).
- [Remote API](https://github.com/mjg59/python-broadlink/blob/730853e5faf2cf979596662faf9def2b1f8fee6d/broadlink/remote.py):
  `rmmini`, `enter_learning`, `check_data`, `send_data`, pulse codec.
- [Device transport](https://github.com/mjg59/python-broadlink/blob/730853e5faf2cf979596662faf9def2b1f8fee6d/broadlink/device.py):
  authentication, AES and UDP packet checksums.
- [Protocol reference](https://github.com/mjg59/python-broadlink/blob/730853e5faf2cf979596662faf9def2b1f8fee6d/protocol.md).
- [MIT license](https://github.com/mjg59/python-broadlink/blob/730853e5faf2cf979596662faf9def2b1f8fee6d/LICENSE),
  copyright Mike Ryan (2014), Matthew Garrett (2016). The dependency is invoked
  directly without modifying or vendoring it. The local fixture is a narrow
  independently written protocol peer, not Broadlink firmware.

## API and execution boundary

`BroadlinkIRAdapter(host, port=80, mac=<configured bytes>, simulated=False,
timeout=1.5)` uses only the RM mini 3 / `0x2737` profile. A constructor accepts a
trusted unicast IPv4 address and MAC. Actions cannot supply addresses, keys, raw
codes, device types or discovery parameters. Constructing the adapter performs
no network I/O, discovery, Wi-Fi setup or authentication. Simulation adapters
are restricted to loopback. Real device/profile compatibility is unverified.

Actions are `{'op': 'learn'|'send', 'name': 'stop'|'forward'}`. A forward send
may additionally specify `hold_seconds` from 0.05 to 1 (default 0.3). Codes stay
in process memory and output exposes only names, byte counts and SHA-256 hashes.
An invalid or truncated learned code is rejected. Forward requires a distinct
learned stop code. This narrow profile also rejects embedded repeats and pulse
sequences longer than 0.5 seconds. Forward is sent once, followed by one stop attempt on normal
completion, cancellation or an uncertain forward response. No forward retry
occurs. Each upstream UDP call is bounded to at most 0.2 seconds; learning has
the configured overall deadline, plus bounded in-flight cleanup. `stop()`
cancels active work, whose forward cleanup attempts stop. If a prior stop ACK
was missing, another explicit `stop()` retries the learned stop once. This is
not an assertion that a physical train has stopped. The hold bound limits when
the stop request is attempted; it cannot guarantee a physical motor's runtime.

`reset()` clears in-memory codes when idle; active work is rejected. A real
adapter also refuses to discard a pending unconfirmed stop. `status()` does
not issue network requests and never exposes code bytes or session keys.
Cancelling learning stops local polling; this RM mini profile has no verified
IR-learning cancel command, so the hub may remain in learning mode until its
own timeout. No RF sweep or discovery command is sent to simulate cancellation.

The loopback fixture factory is `demos.ir_sim.server.make_server(host='127.0.0.1',
port=0)`. Run its `serve_forever()` in a task-owned thread. The resulting UDP
server has `server_port` and `device`:

- `device.arm('stop'|'forward')` selects the next synthetic learning input.
- `device.set_fault(...)` supports `none`, `timeout`, `no_code`, `bad_code`,
  `reject`, `ignore`, `bad_checksum`.
- `device.snapshot()` returns the simulated train state and redacted packet
  traces; `device.reset()` clears fixture state. Reset the adapter separately.

## What is and is not verified

The tests use the installed upstream client, real loopback UDP sockets,
authentication/session encryption, request and response checksums, learned-code
validation, and a paired stop. They also cover absent stop codes, malformed
packets, unsupported actions, ignored transmissions, timeouts, cancellation,
failed-stop recovery and idle reset.

`acknowledged` means a named packet was learned. `sent_unverified` means the hub
protocol accepted transmission; it does not mean the target moved. Every
result keeps `physically_verified=False` and `physical_state='unknown'` where
applicable. Even the fixture's moving/stopped state is a simulation result.
Physical IR timing, transmitter range, actual remote-code learning, target
motion and camera observation remain untested. This RM mini path does not
claim RM4 compatibility.

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_device_ir.py -v
```

The tests skip when the optional `broadlink` package is not installed. A real
transmitter and verified target-specific stop/forward codes are required before
a physical demonstration; the synthetic codes must never be reused for it.
