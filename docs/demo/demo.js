const $ = selector => document.querySelector(selector);
let busy = false;
let revision = 0;
const labels = ['현관', '진열대', '작업대'];

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
}

function showResult(out) {
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
  document.querySelectorAll('button:not(#stop), select, input').forEach(node => { node.disabled = value; });
}

async function refresh() {
  const response = await fetch('/api/state', {cache: 'no-store'});
  if (!response.ok) throw new Error('상태를 읽지 못했어. 서버 연결을 확인해 줘.');
  showState(await response.json());
}

async function action(body) {
  if (busy && body.action !== 'stop') return;
  const ownRevision = ++revision;
  setBusy(true);
  $('#message').textContent = body.action === 'stop' ? '중단을 요청하고 있어.' : '요청하고, 돌아온 상태를 확인하는 중이야.';
  try {
    const response = await fetch('/api/action', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const out = await response.json();
    if (!response.ok) throw new Error(out.message || out.error || '요청을 처리하지 못했어.');
    if (ownRevision === revision) showResult(out);
    await refresh();
  } catch (error) {
    $('#message').textContent = error.message;
    $('#status-dot').className = 'status-dot bad';
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
document.addEventListener('keydown', event => { if (event.key === 'Escape') action({action: 'stop'}); });
refresh().catch(error => { $('#connection').textContent = '연결 확인 필요'; $('#message').textContent = error.message; });
