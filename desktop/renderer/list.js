// The song list on the Review & player page: a virtual tree (only the rows on screen exist), grouped by
// Artist ▸ Album or by folders, with filters, multi-selection, the Choice column and context menus.
import * as M from './listmodel.js';
import { el, hint, menu, popup } from './ui.js';

const ROW_H = 26;
const NAME_MIN = 180;

export class SongList {
  constructor(app) {
    this.app = app;
    this.rows = {};
    this.tree = { nodes: new Map(), roots: [], shown: 0 };
    this.flat = [];
    this.open = new Map();                 // group id → open (overrides the default)
    this.sel = new Set();
    this.focus = null;
    this.anchor = null;
    this.viewing = '';
    this.ui = { filter: 'All', search: '', onlyFound: true };
    this._job = null;
    this._lastBuild = 0;
    this.build();
  }

  get s() { return this.app.settings; }

  // ------------------------------------------------------------------ DOM
  build() {
    this.root = el('div', { id: 'tree', tabindex: '0' });
    this.head = el('div', { class: 'head' });
    this.body = el('div', { class: 'body' });
    this.inner = el('div', { class: 'inner' });
    this.body.append(this.inner);
    this.root.append(this.head, this.body);
    this.body.addEventListener('scroll', () => { this.head.scrollLeft = this.body.scrollLeft; this.draw(); });
    new ResizeObserver(() => this.layout()).observe(this.body);
    this.inner.addEventListener('mousedown', (e) => this._mouse(e));
    this.inner.addEventListener('dblclick', (e) => this._dbl(e));
    this.inner.addEventListener('contextmenu', (e) => this._context(e));
    this.root.addEventListener('keydown', (e) => this._key(e));
    this.head.addEventListener('contextmenu', (e) => { e.preventDefault(); this.columnsMenu(e.clientX, e.clientY); });
  }

  columns() {
    const want = this.s.list_columns && this.s.list_columns.length ? new Set(this.s.list_columns) : null;
    const cols = M.COLUMNS.filter(([c]) => !want || want.has(c));
    return cols.length ? cols : [M.COLUMNS[0]];
  }

  layout() {
    const cols = this.columns();
    const fixed = cols.reduce((a, [, w]) => a + w, 0);
    const avail = this.body.clientWidth || 600;
    this.nameW = Math.max(NAME_MIN, avail - fixed);
    this.totalW = this.nameW + fixed;
    this.inner.style.width = this.totalW + 'px';
    this.head.replaceChildren();
    const view = this.s.list_view === 'folder' ? 'folder' : 'artist';
    const nameH = el('div', { style: { width: this.nameW + 'px', flex: 'none' }, text: M.VIEW_HEADINGS[view] });
    hint(nameH, M.COLUMN_HINTS.name);
    this.head.append(nameH);
    for (const [c, w, label] of cols) {
      let text = label;
      if (c === 'match' || c === 'fit') {
        const n = c === 'match' ? this.s.list_min_match : this.s.list_min_fit;
        text = n ? `${c === 'match' ? 'Match' : 'Fit'} ≥${n}` : `${c === 'match' ? 'Match' : 'Fit'} ▾`;
      }
      const h = el('div', { style: { width: w + 'px', flex: 'none', justifyContent: M.OPS.includes(c) ? 'center' : '' },
        class: c === 'match' || c === 'fit' ? 'click' : '', text });
      hint(h, M.COLUMN_HINTS[c]);
      if (c === 'match' || c === 'fit') h.addEventListener('click', () => this.scorePopup(c, h));
      this.head.append(h);
    }
    this.draw();
  }

  // ------------------------------------------------------------------ data
  setRows(data) {
    if (data.clear) this.rows = {};
    Object.assign(this.rows, data.upsert || {});
    for (const p of data.remove || []) { delete this.rows[p]; this.sel.delete(p); }
    this.refresh(data.clear);
  }

