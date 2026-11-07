"""Automatic idle stops must distinguish completion from uncertainty."""
import shutil
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

import kevin_bridge


class AutoStopTests(unittest.TestCase):
    def test_idle_only_reports_power_saved_after_confirmed_stop(self):
        for status in ('error', 'stopping', 'failed', 'unknown', None, 'stopped'):
            with self.subTest(status=status):
                bridge = kevin_bridge.Bridge.__new__(kevin_bridge.Bridge)
                bridge.job_lock = threading.RLock()
                bridge.job_cancel = threading.Event()
                bridge.c = Mock()
                bridge.c.stop.return_value = {} if status is None else {'status': status}
                result = bridge.guard({'kind': 'idle', 'idle_seconds': 31, 'idle_limit': 30})
                bridge.c.stop.assert_called_once_with()
                self.assertTrue(bridge.job_cancel.is_set())
                self.assertEqual(result['stop_status'], status or 'unknown')
                self.assertEqual(result['action'], 'power_save' if status == 'stopped' else 'none')
                if status != 'stopped':
                    self.assertNotIn('껐어', result['reply'])
                    self.assertIn('확인', result['reply'])

    @unittest.skipUnless(shutil.which('node'), 'Node required for isolated UI regression')
    def test_ui_preserves_unknown_idle_stop_and_transport_failure(self):
        script = r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {style:{}, children:[], getContext:()=>({}),
    prepend(x){this.children.unshift(x)}});
  return elements.get(id);
}
const ctx = vm.createContext({URLSearchParams, location:{search:'', hostname:'localhost', origin:'http://localhost'},
  document:{getElementById:element, createElement:()=>element(Symbol())}, window:{}, Image:class{},
  setTimeout(){}, setInterval(){}, clearInterval(){}, Uint8Array});
vm.runInContext(fs.readFileSync('docs/app/app.js','utf8').split('// ---------- wiring ----------')[0] +
  '\nglobalThis.api={guard,state}; say=text=>globalThis.lastSaid=text;', ctx);
(async()=>{
  for (const status of ['error','stopping','failed','unknown',undefined,'stopped']) {
    ctx.api.state.bridge=true; ctx.api.state.devicesOn=true;
    ctx.fetch=async()=>({ok:true,json:async()=>({action:status==='stopped'||status===undefined?'power_save':'none',
      stop_status:status,reply:'status reply'})});
    await ctx.api.guard('idle',31);
    assert.equal(ctx.api.state.devicesOn,status==='stopped'?false:null);
    assert.match(element('log').children[0].textContent,status==='stopped'?/기기 끔/:/정지 확인/);
  }
  ctx.fetch=async()=>{throw Error('offline')}; ctx.api.state.devicesOn=true;
  await ctx.api.guard('idle',31); assert.equal(ctx.api.state.devicesOn,null);
  assert.match(ctx.lastSaid,/확인하지 못/);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
