'use strict';

document.documentElement.classList.add('js');

const menu = document.querySelector('.menu-toggle');
const navigation = document.querySelector('#navigation');
menu.hidden = false;

function closeMenu() {
  menu.setAttribute('aria-expanded', 'false');
  menu.textContent = '메뉴';
  navigation.classList.remove('open');
}

menu.addEventListener('click', () => {
  const open = menu.getAttribute('aria-expanded') !== 'true';
  menu.setAttribute('aria-expanded', String(open));
  menu.textContent = open ? '닫기' : '메뉴';
  navigation.classList.toggle('open', open);
});
navigation.addEventListener('click', event => {
  if (event.target.closest('a')) closeMenu();
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && menu.getAttribute('aria-expanded') === 'true') {
    closeMenu();
    menu.focus();
  }
});

const tablist = document.querySelector('.demo-tabs');
const tabs = [...tablist.querySelectorAll('button')];
const panels = tabs.map(tab => document.getElementById(tab.getAttribute('aria-controls')));

function selectScreen(index) {
  tabs.forEach((tab, position) => {
    const active = position === index;
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
    panels[position].hidden = !active;
  });
}

tablist.setAttribute('role', 'tablist');
tabs.forEach((tab, index) => {
  tab.setAttribute('role', 'tab');
  panels[index].setAttribute('role', 'tabpanel');
  panels[index].setAttribute('aria-labelledby', tab.id);
  panels[index].tabIndex = 0;
  tab.addEventListener('click', () => selectScreen(index));
  tab.addEventListener('keydown', event => {
    let next;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabs.length - 1;
    if (next !== undefined) {
      event.preventDefault();
      selectScreen(next);
      tabs[next].focus();
    }
  });
});
selectScreen(0);
tablist.hidden = false;

const copy = document.querySelector('#copy-code');
copy.hidden = false;
copy.addEventListener('click', async () => {
  const status = document.querySelector('#copy-status');
  try {
    await navigator.clipboard.writeText(document.querySelector('#quickstart').textContent);
    status.textContent = '명령어 복사 완료';
  } catch {
    status.textContent = '자동 복사 제한 · 위 명령어의 직접 선택·복사 필요';
  }
});
