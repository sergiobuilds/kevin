// Isolated browser logic: no camera, bridge, or paid API traffic.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function app() {
  const elements = new Map(), timers = [], images = [];
  const context2d = { drawImage() {}, getImageData() { return { data: new Uint8Array(160 * 120 * 4) }; } };
  function element(id) {
    if (!elements.has(id)) elements.set(id, { textContent: '', style: {}, children: [], getContext: () => context2d,
      prepend(item) { this.children.unshift(item); }, toDataURL: () => 'data:image/jpeg;base64,frame' });
    return elements.get(id);
  }
  const ctx = vm.createContext({ URLSearchParams, location: { search: '', hostname: 'localhost', origin: 'http://localhost' },
    document: { getElementById: element, createElement: () => element(Symbol()) }, window: {},
    Image: class { constructor() { images.push(this); } set src(value) { this.assignedCors = this.crossOrigin; this.url = value; } get src() { return this.url; } },
    setTimeout(fn) { timers.push(fn); }, setInterval() {}, clearInterval() {},
    fetch: async () => { throw new Error('offline'); }, Uint8Array });
  vm.runInContext(fs.readFileSync('docs/app/app.js', 'utf8').split('// ---------- wiring ----------')[0] +
    '\nglobalThis.api = {call, health, stopAll, watch, pullRoomCam, roomCam, state}; say = text => globalThis.lastSaid = text;', ctx);
  return { ctx, api: ctx.api, element, timers, images, context2d };
}
test('HTTP failures never mark bridge healthy or stop confirmed', async () => {
  const a = app(); a.ctx.fetch = async () => ({ ok: false, status: 503, json: async () => ({ reply: 'ok' }) });
  await a.api.health(); assert.equal(a.api.state.bridge, false);
  await a.api.stopAll(); assert.equal(a.api.state.devicesOn, null);
  assert.match(a.ctx.lastSaid, /확인하지 못/); assert.match(a.element('log').children[0].textContent, /실패/);
});
test('connection refusal does not report stopped; accepted request does', async () => {
  const a = app(); await a.api.stopAll(); assert.equal(a.api.state.devicesOn, null);
  a.ctx.fetch = async () => ({ ok: true, json: async () => ({ reply: 'accepted' }) });
  await a.api.stopAll(); assert.equal(a.api.state.devicesOn, false);
  assert.match(a.element('log').children[0].textContent, /요청 전달/);
});
