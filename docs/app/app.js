// KEVIN web: DOS-style face, room camera and voice, devices through the local bridge.
const params = new URLSearchParams(location.search);
const BRIDGE = params.get('bridge') || (location.hostname.endsWith('github.io') ? 'http://127.0.0.1:8787' : location.origin);
const $ = (id) => document.getElementById(id);
const scene = $('scene'), sx = scene.getContext('2d');
const eye = $('eye'), ex = eye.getContext('2d', { willReadFrequently: true });
const state = { mode: 'talk', view: 'room', talking: false, motion: 0, prev: null, streak: 0,
                lastMotion: Date.now(), guardBusy: false, devicesOn: null, bridge: false, lastGuard: 0 };
let requestVersion = 0, currentJob = null, modeVersion = 0;
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
let voiceVersion = 0, voiceAbort = null, voiceUrl = null;
function silence() {
  voiceVersion++;
  voiceAbort?.abort(); voiceAbort = null;
  if (voiceAudio) { voiceAudio.pause(); voiceAudio = null; }
  if (voiceUrl) { URL.revokeObjectURL(voiceUrl); voiceUrl = null; }
  if ('speechSynthesis' in window) speechSynthesis.cancel();
  state.talking = false;
}
async function speakOut(text) {
  if (!text) return;
  silence();
  const version = voiceVersion;
  voiceAbort = new AbortController();
  try {
    const r = await fetch(BRIDGE + '/tts', { method: 'POST', signal: voiceAbort.signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
    if (!r.ok) throw new Error('tts');
    const blob = await r.blob();
    if (version !== voiceVersion) return;
    const url = voiceUrl = URL.createObjectURL(blob);
    voiceAudio = new Audio(url);
    voiceAudio.onplay = () => state.talking = true;
    voiceAudio.onended = () => { if (version === voiceVersion) state.talking = false; URL.revokeObjectURL(url); };
    await voiceAudio.play();
  } catch {
    if (version !== voiceVersion) return;
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
  const result = await r.json();
  if (!r.ok) throw new Error(result.reply || '연결 응답을 확인하지 못했어.');
  return result;
}
async function health() {
  try {
    const h = await call('/health');
    state.bridge = true;
    $('demo-note').hidden = h.simulation !== true;
    chip('chip-bridge', 'ok');
    chip('chip-ai', h.openai ? 'ok' : 'warn', h.openai ? 'AI·OpenAI' : 'AI·LOCAL');
    chip('chip-led', h.live && h.led ? 'warn' : '', h.led ? 'LED·설정됨' : 'LED·미설정');
    chip('chip-radio', h.live && h.radio ? 'warn' : '', h.radio ? 'RADIO·설정됨' : 'RADIO·미설정');
  } catch { state.bridge = false; chip('chip-bridge', 'bad', 'BRIDGE 꺼짐'); }
}

async function ask(text) {
  text = text.trim(); if (!text) return;
  const version = ++requestVersion;
  currentJob = null; silence();
  showJob({status:'planning'});
  $('text').value = ''; state.view = 'talk'; state.lastMotion = Date.now();
  log('나: ' + text);
  say('응, 들었어.', { speak: false });
  if (!state.bridge) { showJob({status:'failed'}); say('MSI 연결기가 꺼져 있어. cmd에서 kevin 을 실행해줘.'); return; }
  try {
    const r = await call('/say', { text });
    if (version !== requestVersion) return;
    say(r.reply, {speak: false});
    if (!r.job_id) throw new Error('실행 번호를 받지 못했어.');
    currentJob = r.job_id;
    monitorJob(r.job_id, version);
  } catch (error) {
    if (version !== requestVersion) return;
    showJob({status:'failed'});
    say(error.message || '연결이 끊겨 실행 결과를 확인하지 못했어.');
  }
}
async function stopAll() {
  const version = ++requestVersion;
  currentJob = null; silence();
  showJob({status:'stopping'});
  $('alert').hidden = true;
  say('중단을 요청하고 있어.', {speak: false});
  try {
    const r = await call('/stop', {});
    if (version !== requestVersion) return;
    if (!r.job_id) throw new Error('정지 응답을 받지 못했어.');
    currentJob = r.job_id; monitorJob(r.job_id, version);
  } catch {
    if (version !== requestVersion) return;
    state.devicesOn = null;
    $('job-status').textContent = '정지 확인 실패';
    $('job-status').dataset.status = 'failed';
    say('중단 요청이 전달됐는지 확인하지 못했어. 장치를 직접 확인해 줘.');
    log('정지 결과 확인 불가');
  }
}

function showJob(job) {
  const labels = {planning:'계획 중', running:'실행 중', observing:'관찰 중', stopping:'정지 확인 중',
    complete:'응답 확인', preview:'미리보기', failed:'실행 실패', stopped:'중단됨', unsupported:'지원하지 않는 요청'};
  $('job-status').textContent = labels[job.status] || '결과 확인 중';
  $('job-status').dataset.status = job.status;
  const replies = Object.entries(job.device_results || {}).map(([name, reply]) =>
    `${name === 'led' ? '전광판' : '라디오'}: ${reply?.status || '미확인'}`);
  $('device-results').textContent = replies.join(' · ') || '아직 장치 응답이 없습니다.';
  const observation = job.observation || {};
  $('observation-result').textContent = observation.status === 'available'
    ? `화면 변화 ${observation.changed ? '있음' : '없음'} · 실제 목표 달성·소리는 별도 확인`
    : '관찰 자료 없음 · 장치 응답과 실제 동작은 구분합니다.';
  const receipt = job.receipt;
  const valid = receipt && /^\/evidence\/[0-9]{8}-[0-9]{6}-[a-f0-9]{6}\/receipt\.json$/.test(receipt.url);
  $('receipt-link').hidden = !valid;
  if (valid) {
    $('receipt-link').href = BRIDGE + receipt.url;
    $('receipt-link').textContent = receipt.integrity_verified ? '서명·이미지 해시 확인된 기록 ↗' : '관찰 기록 확인 ↗';
    const prefix = receipt.url.slice(0, receipt.url.lastIndexOf('/') + 1);
    $('evidence-before').src = BRIDGE + prefix + 'before.jpg';
    $('evidence-after').src = BRIDGE + prefix + 'after1.jpg';
  }
  $('evidence-images').hidden = !valid;
}
async function monitorJob(id, version) {
  const deadline = Date.now() + 180000;
  try {
    while (version === requestVersion && currentJob === id) {
      const data = await call('/jobs/' + id);
      if (version !== requestVersion || currentJob !== id) return;
      const job = data.job;
      if (!job || job.job_id !== id) throw new Error('실행 결과를 찾지 못했어.');
      showJob(job);
      if (['complete','preview','failed','stopped','unsupported'].includes(job.status)) {
        currentJob = null;
        if (job.status === 'stopped') state.devicesOn = false;
        else if (job.status === 'failed') state.devicesOn = null;
        const message = job.message || '실행 결과를 확인해 줘.';
        say(message); log('KEVIN: ' + message);
        return;
      }
      if (Date.now() > deadline) throw new Error('응답이 늦어 실행 결과를 확인하지 못했어.');
      await new Promise(resolve => setTimeout(resolve, 400));
    }
  } catch (error) {
    if (version !== requestVersion) return;
    currentJob = null; state.devicesOn = null;
    $('job-status').textContent = '결과 확인 불가';
    $('job-status').dataset.status = 'failed';
    say(error.message); log('실행 결과 확인 불가');
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
async function setMode(m) {
  const version = ++modeVersion;
  try { await call('/mode', {mode:m}); } catch { say('모드를 바꾸지 못했어. 연결을 확인해 줘.'); return; }
  if (version !== modeVersion) return;
  state.mode = m; state.streak = 0; state.lastMotion = Date.now();
  document.querySelectorAll('.modes button').forEach(b => b.classList.toggle('on', b.dataset.mode === m));
  const notes = { talk: '말을 걸면 케빈이 대답해요.', away: '사람을 확인하면 경보를 요청해요. 실제 신고 기능은 없습니다.', home: '새 영상에서 사람이 없는 상태를 30초간 확인해야 기기를 꺼요. 확인 불가는 보류해요.' };
  $('mode-note').textContent = notes[m];
  say({ talk: '응, 말 걸어줘.', away: '다녀와. 사람이 보이는지 확인할게.', home: '사람이 없는 상태가 확인되면 끌게. 못 보면 기다릴게.' }[m]);
  state.view = m === 'talk' ? 'talk' : 'room';
  log('모드: ' + m);
}

// ---------- webcam: KEVIN's eye ----------
const roomCam = new Image();
roomCam.crossOrigin = 'anonymous';
let camSource = 'room';
let lastRoomFrame = 0;
async function startCam() {
  roomCam.onload = () => { lastRoomFrame = Date.now(); chip('chip-cam', 'ok', 'CAM·방'); };
  roomCam.onerror = async () => {
    if (camSource !== 'room') return;
    lastRoomFrame = 0; state.prev = null;
    chip('chip-cam', 'bad', 'CAM·확인 불가');
    $('eye-note').textContent = '방 카메라를 확인할 수 없어 자동 동작을 보류해요.';
  };
  pullRoomCam();
}
function pullRoomCam() {
  if (camSource !== 'room') return;
  const next = new Image();
  next.crossOrigin = 'anonymous';
  next.onload = () => { roomCam.src = next.src; setTimeout(pullRoomCam, 250); };
  next.onerror = () => { roomCam.onerror(); setTimeout(pullRoomCam, 2000); };
  next.src = BRIDGE + '/camera.jpg?t=' + Date.now();
}
function camReady() { return roomCam.complete && roomCam.naturalWidth > 0 && Date.now() - lastRoomFrame < 2500; }
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
    if (state.mode === 'home' && !currentJob && !state.guardBusy && Date.now() - state.lastGuard >= 5000) guard('presence');
  }
  } catch { state.prev = null; $('eye-note').textContent = '영상을 읽지 못해 자동 동작을 보류해요.'; }
  finally { setTimeout(watch, 500); }
}
async function guard(kind, idle = 0) {
  state.guardBusy = true;
  state.lastGuard = Date.now();
  const version = requestVersion, mode = state.mode;
  try {
    const body = {kind, mode};
    const r = state.bridge ? await call('/guard', body) : { action: 'none' };
    if (version !== requestVersion || mode !== state.mode) return;
    if (r.seen) $('eye-note').textContent = '본 것: ' + r.seen;
    if (r.presence === 'present') $('eye-note').textContent = '사람이 보여요. 절약 동작을 보류해요.';
    if (r.presence === 'absent') $('eye-note').textContent = `사람 없음 확인 중 · ${r.absence_seconds || 0}초`;
    if (r.action === 'unavailable') $('eye-note').textContent = r.reply;
    if (r.action === 'intruder_alert') {
      $('alert').hidden = false; state.view = 'talk'; say(r.reply); log('침입 감지 → 경보' + (r.seen ? ' (' + r.seen + ')' : ''));
      setTimeout(() => { $('alert').hidden = true; }, 15000);
    } else if (r.action === 'power_save') {
      say(r.reply, {speak:false}); log('절약: 중단 요청');
    }
    if (r.job_id) { currentJob = r.job_id; monitorJob(r.job_id, version); }
  } catch { /* bridge offline */ }
  finally { setTimeout(() => state.guardBusy = false, 3000); }
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
if (params.get('view') === 'talk') { state.view = 'talk'; say(params.get('line') || '좋아, 산타 날리고 눈 펑펑 내리게 할게!', { speak: false }); }
if (params.get('view') === 'alert') { state.view = 'talk'; $('alert').hidden = false; say('경보 화면 미리보기야. 실제 신고 기능은 없어.', { speak: false }); }
$('text').focus();
