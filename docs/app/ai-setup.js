// Credentials stay in this form until submitted to the same-origin local bridge.
window.KevinAI = (() => {
  const $ = id => document.getElementById(id), dialog = $('ai-settings');
  let settings = null, csrf = '', ticket = '', testedDraft = '', initialOffered = false, generation = 0;
  const bridge = () => new URL(new URLSearchParams(location.search).get('bridge') ||
    (location.hostname.endsWith('github.io') ? 'http://127.0.0.1:8787' : location.origin));
  const provider = () => document.querySelector('input[name="provider"]:checked')?.value;
  const feedback = (text, error = false) => { $('ai-feedback').textContent = text; $('ai-feedback').classList.toggle('error', error); };
  function invalidate() { ticket = ''; testedDraft = ''; $('ai-save').disabled = true; feedback('연결을 확인한 뒤 저장할 수 있어요.'); }
  function changedProvider() {
    const selected = provider();
    $('ai-key-row').hidden = selected !== 'openai'; $('ai-url-row').hidden = selected !== 'ollama';
    $('ai-model').value = selected === settings?.provider ? settings.model : settings?.defaults?.[selected] || '';
    $('ai-key').disabled = settings?.managed.includes('api_key') || false;
    $('ai-key-note').textContent = settings?.has_key ? '등록된 키가 있어요. 비워 두면 기존 키를 사용합니다. 새 키를 저장하면 개인 설정 파일에 평문으로 보관합니다.' : '키는 이 컴퓨터의 개인 설정 파일에 평문으로 저장하고 OpenAI 인증에만 사용합니다.';
    invalidate();
  }
  async function api(path, body) {
    const response = await fetch(new URL(path, bridge()), body ? {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-KEVIN-Setup': csrf},
      body: JSON.stringify(body), cache: 'no-store'
    } : {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'AI 설정 연결을 확인해 주세요.');
    return data;
  }
  function draft() {
    return {provider: provider(), model: $('ai-model').value.trim(), ollama_url: $('ai-url').value.trim(),
            api_key: $('ai-key').value, revision: settings.revision};
  }
  function close() {
    generation++; dialog.close(); $('ai-key').value = ''; $('ai-key').type = 'password';
    $('ai-reveal').textContent = '표시'; $('ai-reveal').setAttribute('aria-pressed', 'false');
    csrf = ''; ticket = ''; testedDraft = '';
  }
  async function open() {
    if (dialog.open) return;
    generation++; settings = null; $('ai-key').value = ''; invalidate(); feedback('설정을 불러오는 중…');
    const local = bridge().origin === location.origin;
    $('ai-fields').hidden = !local; $('ai-remote').hidden = local;
    $('ai-local-link').href = new URL('/?ai=setup', bridge()).href;
    dialog.showModal();
    if (!local) return;
    const current = generation;
    try {
      const data = await api('/ai/config');
      if (current !== generation || !dialog.open) return;
      settings = data.settings; csrf = data.csrf;
      document.querySelectorAll('input[name="provider"]').forEach(input => {
        input.checked = settings.configured && input.value === settings.provider;
        input.disabled = settings.managed.includes('provider');
      });
      $('ai-url').value = settings.ollama_url; $('ai-managed').hidden = !settings.managed.length;
      changedProvider(); $('ai-test').disabled = !provider();
      feedback(settings.verified ? '현재 설정은 연결 확인을 마쳤어요.' : '방식을 선택하고 연결을 확인해 주세요.');
    } catch (error) { feedback(error.message, true); $('ai-test').disabled = true; }
  }
  document.querySelectorAll('input[name="provider"]').forEach(input => input.addEventListener('change', () => {
    changedProvider(); $('ai-test').disabled = false;
  }));
  ['ai-key', 'ai-model', 'ai-url'].forEach(id => $(id).addEventListener('input', invalidate));
  $('ai-close').onclick = close; $('ai-later').onclick = close;
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  $('ai-reveal').onclick = () => {
    const show = $('ai-key').type === 'password'; $('ai-key').type = show ? 'text' : 'password';
    $('ai-reveal').textContent = show ? '숨기기' : '표시'; $('ai-reveal').setAttribute('aria-pressed', String(show));
  };
  $('ai-test').onclick = async () => {
    if (!settings || !provider()) return;
    const value = draft(), encoded = JSON.stringify(value), current = generation;
    $('ai-test').disabled = true; feedback('연결 확인 중… 기기는 움직이지 않아요.');
    try {
      const result = await api('/ai/test', {draft: value});
      if (current !== generation || !dialog.open || encoded !== JSON.stringify(draft())) return;
      ticket = result.test_id; testedDraft = encoded; $('ai-save').disabled = false;
      feedback('✓ 연결을 확인했어요. 사용 모델: ' + result.model);
    } catch (error) { if (current === generation) feedback(error.message, true); }
    finally { if (current === generation) $('ai-test').disabled = !provider(); }
  };
  $('ai-form').onsubmit = async event => {
    event.preventDefault();
    if (!ticket || testedDraft !== JSON.stringify(draft())) return;
    $('ai-save').disabled = true;
    const current = generation;
    try {
      await api('/ai/save', {draft: draft(), test_id: ticket});
      if (current === generation) close();
      window.dispatchEvent(new Event('kevin-ai-saved'));
    } catch (error) { if (current === generation) { ticket = ''; feedback(error.message, true); } }
  };
  $('chip-ai').onclick = open;
  return {
    open,
    update(value) {
      const button = $('chip-ai'), label = value?.provider === 'openai' ? 'OpenAI' : '로컬';
      button.textContent = value?.verified ? 'AI · ' + label : 'AI 설정 · 확인 필요';
      button.className = 'chip ' + (value?.verified ? 'ok' : 'warn');
      button.title = value?.verified ? '명령 해석 AI 설정 · 마지막 연결 확인 완료' : '명령 해석 AI를 선택하고 연결을 확인하세요.';
      if (!initialOffered && value && (!value.configured || new URLSearchParams(location.search).get('ai') === 'setup')) {
        initialOffered = true; open();
      }
    }
  };
})();
