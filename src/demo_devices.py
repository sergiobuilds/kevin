"""Task-owned loopback fixtures for tracks 04–07; never reads live settings."""
from __future__ import annotations

import threading
from dataclasses import asdict


FAULTS = {
    'wallpad': ('none', 'ignore', 'timeout', 'stale', 'unavailable'),
    'vacuum': ('none', 'ignore', 'timeout', 'stale', 'unavailable'),
    'tv': ('none', 'offline', 'ignore', 'timeout', 'empty'),
    'ir': ('none', 'timeout', 'no_code', 'bad_code', 'reject', 'ignore'),
}


class DemoDevices:
    def __init__(self, timeout=1.5):
        from demos.household_sim.server import make_server as household_server
        from demos.tv_sim.server import make_server as tv_server
        from demos.ir_sim.server import make_server as ir_server
        from devices.wallpad import HAWallpadAdapter
        from devices.vacuum import ValetudoVacuumAdapter
        from devices.tv import AndroidTVAdapter
        from devices.ir import BroadlinkIRAdapter

        self.servers, self.threads = [], []
        self.adapters = {}
        self._closed = False
        try:
            house, vacuum, tv, ir = (self._start(factory) for factory in
                                     (household_server, household_server, tv_server, ir_server))
            self.fixtures = {'wallpad': house.device, 'vacuum': vacuum.device,
                             'tv': tv.device, 'ir': ir.device}
            base = f'http://127.0.0.1:{house.server_address[1]}'
            self.adapters = {
                'wallpad': HAWallpadAdapter(base, entities={name: 'light.' + name for name in
                    ('living_1', 'living_2', 'hall')}, simulated=True, timeout=timeout),
                'vacuum': ValetudoVacuumAdapter(f'http://127.0.0.1:{vacuum.server_address[1]}', zones={'entry': {'points': {
                    'pA': {'x': 100, 'y': 100}, 'pB': {'x': 200, 'y': 100},
                    'pC': {'x': 200, 'y': 200}, 'pD': {'x': 100, 'y': 200}},
                    'iterations': 1}}, simulated=True, timeout=timeout),
                'tv': AndroidTVAdapter('127.0.0.1', tv.server_address[1], apps={
                    'home': 'com.android.tv.launcher/.MainActivity',
                    'youtube': 'com.google.android.youtube.tv/.MainActivity'},
                    simulated=True, timeout=timeout),
                'ir': BroadlinkIRAdapter('127.0.0.1', ir.server_address[1],
                                        simulated=True, timeout=timeout),
            }
        except Exception:
            self.close()
            raise

    def _start(self, factory):
        server = factory('127.0.0.1', 0)
        self.servers.append(server)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={'poll_interval': .05}, daemon=True)
        self.threads.append(thread)
        thread.start()
        return server

    def snapshot(self):
        # Fixture inspection is local and never creates extra device commands.
        return {name: fixture.snapshot() for name, fixture in self.fixtures.items()}

    def apply(self, device, plan, cancel):
        if device not in self.adapters or not isinstance(plan, dict):
            raise ValueError('unknown device or invalid plan')
        if device == 'ir' and plan.get('op') == 'learn' and plan.get('name') in ('stop', 'forward'):
            self.fixtures['ir'].arm(plan['name'])
        return self.adapters[device].apply(plan, cancel)

    def set_fault(self, device, fault):
        if device not in FAULTS or fault not in FAULTS[device]:
            raise ValueError('unsupported fixture fault')
        self.fixtures[device].set_fault(fault)

    def reset(self):
        for fixture in {id(f): f for f in self.fixtures.values()}.values():
            fixture.reset()
        for adapter in self.adapters.values():
            if hasattr(adapter, 'reset'):
                adapter.reset()

    def stop(self):
        # No application/global mutex is held during bounded network stops.
        results = {}
        for name, adapter in self.adapters.items():
            try:
                results[name] = asdict(adapter.stop())
            except Exception:
                results[name] = {'status': 'failed', 'physically_verified': False,
                                 'detail': {'source': 'simulation', 'error': 'stop_failed'}}
        return results

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.adapters:
            self.stop()
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)