  filterOpts() {
    return { filter: this.ui.filter, search: this.ui.search, onlyFound: !!this.ui.onlyFound,
      hideSaved: !!this.s.hide_saved, hideLow: !!this.s.hide_low_confidence, minMatch: this.s.list_min_match || 0,
      minFit: this.s.list_min_fit || 0, confT: this.s.confident_threshold, fitT: this.s.confident_fit,
      view: this.s.list_view === 'folder' ? 'folder' : 'artist', root: this.s.music_folder || '' };
  }

  /** Rebuild the tree soon (big lists: at most a few times a second). */
  refresh(now = false) {
    if (this._job) return;
    const n = Object.keys(this.rows).length;
    const wait = now ? 0 : Math.max(0, (n > 5000 ? 400 : n > 1000 ? 150 : 30) - (performance.now() - this._lastBuild));
    this._job = setTimeout(() => { this._job = null; this.rebuild(); }, wait);
  }

  rebuild() {
    this._lastBuild = performance.now();
    this.tree = M.buildTree(this.rows, this.filterOpts());
    for (const id of [...this.sel]) if (!this.tree.nodes.has(id)) this.sel.delete(id);
    this.flat = M.flatten(this.tree, (g) => this.isOpen(g));
    this.inner.style.height = this.flat.length * ROW_H + 'px';
    this.draw();
    this.app.listCount(`${this.tree.shown} shown / ${Object.keys(this.rows).length} songs`);
  }

  isOpen(g) { return this.open.has(g.id) ? this.open.get(g.id) : g.open; }

  setOpen(id, open) {
    this.open.set(id, open);
    this.flat = M.flatten(this.tree, (g) => this.isOpen(g));
    this.inner.style.height = this.flat.length * ROW_H + 'px';
    this.draw();
  }

  // ------------------------------------------------------------------ drawing
  draw() {
    if (this._drawJob) return;
    this._drawJob = requestAnimationFrame(() => { this._drawJob = null; this._draw(); });
  }

  _draw() {
    const top = this.body.scrollTop, h = this.body.clientHeight || 400;
    const first = Math.max(0, Math.floor(top / ROW_H) - 5);
    const last = Math.min(this.flat.length, Math.ceil((top + h) / ROW_H) + 5);
    const cols = this.columns();
    const frag = document.createDocumentFragment();
    for (let i = first; i < last; i++) {
      const n = this.flat[i];
      frag.append(this._row(n, i, cols));
    }
    this.inner.replaceChildren(frag);
    if (!this.flat.length) {
      this.inner.append(el('div', { class: 'empty', text: Object.keys(this.rows).length ?
        'No songs match the filters.' : 'No songs yet — run Find lyrics, Load earlier results or Open file…' }));
    }
  }

