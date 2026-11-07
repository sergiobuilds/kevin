"""Run only the 04–07 loopback tools over MCP; no live settings or camera."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from demo_devices import DemoDevices
from kevin_mcp import Kevin, serve


class FixtureAdapter:
    def __init__(self, lab, name):
        self.lab, self.name = lab, name
        self.capabilities = lab.adapters[name].capabilities

    def apply(self, plan, cancel):
        return self.lab.apply(self.name, plan, cancel)

    def stop(self):
        return self.lab.adapters[self.name].stop()


def main():
    lab = DemoDevices()
    try:
        kevin = Kevin(controller=SimpleNamespace(stop=lambda: {'status': 'stopped'}),
                      camera=False, devices={name: FixtureAdapter(lab, name) for name in lab.adapters},
                      devices_only=True)
        serve(kevin)
    finally:
        lab.close()


if __name__ == '__main__':
    main()
