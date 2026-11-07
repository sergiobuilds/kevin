// KEVIN web: DOS-style face, browser webcam and voice, devices through the local bridge.
const params = new URLSearchParams(location.search);
const BRIDGE = params.get('bridge') || (location.hostname.endsWith('github.io') ? 'http://127.0.0.1:8787' : location.origin);
const $ = (id) => document.getElementById(id);
const scene = $('scene'), sx = scene.getContext('2d');
const eye = $('eye'), ex = eye.getContext('2d', { willReadFrequently: true });
const state = { mode: 'talk', view: 'room', talking: false, motion: 0, prev: null, streak: 0,
                lastMotion: Date.now(), guardBusy: false, devicesOn: true, bridge: false };
const IMG = {};
for (const [n, f] of [['room', 'room'], ['walker', 'kevin_walker'], ['portrait', 'kevin_portrait']]) { IMG[n] = new Image(); IMG[n].src = 'assets/' + f + '.png'; }

// ---------- dialogue ----------
let typing = null;
function say(text, { speak = true } = {}) {
  if (!text) return;
  clearInterval(typing);
  const el = $('line'); el.textContent = ''; let i = 0;
  typing = setInterval(() => { el.textContent = text.slice(0, ++i); if (i >= text.length) clearInterval(typing); }, 35);
  if (speak) speakOut(text.replace(/\([^)]*\)/g, '').trim());
}
let voiceAudio = null;
async function speakOut(text) {
  if (!text) return;
  try {
    if (voiceAudio) { voiceAudio.pause(); voiceAudio = null; }
    const r = await fetch(BRIDGE + '/tts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
    if (!r.ok) throw new Error('tts');
    const url = URL.createObjectURL(await r.blob());
    voiceAudio = new Audio(url);
    voiceAudio.onplay = () => state.talking = true;
    voiceAudio.onended = () => { state.talking = false; URL.revokeObjectURL(url); };
    await voiceAudio.play();
  } catch {
    if (!('speechSynthesis' in window)) return;
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text); u.lang = 'ko-KR'; u.rate = 1.05; u.pitch = 1.3;
    const ko = speechSynthesis.getVoices().find(v => v.lang.startsWith('ko')); if (ko) u.voice = ko;
    u.onstart = () => state.talking = true; u.onend = () => state.talking = false;
    speechSynthesis.speak(u);
  }
}
function log(text) {
  const li = document.createElement('li');
  li.textContent = new Date().toTimeString().slice(0, 8) + ' ' + text;
  $('log').prepend(li);
  while ($('log').children.length > 40) $('log').lastChild.remove();
}
function chip(id, cls, label) { const c = $(id); c.className = 'chip ' + (cls || ''); if (label) c.textContent = label; }

