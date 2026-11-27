// Small DOM helpers: elements, hints (tooltips), menus, popups and dialogs.

export const TITLE = 'Mass Synced Lyrics';

export function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k === 'hint') hint(e, v);
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'style' && typeof v === 'object') Object.assign(e.style, v);
    else if (v === true) e.setAttribute(k, '');
    else e.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// ---------------------------------------------------------------- hints (tooltips)
let tipEl = null;
let tipTimer = null;

export function showTip(text, x, y) {
  if (!text) { hideTip(); return; }
  if (!tipEl) { tipEl = el('div', { id: 'tip' }); document.body.append(tipEl); }
  tipEl.textContent = text;
  tipEl.classList.remove('hidden');
  const r = tipEl.getBoundingClientRect();
  let left = x + 14, top = y + 18;
  if (left + r.width > innerWidth - 8) left = Math.max(8, innerWidth - r.width - 8);
  if (top + r.height > innerHeight - 8) top = Math.max(8, y - r.height - 14);
  tipEl.style.left = left + 'px';
  tipEl.style.top = top + 'px';
}

export function hideTip() {
  clearTimeout(tipTimer);
  if (tipEl) tipEl.classList.add('hidden');
}

export function hint(e, text) {
  e._hint = text;
  if (e._hinted) return e;
  e._hinted = true;
  e.addEventListener('mouseenter', (ev) => {
    clearTimeout(tipTimer);
    tipTimer = setTimeout(() => showTip(typeof e._hint === 'function' ? e._hint() : e._hint, ev.clientX, ev.clientY), 550);
  });
  e.addEventListener('mousemove', (ev) => { e._lastXY = [ev.clientX, ev.clientY]; });
  e.addEventListener('mouseleave', hideTip);
  e.addEventListener('mousedown', hideTip);
  return e;
}

// ---------------------------------------------------------------- menus
let openMenus = [];

export function closeMenus() {
  for (const m of openMenus) m.remove();
  openMenus = [];
}

/** items: [{label, action, disabled, checked, sub: [...]}, '-' for a separator] */
export function menu(items, x, y, level = 0) {
  if (level === 0) closeMenus();
  const m = el('div', { class: 'menu' });
  for (const it of items) {
    if (it === '-') { m.append(el('div', { class: 'msep' })); continue; }
    const mi = el('div', { class: 'mi' + (it.disabled ? ' dis' : '') + (it.sub ? ' sub' : '') +
      (it.checked !== undefined ? ' has-chk' : '') });
    if (it.checked !== undefined) mi.append(el('span', { class: 'chk', text: it.checked ? '✓' : '' }));
    mi.append(it.label);
    if (it.hint) hint(mi, it.hint);
    if (it.sub) {
      mi.addEventListener('mouseenter', () => {
        while (openMenus.length > level + 1) openMenus.pop().remove();
        const r = mi.getBoundingClientRect();
        menu(it.sub, r.right - 2, r.top - 4, level + 1);
      });
    } else {
      mi.addEventListener('mouseenter', () => { while (openMenus.length > level + 1) openMenus.pop().remove(); });
      mi.addEventListener('click', (e) => {
        e.stopPropagation();
        if (it.disabled) return;
        closeMenus();
        it.action && it.action();
      });
    }
    m.append(mi);
  }
  document.body.append(m);
  openMenus.push(m);
  const r = m.getBoundingClientRect();
  m.style.left = Math.max(4, Math.min(x, innerWidth - r.width - 4)) + 'px';
  m.style.top = Math.max(4, Math.min(y, innerHeight - r.height - 4)) + 'px';
  return m;
}

document.addEventListener('mousedown', (e) => {
  if (!openMenus.some((m) => m.contains(e.target))) closeMenus();
}, true);
window.addEventListener('blur', closeMenus);

