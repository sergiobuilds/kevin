const $ = selector => document.querySelector(selector);
let busy = false;
let revision = 0;
const labels = ['현관', '진열대', '작업대'];
const statusLabels = {acknowledged: '응답 확인', sent_unverified: '전송 · 실물 미확인', failed: '확인 실패',
  stopped: '중단 요청', ready: '준비됨', busy: '처리 중'};

function showState(state) {
  $('#connection').textContent = '로컬 WLED 연결';
  const device = state.device || {};
  const segments = device.state?.seg || [];
  $('#zones').replaceChildren(...segments.map(segment => {
    const node = document.createElement('div');
    node.className = 'zone';
    const value = segment.on && device.state?.on !== false ? segment.bri / 255 : 0;
    const color = segment.col?.[0] || [255, 214, 143];
    node.style.setProperty('--lamp', value ? `rgb(${color.join(',')})` : '#4c5446');
    node.style.setProperty('--glow', `rgba(${color.join(',')},${(value * .32).toFixed(3)})`);
    for (const className of ['fixture', 'cone', 'furniture']) {
      const part = document.createElement('div'); part.className = className; node.append(part);
    }
    const name = document.createElement('b'); name.textContent = labels[segment.id] || segment.n;
    const detail = document.createElement('small'); detail.textContent = `${segment.on ? 'ON' : 'OFF'} · ${segment.bri} / 255`;
    node.append(name, detail); return node;
  }));
  const traces = device.traces || [];
  $('#request-count').textContent = `HTTP 요청 ${traces.length}회`;
  $('#trace').textContent = traces.length ? JSON.stringify(traces.slice(-12), null, 2) : '아직 요청이 없습니다.';
  $('#fault').checked = device.fault === 'ignore';
  for (const card of document.querySelectorAll('.track')) {
    const track = state.tracks?.[card.dataset.device];
    if (track) {
      card.querySelector('.track-data').textContent = JSON.stringify(track, null, 2);
      if (track.fault) card.querySelector('[data-track-fault]').value = track.fault;
    }
  }
}

function showResult(out) {
  for (const [device, result] of Object.entries(out.device_results || {})) {
    const card = document.querySelector(`[data-device="${device}"]`);
    if (!card) continue;
    card.dataset.result = result.status;
    card.querySelector('.track-outcome').textContent = `${statusLabels[result.status] || result.status} · ${result.status === 'failed' ? '정지 응답을 확인하지 못했어요.' : '중단 요청 결과를 확인해 주세요. 실물 정지는 별도 확인이 필요합니다.'}`;
    card.querySelector('.track-outcome').classList.toggle('bad', result.status === 'failed');
  }
  if (out.device) {
    const card = document.querySelector(`[data-device="${out.device}"]`);
    const detail = out.result?.detail || {};
    const line = card.querySelector('.track-outcome');
    line.textContent = `${statusLabels[out.status] || out.status} · ${out.message}`;
    line.classList.toggle('bad', ['failed', 'error', 'unsupported'].includes(out.status));
    card.querySelector('.track-data').textContent = JSON.stringify(out.result || out, null, 2);
    card.dataset.result = out.status;
    card._lastResult = out.result;
  }
  $('#message').textContent = out.message || '결과를 확인했어.';
  $('#status-dot').className = 'status-dot ' + (['failed', 'stale', 'error'].includes(out.status) ? 'bad' : 'good');
  const result = out.result?.detail || out.result?.device?.detail || out.result || {};
  const matched = result.readback_matches;
  const preserved = result.untouched_preserved;
  $('#readback').textContent = matched === undefined ? '—' : matched ? '상태 일치' : '상태 불일치';
  $('#preserved').textContent = preserved === undefined ? '—' : preserved ? '확인됨' : '확인 필요';
  for (const [id, url] of [['before', out.before_url], ['after', out.after_url]]) {
    const image = $('#' + id); image.hidden = !url;
    if (url) image.src = url; else image.removeAttribute('src');
  }
  const rec = out.receipt;
  $('#receipt-link').hidden = !rec;
  if (rec) {
    $('#receipt-link').href = rec.url;
    $('#receipt-status').textContent = `${rec.verified ? '서명·사진 해시 확인됨' : '서명 확인 필요'} · 합성 장면`;
  } else {
    $('#receipt-status').textContent = out.status === 'stale' ? '새 영상이 없어 관찰 영수증을 만들지 않았습니다.' : '이 요청에는 영상 영수증이 없습니다.';
  }
}