  _row(n, i, cols) {
    const tr = el('div', { class: 'tr', style: { top: i * ROW_H + 'px', width: this.totalW + 'px' } });
    tr.dataset.id = n.id;
    let vals, name;
    if (n.kind === 'group') {
      tr.classList.add(n.depth === 0 ? 'group' : 'subgroup');
      vals = M.groupValues(n.stats);
      name = n.label;
    } else {
      const r = this.rows[n.id];
      vals = M.rowValues(r);
      tr.classList.add(M.rowTag(r));
      if (M.isBusy(r)) tr.classList.add('busy');
      if (n.id === this.viewing) tr.classList.add('viewing');
      name = (n.id === this.viewing ? '▶ ' : r.check ? '⚠ ' : '') + M.rowName(n.id, r);
    }
    if (this.sel.has(n.id)) tr.classList.add('sel');
    if (this.focus === n.id) tr.classList.add('focus');
    const nameCell = el('div', { class: 'c-name', style: { width: this.nameW + 'px', flex: 'none',
      paddingLeft: 4 + n.depth * 16 + 'px' } },
    el('span', { class: 'tw', text: n.kind === 'group' ? (this.isOpen(n) ? '▾' : '▸') : '' }),
    el('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis' }, text: name }));
    tr.append(nameCell);
    for (const [c, w] of cols) {
      tr.append(el('div', { class: 'c-' + c + (M.OPS.includes(c) ? ' c-mid' : ''), style: { width: w + 'px', flex: 'none' },
        text: vals[c] ?? '' }));
    }
    return tr;
  }

  // ------------------------------------------------------------------ selection
  songsUnder(id) {
    const n = this.tree.nodes.get(id);
    if (!n) return [];
    return n.kind === 'group' ? n.stats.songs : [id];
  }

  selectedSongs() {
    const out = [], seen = new Set();
    for (const n of this.flatAll()) {
      if (!this.sel.has(n.id)) continue;
      for (const p of this.songsUnder(n.id)) if (!seen.has(p) && this.rows[p]) { seen.add(p); out.push(p); }
    }
    return out;
  }

  flatAll() {
    // every node in list order (closed groups' children too), so a selection keeps its order
    const out = [];
    const walk = (ids) => { for (const id of ids) { const n = this.tree.nodes.get(id); out.push(n); if (n.kind === 'group') walk(n.children); } };
    walk(this.tree.roots);
    return out;
  }

  setSelection(ids, focus, notify = true) {
    this.sel = new Set(ids);
    this.focus = focus ?? (ids.length ? ids[ids.length - 1] : null);
    this.draw();
    if (notify) this.app.listSelected(this.focus, this.selectedSongs());
  }

  _idAt(e) {
    const tr = e.target.closest('.tr');
    return tr ? tr.dataset.id : null;
  }

  _colAt(e) {
    const cell = e.target.closest('.tr > div');
    if (!cell) return '';
    const m = /c-(\w+)/.exec(cell.className);
    return m ? m[1] : '';
  }

  _mouse(e) {
    if (e.button !== 0) return;
    this.root.focus({ preventScroll: true });
    const id = this._idAt(e);
    if (!id) return;
    const n = this.tree.nodes.get(id);
    const col = this._colAt(e);
    if (n.kind === 'group' && e.target.classList.contains('tw')) { this.setOpen(id, !this.isOpen(n)); return; }
    if (col === 'choice') {
      // the Choice cell cycles: a multi-selection, a group, or one song
      const songs = this.selectedSongs();
      if (this.sel.has(id) && songs.length > 1) { this.app.cycleMany(songs, 'sel:' + [...this.sel].sort().join(',')); return; }
      if (n.kind === 'group') { this.app.cycleMany(this.songsUnder(id), id); return; }
      this.app.cycle(id);
    }
    if (e.ctrlKey || e.metaKey) {
      const s = new Set(this.sel);
      s.has(id) ? s.delete(id) : s.add(id);
      this.anchor = id;
      this.setSelection([...s], id);
    } else if (e.shiftKey && this.anchor) {
      const ids = this.flat.map((x) => x.id);
      const a = ids.indexOf(this.anchor), b = ids.indexOf(id);
      if (a >= 0 && b >= 0) this.setSelection(ids.slice(Math.min(a, b), Math.max(a, b) + 1), id);
    } else {
      this.anchor = id;
      if (!(this.sel.size === 1 && this.sel.has(id))) this.setSelection([id], id);
      else { this.focus = id; this.app.listSelected(id, this.selectedSongs()); }
    }
  }

  _dbl(e) {
    const id = this._idAt(e);
    const n = id && this.tree.nodes.get(id);
    if (n && n.kind === 'group' && this._colAt(e) !== 'choice') this.setOpen(id, !this.isOpen(n));
  }

  _key(e) {
    if (e.target !== this.root) return;
    const ids = this.flat.map((x) => x.id);
    let i = ids.indexOf(this.focus);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      e.stopPropagation();
      i = Math.max(0, Math.min(ids.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)));
      if (!ids.length) return;
      if (e.shiftKey && this.anchor) {
        const a = ids.indexOf(this.anchor);
        this.setSelection(ids.slice(Math.min(a, i), Math.max(a, i) + 1), ids[i]);
      } else { this.anchor = ids[i]; this.setSelection([ids[i]], ids[i]); }
      this.scrollTo(i);
    } else if ((e.key === 'ArrowRight' || e.key === 'ArrowLeft') && i >= 0) {
      const n = this.flat[i];
      e.preventDefault();
      e.stopPropagation();
      if (n.kind === 'group' && this.isOpen(n) !== (e.key === 'ArrowRight')) this.setOpen(n.id, e.key === 'ArrowRight');
      else if (e.key === 'ArrowLeft' && n.parent) { this.setSelection([n.parent], n.parent); this.scrollTo(this.flat.findIndex((x) => x.id === n.parent)); }
    } else if (e.key === 'Delete') {
      e.preventDefault();
      e.stopPropagation();
      const songs = this.selectedSongs();
      if (songs.length) this.app.removeSongs(songs);
    } else if (e.key === 'a' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      e.stopPropagation();
      this.setSelection(this.tree.roots.slice(), this.focus);
    }
  }

  scrollTo(i) {
    const top = i * ROW_H, h = this.body.clientHeight;
    if (top < this.body.scrollTop) this.body.scrollTop = top;
    else if (top + ROW_H > this.body.scrollTop + h) this.body.scrollTop = top + ROW_H - h;
    this.draw();
  }

  /** Select a song (opening its groups) and show it. */
  selectSong(path, notify = true) {
    const n = this.tree.nodes.get(path);
    if (!n) return false;
    let p = n.parent;
    while (p) { this.open.set(p, true); p = this.tree.nodes.get(p).parent; }
    this.flat = M.flatten(this.tree, (g) => this.isOpen(g));
    this.inner.style.height = this.flat.length * ROW_H + 'px';
    this.anchor = path;
    this.setSelection([path], path, notify);
    this.scrollTo(this.flat.findIndex((x) => x.id === path));
    return true;
  }

  /** Up / W: previous song · Down / S: next song (into and across groups). */
  step(d) {
    const order = M.songOrder(this.tree);
    if (!order.length) return;
    let cur = this.viewing && order.includes(this.viewing) ? this.viewing : null;
    if (!cur) { const s = [...this.sel].find((x) => order.includes(x)); cur = s || null; }
    const i = cur !== null ? order.indexOf(cur) + d : (d > 0 ? 0 : order.length - 1);
    if (i < 0 || i >= order.length) return;
    this.selectSong(order[i]);
  }

  setViewing(path) {
    if (this.viewing === path) return;
    this.viewing = path;
    this.draw();
  }

  firstSong() { return M.songOrder(this.tree)[0] || null; }

  // ------------------------------------------------------------------ menus and popups
  _context(e) {
    e.preventDefault();
    const id = this._idAt(e);
    if (!id) return;
    if (!this.sel.has(id)) { this.anchor = id; this.setSelection([id], id); }
    const songs = this.selectedSongs();
    if (!songs.length) return;
    const n = this.tree.nodes.get(id);
    const what = `${songs.length} song${songs.length !== 1 ? 's' : ''}`;
    const target = this.s.output_mode === 'embed' ? 'embed into' : 'save .lrc for';
    const where = n.kind === 'group' && this.sel.size === 1 ? 'this ' + (this.s.list_view === 'folder' ? 'folder' : 'group') : what;
    const a = this.app;
    menu([
      n.kind === 'group' ? { label: 'Open folder', action: () => a.openLocation(id, n) }
        : { label: 'Open file location', action: () => a.openLocation(id, n) },
      '-',
      { label: `Save now — ${target} ${what}`, action: () => a.applyPaths(songs) },
      '-',
      { label: 'Use Local lyrics', action: () => a.setChoice(songs, 'local') },
      { label: 'Use Online lyrics', action: () => a.setChoice(songs, 'online') },
      { label: "Don't change", action: () => a.setChoice(songs, 'keep') },
      { label: 'Reset to default', action: () => a.resetChoice(songs) },
      { label: `⚡ Align on the GPU (${what})`, sub: [
        { label: 'Lines', action: () => a.alignCommand('songs', songs, false) },
        { label: 'Lines and words (word-synced)', action: () => a.alignCommand('songs', songs, true) },
      ] },
      { label: `Clear cached data (${what})…`, action: () => a.clearCache(songs) },
      '-',
      { label: `Restore original lyrics (${what})…`, action: () => a.restorePaths(songs) },
      '-',
      { label: `Remove ${where} from the list`, action: () => a.removeSongs(songs) },
    ], e.clientX, e.clientY);
  }

  groupFolder(id, n) {
    if (n.kind === 'group' && n.folder && this.s.list_view === 'folder') return n.folder;
    return M.commonFolder(this.songsUnder(id));
  }

  columnsMenu(x, y) {
    const shown = new Set(this.columns().map(([c]) => c));
    const items = M.COLUMNS.map(([c, , label]) => ({ label: label.replace(' ↻', ''), checked: shown.has(c), action: () => {
      const next = shown.has(c) ? [...shown].filter((x2) => x2 !== c) : [...shown, c];
      this.app.setSetting({ list_columns: M.COLUMNS.map(([k]) => k).filter((k) => next.includes(k)) });
    } }));
    items.push('-', { label: 'Show all columns', action: () => this.app.setSetting({ list_columns: M.COLUMNS.map(([k]) => k) }) });
    menu(items, x, y);
  }

  scorePopup(col, anchor) {
    if (this._popup && this._popup.isConnected) { this._popup.close(); return; }
    const label = col === 'match' ? 'Match' : 'Fit';
    const levelName = col === 'match' ? 'High confidence' : 'Good fit';
    const lo = col === 'match' ? 50 : 0;
    const minKey = col === 'match' ? 'list_min_match' : 'list_min_fit';
    const lvlKey = col === 'match' ? 'confident_threshold' : 'confident_fit';
    const slider = (caption, start, min, key, tip) => {
      const cap = el('div');
      const inp = el('input', { type: 'range', min, max: 100, step: 5, value: M.snap5(start, min), style: { width: '260px' } });
      const show = () => { cap.textContent = caption(Number(inp.value)); };
      show();
      inp.addEventListener('input', show);
      inp.addEventListener('change', () => this.app.setSetting({ [key]: Number(inp.value) }));
      hint(inp, tip);
      return [el('div', { class: 'col' }, cap, inp), inp];
    };
    const [a, fin] = slider((v) => (v ? `Show ${label} ≥ ${v}%` : `Show every ${label} score`), this.s[minKey] || 0, 0, minKey,
      `Hide songs whose ${label} score is below this (steps of 5%). Songs without a score stay listed.`);
    const [b] = slider((v) => `${levelName} at ≥ ${v}%`, this.s[lvlKey], lo, lvlKey,
      `The ${label} score that counts as ${levelName}: Apply confident, Hide low confidence and the group counts use it. ` +
      'Same setting as on the Find lyrics tab.');
    const row = el('div', { class: 'row', style: { justifyContent: 'space-between', marginTop: '8px' } },
      el('button', { class: 'small', text: 'Clear', hint: `Show every ${label} score again.`,
        onclick: () => { fin.value = 0; this.app.setSetting({ [minKey]: 0 }); this._popup.close(); } }),
      el('button', { class: 'small', text: 'Done', onclick: () => this._popup.close() }));
    this._popup = popup(el('div', { class: 'col', style: { gap: '10px' } }, a, b, row), anchor);
    fin.focus();
  }
}
