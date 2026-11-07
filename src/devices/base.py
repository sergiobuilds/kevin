"""Contract every Kevin device adapter follows.

The language model never touches hardware. It produces a plan; the show
controller validates it and hands each adapter only the fields it owns.
Adapters talk to the device in the device's own protocol (BLE, IR, RS485,
ADB, LAN ...) without a vendor app or cloud in the loop.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Capability:
    """One thing the AI may ask this device to do, e.g. `speed` 0.5..2."""
    name: str
    description: str
    schema: dict[str, Any]


@dataclass
class Result:
    status: str                      # "acknowledged" | "sent_unverified" | "failed" | "stopped"
    physically_verified: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


class DeviceAdapter(Protocol):
    """Implement this in src/devices/<slug>.py.

    - `capabilities` lists every field the planner may set. Anything else is
      rejected before it reaches the adapter.
    - `apply` must return promptly once `cancel` is set (Esc stops everything).
    - `stop` must leave the device in a safe, quiet state and be idempotent.
    - Tests use a simulator; real hardware runs only with `--live`.
    """

    slug: str
    capabilities: tuple[Capability, ...]

    def apply(self, plan: dict[str, Any], cancel: threading.Event) -> Result: ...

    def stop(self) -> Result: ...

    def status(self) -> dict[str, Any]: ...
