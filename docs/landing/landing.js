'use strict';
document.documentElement.classList.add('js');
const menu = document.querySelector('.menu-toggle');
const navigation = document.querySelector('#navigation');
menu.hidden = false;
menu.addEventListener('click', () => {
  const open = menu.getAttribute('aria-expanded') !== 'true';
  menu.setAttribute('aria-expanded', String(open));
  navigation.classList.toggle('open', open);
  menu.textContent = open ? '닫기' : '메뉴';
});
navigation.addEventListener('click', event => {
  if (event.target.closest('a')) {
    menu.setAttribute('aria-expanded', 'false');
    navigation.classList.remove('open');
    menu.textContent = '메뉴';
  }
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && menu.getAttribute('aria-expanded') === 'true') {
    menu.click(); menu.focus();
  }
});
const scenes = {
  holiday: {
    request: '“크리스마스 분위기 내줘.\n눈은 많이.”',
    plan: {preset:'christmas_train', snow:'heavy', music:'play'},
    title:'눈 내리는 LED와 캐럴',
    copy:'LED 장면과 라디오 재생을 나누어 계획합니다. 아래 GIF는 생성 프레임 미리보기이며 실물 촬영이 아닙니다.',
    text:''
  },
  birthday: {
    request:'“지민이 생일을\n축하해줘.”',
    plan:{led:{mode:'text',text:'지민아 생일 축하해'},music:{mode:'song',title:'생일축하'}},
    title:'축하 문구와 생일 멜로디',
    copy:'문구와 멜로디를 만드는 구현을 설명하는 예시입니다. 이 장면의 실물 동작은 아직 검증하지 않았습니다.',
    text:'HAPPY BIRTHDAY'
  },
  game: {
    request:'“가위바위보 하자.\n나는 바위.”',
    plan:{human:'rock',kevin:'scissors',judge:'Python rules',result:'human_win'},
    title:'규칙 기반 게임 판정',
    copy:'바위와 가위의 예시에서는 사람이 이깁니다. 결과와 점수는 Python 규칙으로 판정합니다. 여기는 실제 게임 실행이 아닌 흐름 예시입니다.',
    text:'ROCK > SCISSORS'
  }
};
const tabs = [...document.querySelectorAll('[data-case]')];
const tablist = document.querySelector('.demo-tabs');
const panel = document.querySelector('#preview');
tablist.setAttribute('role','tablist');
panel.setAttribute('role','tabpanel');
tabs.forEach((tab,index) => {
  tab.setAttribute('role','tab');
  tab.setAttribute('aria-selected',String(index === 0));
  tab.tabIndex = index === 0 ? 0 : -1;
});
function select(tab) {
  const scene = scenes[tab.dataset.case];
  tabs.forEach(item => {
    const active = item === tab;
    item.classList.toggle('selected', active);
    item.setAttribute('aria-selected',String(active));
    item.tabIndex = active ? 0 : -1;
  });
  panel.setAttribute('aria-labelledby',tab.id);
  document.querySelector('#request').textContent = scene.request;
  document.querySelector('#plan').textContent = JSON.stringify(scene.plan,null,2);
  document.querySelector('#outcome-title').textContent = scene.title;
  document.querySelector('#outcome-copy').textContent = scene.copy;
  const label = document.querySelector('#scene-text');
  label.textContent = scene.text;
  label.hidden = !scene.text;
  document.querySelector('#scene-gif').hidden = Boolean(scene.text);
}
tabs.forEach((tab,index) => {
  tab.addEventListener('click',() => select(tab));
  tab.addEventListener('keydown',event => {
    let next;
    if (event.key === 'ArrowRight') next = (index+1)%tabs.length;
    if (event.key === 'ArrowLeft') next = (index+tabs.length-1)%tabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabs.length-1;
    if (next !== undefined) { event.preventDefault(); select(tabs[next]); tabs[next].focus(); }
  });
});
const copy = document.querySelector('#copy-code');
copy.hidden = false;
copy.addEventListener('click',async () => {
  const status = document.querySelector('#copy-status');
  try {
    await navigator.clipboard.writeText(document.querySelector('#quickstart').textContent);
    status.textContent = '명령어를 복사했습니다.';
  } catch {
    status.textContent = '자동 복사가 제한되었습니다. 위 명령어를 선택해 복사해 주세요.';
  }
});