// ---------------------------------------------------------------- popups (closed by clicking elsewhere)
export function popup(content, anchor, onClose) {
  const p = el('div', { class: 'popup' }, content);
  document.body.append(p);
  const r = anchor.getBoundingClientRect();
  const pr = p.getBoundingClientRect();
  p.style.left = Math.max(4, Math.min(r.left, innerWidth - pr.width - 4)) + 'px';
  p.style.top = Math.min(r.bottom + 4, innerHeight - pr.height - 4) + 'px';
  const away = (e) => { if (!p.contains(e.target) && !anchor.contains(e.target)) close(); };
  const key = (e) => { if (e.key === 'Escape' || e.key === 'Enter') close(); };
  function close() {
    if (!p.isConnected) return;
    p.remove();
    document.removeEventListener('mousedown', away, true);
    document.removeEventListener('keydown', key, true);
    onClose && onClose();
  }
  setTimeout(() => {
    document.addEventListener('mousedown', away, true);
    document.addEventListener('keydown', key, true);
  });
  p.close = close;
  return p;
}

// ---------------------------------------------------------------- dialogs
let modalOpen = 0;
export const modalIsOpen = () => modalOpen > 0;

/** A modal dialog. buttons: [{label, value, accent, cancel}]. Resolves with the clicked button's value. */
export function dialog({ title = TITLE, message = '', kind = '', body = null, buttons = [{ label: 'OK', value: true, accent: true }] }) {
  return new Promise((resolve) => {
    modalOpen++;
    hideTip();
    closeMenus();
    const back = el('div', { id: 'modal-back' });
    const box = el('div', { class: 'modal ' + kind, role: 'dialog' }, el('h3', { text: title }));
    if (message) box.append(el('div', { class: 'msg', text: message }));
    if (body) box.append(body);
    const row = el('div', { class: 'btns' });
    const finish = (v) => {
      modalOpen--;
      back.remove();
      document.removeEventListener('keydown', key, true);
      resolve(v);
    };
    let def = null, cancel = null;
    for (const b of buttons) {
      const btn = el('button', { class: b.accent ? 'accent' : '', text: b.label, onclick: () => finish(b.value) });
      if (b.accent) def = b;
      if (b.cancel) cancel = b;
      row.append(btn);
    }
    box.append(row);
    back.append(box);
    document.body.append(back);
    const key = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(cancel ? cancel.value : (buttons.length === 1 ? buttons[0].value : null)); }
      if (e.key === 'Enter' && def && !(e.target instanceof HTMLButtonElement)) { e.preventDefault(); e.stopPropagation(); finish(def.value); }
    };
    document.addEventListener('keydown', key, true);
    const first = box.querySelector('input') || row.querySelector('button.accent') || row.querySelector('button');
    setTimeout(() => first && first.focus());
  });
}

export const alertBox = (message, kind = '', title = TITLE) => dialog({ title, message, kind });
export const errorBox = (message) => dialog({ message, kind: 'error' });

export function confirmBox(message, { ok = 'Yes', cancel = 'No', kind = '', title = TITLE } = {}) {
  return dialog({ title, message, kind, buttons: [{ label: ok, value: true, accent: true }, { label: cancel, value: false, cancel: true }] });
}

export async function promptBox(message, value = '', title = TITLE) {
  const inp = el('input', { type: 'text', value });
  const ok = await dialog({ title, message, body: inp, buttons: [{ label: 'OK', value: true, accent: true }, { label: 'Cancel', value: false, cancel: true }] });
  return ok ? inp.value : null;
}

// ---------------------------------------------------------------- formatting
export function fmtTime(t) {
  t = Math.max(0, t || 0);
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, '0')}`;
}
export function fmtClock(t) {
  t = Math.round(Math.max(0, t));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}
export function fmtTs(t) {
  // LRC timestamp mm:ss.cc
  const cs = Math.round(Math.max(0, t) * 100);
  const m = Math.floor(cs / 6000), s = Math.floor((cs % 6000) / 100), c = cs % 100;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${String(c).padStart(2, '0')}`;
}

export function debounce(fn, ms) {
  let t = null;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  d.flush = (...a) => { clearTimeout(t); fn(...a); };
  return d;
}