// ---------- bridge ----------
async function call(path, body) {
  const r = await fetch(BRIDGE + path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
  if (!r.ok) throw new Error(`Bridge HTTP ${r.status}`);
  return r.json();
}
async function health() {
  try {
    const h = await call('/health');
    state.bridge = true;
    chip('chip-bridge', 'ok', 'BRIDGE 연결됨');
    chip('chip-ai', h.openai ? 'ok' : 'warn', h.openai ? 'AI·OpenAI' : 'AI·LOCAL');
    chip('chip-led', h.live && h.led ? 'ok' : 'warn');
    chip('chip-radio', h.live && h.radio ? 'ok' : 'warn');
  } catch { state.bridge = false; chip('chip-bridge', 'bad', 'BRIDGE 꺼짐'); }
}

async function ask(text) {
  text = text.trim(); if (!text) return;
  $('text').value = ''; state.view = 'talk'; state.lastMotion = Date.now();
  log('나: ' + text);
  say('응, 들었어.', { speak: false });
  if (!state.bridge) { say('MSI 연결기가 꺼져 있어. cmd에서 kevin 을 실행해줘.'); return; }
  try {
    const r = await call('/say', { text });
    say(r.reply || '응!'); log('KEVIN: ' + (r.reply || '') + (r.source ? ' [' + r.source + ']' : ''));
    if (r.plan && r.plan.mode !== 'unsupported') state.devicesOn = r.plan.mode !== 'stop';
  } catch { say('어, 연결이 끊겼어. 다시 말해줄래?'); }
}
async function stopAll() {
  $('alert').hidden = true;
  try {
    const r = await call('/stop', {});
    if (r.error || r.stopped === false || r.status === 'error') throw new Error('Stop failed');
    state.devicesOn = r.status === 'stopping' ? null : false; say(r.reply || '정지 요청을 전달했어.'); log('정지 요청 전달됨');
  } catch {
    state.devicesOn = null;
    say('정지 요청을 확인하지 못했어. 기기 상태를 직접 확인해줘.');
    log('정지 실패 · 기기 상태 확인 필요');
  }
}

// ---------- voice ----------
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null;
function listen() {
  if (!SR) { say('이 브라우저는 말하기를 못 알아들어. 크롬으로 열어줘.'); return; }
  if (rec) { rec.stop(); return; }
  rec = new SR(); rec.lang = 'ko-KR'; rec.interimResults = true;
  $('mic').classList.add('rec'); state.view = 'talk'; say('듣고 있어…', { speak: false });
  rec.onresult = (e) => { const t = Array.from(e.results).map(r => r[0].transcript).join(''); $('text').value = t; if (e.results[e.results.length - 1].isFinal) ask(t); };
  rec.onend = () => { $('mic').classList.remove('rec'); rec = null; };
  rec.onerror = () => { $('mic').classList.remove('rec'); rec = null; say('잘 못 들었어. 한 번만 더 말해줄래?'); };
  rec.start();
}

// ---------- modes ----------
function setMode(m) {
  state.mode = m; state.streak = 0; state.lastMotion = Date.now();
  document.querySelectorAll('.modes button').forEach(b => b.classList.toggle('on', b.dataset.mode === m));
  const notes = { talk: '말을 걸면 케빈이 대답해요.', away: '사람이 보이면 「나 홀로 집에」처럼 겁을 줘요.', home: '30초 동안 아무도 없으면 기기를 꺼요.' };
  $('mode-note').textContent = notes[m];
  say({ talk: '응, 말 걸어줘.', away: '다녀와. 집은 내가 지킬게.', home: '아무도 없으면 내가 알아서 끌게.' }[m]);
  state.view = m === 'talk' ? 'talk' : 'room';
  log('모드: ' + m);
}

// ---------- webcam: KEVIN's eye ----------
const roomCam = new Image();
roomCam.crossOrigin = 'anonymous';
let camSource = 'room';
async function startCam() {
  roomCam.onload = () => { chip('chip-cam', 'ok', 'CAM·방'); };
  roomCam.onerror = async () => {
    if (camSource !== 'room') return;
    camSource = 'browser';
    try {
      const s = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 }, audio: false });
      $('cam').srcObject = s; chip('chip-cam', 'ok', 'CAM·이 기기'); $('eye-note').textContent = '이 기기 카메라로 보고 있어.';
    } catch { chip('chip-cam', 'bad'); $('eye-note').textContent = '방 카메라가 꺼져 있어요.'; }
  };
  pullRoomCam();
}
function pullRoomCam() {
  if (camSource !== 'room') return;
  const next = new Image();
  next.crossOrigin = 'anonymous';
  next.onload = () => { roomCam.src = next.src; setTimeout(pullRoomCam, 250); };
  next.onerror = () => { roomCam.onerror(); };
  next.src = BRIDGE + '/camera.jpg?t=' + Date.now();
}
function camReady() { return camSource === 'room' ? roomCam.complete && roomCam.naturalWidth > 0 : $('cam').readyState >= 2; }
function camEl() { return camSource === 'room' ? roomCam : $('cam'); }
function frameB64() {
  const c = document.createElement('canvas'); c.width = 320; c.height = 240;
  c.getContext('2d').drawImage(camEl(), 0, 0, 320, 240);
  return c.toDataURL('image/jpeg', .7).split(',')[1];
}
async function watch() {
  try {
    if (camReady()) {
      ex.drawImage(camEl(), 0, 0, 160, 120);
      const d = ex.getImageData(0, 0, 160, 120).data;
      const g = new Uint8Array(160 * 120);
      for (let i = 0; i < g.length; i++) g[i] = (d[4 * i] + d[4 * i + 1] + d[4 * i + 2]) / 3;
      let changed = 0;
      if (state.prev) for (let i = 0; i < g.length; i++) if (Math.abs(g[i] - state.prev[i]) > 28) changed++;
      state.prev = g; state.motion = changed / g.length;
      $('motion-bar').style.width = Math.min(100, state.motion * 600) + '%';
      // pixelate the eye view for the DOS look
      ex.imageSmoothingEnabled = false; ex.drawImage(eye, 0, 0, 160, 120, 0, 0, 80, 60); ex.drawImage(eye, 0, 0, 80, 60, 0, 0, 160, 120);
      const moving = state.motion > .06;
      if (moving) state.lastMotion = Date.now();
      state.streak = moving ? state.streak + 1 : 0;
      if (state.mode === 'away' && state.streak >= 2 && !state.guardBusy) guard('motion');
      const idle = (Date.now() - state.lastMotion) / 1000;
      if (state.mode === 'home') $('eye-note').textContent = '아무도 안 보인 지 ' + Math.floor(idle) + '초';
      if (state.mode === 'home' && idle > 30 && state.devicesOn && !state.guardBusy) guard('idle', idle);
    }
  } catch {
    state.prev = null; state.streak = 0;
    chip('chip-cam', 'bad', 'CAM·읽기 실패');
    $('eye-note').textContent = '카메라 영상을 읽지 못했어요. 다시 확인하고 있어요.';
    // Reset a tainted canvas so the next valid frame can be read.
    eye.width = eye.width;
  } finally {
    setTimeout(watch, 500);
  }
}
async function guard(kind, idle = 0) {
  state.guardBusy = true;
  try {
    const body = kind === 'idle' ? { kind, mode: 'home', idle_seconds: Math.floor(idle), idle_limit: 30 }
                                 : { kind, mode: 'away', streak: state.streak, image: frameB64() };
    const r = state.bridge ? await call('/guard', body) : { action: 'none' };
    if (r.seen) $('eye-note').textContent = '본 것: ' + r.seen;
    if (r.action === 'intruder_alert') {
      $('alert').hidden = false; state.view = 'talk'; say(r.reply); log('침입 감지 → 경보' + (r.seen ? ' (' + r.seen + ')' : ''));
      setTimeout(() => { $('alert').hidden = true; }, 15000);
    } else if (r.action === 'power_save') {
      state.devicesOn = false; say(r.reply); log('절약: 기기 끔');
    }
  } catch { /* bridge offline */ }
  setTimeout(() => state.guardBusy = false, 3000);
}

