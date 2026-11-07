import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import unittest


@unittest.skipUnless(all(importlib.util.find_spec(name) for name in ('adb_shell', 'broadlink')),
                     'optional requirements-demo.txt not installed')
class DeviceDemoMcpTests(unittest.TestCase):
    def test_real_stdio_process_exposes_and_runs_only_fixture_devices(self):
        root = Path(__file__).resolve().parent.parent
        # Even inherited live settings must not expose physical show tools.
        process = subprocess.Popen([sys.executable, str(root / 'src/device_demo_mcp.py')],
            cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=dict(os.environ, KEVIN_SHOW_LIVE='1', KEVIN_CAMERA='0'))
        output = queue.Queue()
        reader = threading.Thread(target=lambda: [output.put(line) for line in process.stdout], daemon=True)
        reader.start()
        mid = 0

        def request(method, params=None):
            nonlocal mid
            mid += 1
            process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': mid, 'method': method,
                                            'params': params or {}}) + '\n')
            process.stdin.flush()
            reply = json.loads(output.get(timeout=12))
            self.assertEqual(reply['id'], mid)
            self.assertNotIn('error', reply, reply)
            return reply['result']

        try:
            request('initialize')
            tools = request('tools/list')['tools']
            self.assertEqual({t['name'] for t in tools},
                             {'stop_all', 'device_wallpad', 'device_tv', 'device_ir', 'device_vacuum'})
            calls = [('wallpad', {'channel': 'living_1', 'on': True}),
                     ('tv', {'action': 'snapshot'}), ('tv', {'action': 'launch', 'app': 'youtube'}),
                     ('tv', {'action': 'restore'}), ('ir', {'op': 'learn', 'name': 'stop'}),
                     ('ir', {'op': 'learn', 'name': 'forward'}),
                     ('ir', {'op': 'send', 'name': 'forward', 'hold_seconds': .1}),
                     ('vacuum', {'action': 'clean_zone', 'zone': 'entry'}),
                     ('vacuum', {'action': 'pause'})]
            for device, plan in calls:
                result = request('tools/call', {'name': 'device_' + device, 'arguments': plan})
                self.assertFalse(result['isError'], result)
                content = result['structuredContent']
                self.assertFalse(content['physically_verified'])
                self.assertEqual(content['detail']['source'], 'simulation')
            request('tools/call', {'name': 'stop_all', 'arguments': {}})
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            reader.join(2)
            errors = process.stderr.read()
            process.stdout.close()
            process.stderr.close()
        self.assertEqual(process.returncode, 0, errors)