function setBusy(value) {
  busy = value;
  document.querySelectorAll('button:not(#stop):not(#extended-stop), select, input').forEach(node => { node.disabled = value; });
}

async function refresh(expectedRevision = revision) {
  const response = await fetch('/api/state', {cache: 'no-store'});
  if (!response.ok) throw new Error('상태를 읽지 못했어. 서버 연결을 확인해 줘.');
  const state = await response.json();
  if (expectedRevision === revision) showState(state);
}

async function action(body) {
  if (busy && body.action !== 'stop') return;
  const ownRevision = ++revision;
  setBusy(true);
  $('#message').textContent = body.action === 'stop' ? '중단을 요청하고 있어.' : '요청하고, 돌아온 상태를 확인하는 중이야.';
  try {
    const response = await fetch('/api/action', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const out = await response.json();
    if (ownRevision !== revision) return;
    if (!response.ok) throw new Error(out.message || out.error || '요청을 처리하지 못했어.');
    showResult(out);
    await refresh(ownRevision);
    if (ownRevision !== revision) return;
    if (out.device && out.result) {
      const card = document.querySelector(`[data-device="${out.device}"]`);
      card.querySelector('.track-data').textContent = JSON.stringify({result: out.result,
        fixture: JSON.parse(card.querySelector('.track-data').textContent)}, null, 2);
    }
    if (body.action === 'reset') {
      document.querySelectorAll('.track-outcome').forEach(node => { node.textContent = '초기화했어요. 새 요청을 실행해 보세요.'; node.classList.remove('bad'); });
      document.querySelectorAll('[data-track-fault]').forEach(node => { node.value = 'none'; });
    }
  } catch (error) {
    if (ownRevision === revision) {
      $('#message').textContent = error.message;
      $('#status-dot').className = 'status-dot bad';
    }
  } finally {
    if (ownRevision === revision) setBusy(false);
  }
}

$('#brightness').addEventListener('input', event => { $('#brightness-value').textContent = `${event.target.value} / 255`; });
$('#light-form').addEventListener('submit', event => {
  event.preventDefault();
  action({action: 'light', segment: Number($('#segment').value), brightness: Number($('#brightness').value), on: $('#power').checked});
});
document.querySelectorAll('[data-scenario]').forEach(button => button.addEventListener('click', () => action({action: 'observe', scenario: button.dataset.scenario})));
$('#fault').addEventListener('change', event => action({action: 'fault', fault: event.target.checked ? 'ignore' : 'none'}));
$('#reset').addEventListener('click', () => action({action: 'reset'}));
$('#stop').addEventListener('click', () => action({action: 'stop'}));
$('#extended-stop').addEventListener('click', () => action({action: 'stop'}));
$('#extended-reset').addEventListener('click', () => action({action: 'reset'}));
document.querySelectorAll('.track [data-plan]').forEach(button => button.addEventListener('click', () =>
  action({action: 'device', device: button.closest('.track').dataset.device, plan: JSON.parse(button.dataset.plan)})));
document.querySelectorAll('[data-track-fault]').forEach(select => select.addEventListener('change', () =>
  action({action: 'device_fault', device: select.closest('.track').dataset.device, fault: select.value})));
document.addEventListener('keydown', event => { if (event.key === 'Escape') action({action: 'stop'}); });
refresh().catch(error => { if (revision === 0) { $('#connection').textContent = '연결 확인 필요'; $('#message').textContent = error.message; } });