// ---------- scene: 320x200 VGA ----------
// Pokemon-style NPC: step one tile, stop, look around, sometimes turn in place.
const TILE = 10, AREA = { x0: 9, x1: 25, y0: 10, y1: 14 };   // walkable floor in tiles (320x200)
const npc = { tx: 16, ty: 12, x: 160, y: 120, face: 1, from: null, t0: 0, wait: 1500, last: 0 };
function npcThink(now) {
  if (npc.from || now - npc.last < npc.wait) return;
  npc.last = now;
  npc.wait = 700 + Math.random() * 2200;
  if (Math.random() < .3) { npc.face = Math.random() < .5 ? -1 : 1; return; }   // just look around
  const dirs = [[1, 0], [-1, 0], [0, 1], [0, -1]].filter(([dx, dy]) => {
    const nx = npc.tx + dx, ny = npc.ty + dy;
    return nx >= AREA.x0 && nx <= AREA.x1 && ny >= AREA.y0 && ny <= AREA.y1;
  });
  const [dx, dy] = dirs[Math.floor(Math.random() * dirs.length)];
  if (dx) npc.face = dx;
  npc.from = { x: npc.x, y: npc.y }; npc.tx += dx; npc.ty += dy; npc.t0 = now;
}
function npcMove(now) {
  if (!npc.from) return 0;
  const k = Math.min(1, (now - npc.t0) / 380);              // one tile in ~0.4s, like walking in-game
  npc.x = npc.from.x + (npc.tx * TILE - npc.from.x) * k;
  npc.y = npc.from.y + (npc.ty * TILE - npc.from.y) * k;
  if (k >= 1) npc.from = null;
  return k < 1 ? Math.floor(k * 4) % 2 : 0;                 // two-frame step bob
}
function draw(t) {
  sx.imageSmoothingEnabled = false;
  if (state.view === 'room') {
    if (IMG.room.complete) sx.drawImage(IMG.room, 0, 0, 320, 200);
    npcThink(t);
    const step = npcMove(t);
    if (IMG.walker.complete) {
      const h = 40, w = Math.round(h * IMG.walker.width / IMG.walker.height);
      const x = Math.round(npc.x - w / 2), y = Math.round(npc.y - h + 8 - step);
      sx.save();
      if (npc.face < 0) { sx.translate(x * 2 + w, 0); sx.scale(-1, 1); }
      sx.drawImage(IMG.walker, x, y, w, h);
      sx.restore();
    }
  } else {
    sx.fillStyle = state.mode === 'away' ? '#2a0610' : '#13305a'; sx.fillRect(0, 0, 320, 200);
    for (let y = 0; y < 200; y += 4) { sx.fillStyle = 'rgba(255,255,255,.025)'; sx.fillRect(0, y, 320, 1); }
    if (IMG.portrait.complete) {
      const h = 200, w = h * IMG.portrait.width / IMG.portrait.height;
      const bob = state.talking ? Math.sin(t / 90) * 1.5 : Math.sin(t / 700);
      sx.drawImage(IMG.portrait, (320 - w) / 2, bob + 2, w, h);
    }
  }
  requestAnimationFrame(draw);
}

// ---------- wiring ----------
$('ask').onsubmit = (e) => { e.preventDefault(); ask($('text').value); };
$('mic').onclick = listen;
$('stop').onclick = stopAll;
document.querySelectorAll('.modes button').forEach(b => b.onclick = () => setMode(b.dataset.mode));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { e.preventDefault(); stopAll(); }
  if (e.key === 'F1') { e.preventDefault(); setMode('talk'); }
  if (e.key === 'F2') { e.preventDefault(); setMode('away'); }
  if (e.key === 'F3') { e.preventDefault(); setMode('home'); }
  if (e.key === ' ' && document.activeElement === $('text') && !$('text').value) { e.preventDefault(); listen(); }
});
setInterval(() => { $('clock').textContent = new Date().toTimeString().slice(0, 5); }, 1000);
setInterval(health, 5000);
health(); startCam().then(watch); requestAnimationFrame(draw);
say('안녕, 난 케빈이야. 이 집은 내가 맡을게. 말 걸어줘.', { speak: false });
$('text').focus();
