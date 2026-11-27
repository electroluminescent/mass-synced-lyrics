// Review & player: transport, the overview and scrolling timelines (Current, Found, Aligned, Edited), checkpoints,
// timing controls, Auto-align, the lyric panes and the choice of what to write. The backend keeps the logic
// (review_session.py); this draws its state, plays the audio and turns mouse and keys into its actions.
import { AudioPlayer } from './audio.js';
import { $, alertBox, debounce, el, fmtClock, fmtTime, fmtTs, hideTip, hint, menu, promptBox, showTip } from './ui.js';

const GPU = '⚡', HAND = '✎';
const BG = '#1d1f24', WAVE = '#5b8def', WAVE_VOC = '#c58cff', PLAYHEAD = '#ff4d4f', CP_COLOR = '#f5c542';
const SEL = '#ffffff', GHOST = '#bcd8ff', RULER_FG = '#9aa0a6', RULER_TICK = '#5f6368';
const ON = { cur: '#ffd27a', new: '#8ff0c0', aligned: '#d58cff', edit: '#bcd8ff' };
const LABEL_COLOR = { cur: '#a7782a', new: '#2f8a5f', aligned: '#d58cff', edit: '#7db7ff' };
const LANES = ['cur', 'new', 'aligned', 'edit'];
const LANE_LABEL = { cur: 'Current', new: 'Found', aligned: GPU + ' Aligned', edit: HAND + ' Edited' };
const OV = { lanes: { cur: 44, new: 56, aligned: 68, edit: 80 }, h: 10, ruler: 92, height: 108, mid: 20, amp: 18 };
const SC = { lanes: { cur: 64, new: 83, aligned: 102, edit: 121 }, h: 17, ruler: 140, height: 156, mid: 30, amp: 27 };
const WINDOW = 8.0;          // seconds shown in the scrolling view
const ANIM = 0.2;            // seconds for eased jumps
const EDGE_PX = 6;

const easeOut = (p) => 1 - (1 - Math.max(0, Math.min(1, p))) ** 3;
const bar = (b) => ({ start: b[0], end: b[1], text: b[2], key: b[3], row: b[4], fill: b[5], words: b[6] });

function currentIndex(lines, pos) {
  let best = -1;
  for (let i = 0; i < lines.length; i++) {
    const t = lines[i][0];
    if (t === null || t === undefined) return -1;
    if (t <= pos) best = i; else break;
  }
  return best;
}

function rulerStep(span, width, minPx = 70) {
  for (const s of [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]) if (span <= 0 || (s / span) * width >= minPx) return s;
  return 3600;
}

function svgTarget() {
  return '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="4.5" fill="none" stroke="#fff" stroke-width="2"/>' +
    '<path d="M8 0v4M8 12v4M0 8h4M12 8h4" stroke="#fff" stroke-width="2"/></svg>';
}
function svgCopy() {
  return '<svg viewBox="0 0 16 16"><rect x="2" y="1.5" width="8" height="10" fill="none" stroke="#fff"/>' +
    '<rect x="6" y="5" width="8" height="10" fill="#2b3442" stroke="#fff" stroke-width="2"/></svg>';
}

export class Player {
  constructor(app) {
    this.app = app;
    this.audio = new AudioPlayer();
    this.st = null;              // the backend's state of the open song
    this.lanes = { cur: [], new: [], aligned: [], edit: [] };
    this.peaks = null;
    this.duration = 0;
    this.token = 0;
    this.audioPath = '';
    this.vocals = '';
    this.useVocals = false;
    this.buffers = { mix: null, vocals: null };
    this.sel = { lane: null, keys: new Set() };
    this.cpRange = null;
    this.cpCycle = -1;
    this.disp = 0;
    this.lastActual = null;
    this.lastT = 0;
    this.jump = null;
    this.nowIdx = { cur: -1, new: -1 };
    this.drag = null;
    this.ovCache = null;
    this.audioNote = '';
    this.build();
    requestAnimationFrame(() => this.tick());
  }

  get s() { return this.app.settings; }
  call(name, ...args) { return this.app.rv(name, ...args); }
  pos() { return this.audio.position(); }
  dur() { return this.duration || (this.st && this.st.duration) || 0; }

  // ================================================================== layout
  build() {
    const B = (text, onclick, h, cls = '') => el('button', { class: cls, text, onclick, hint: h });
    this.root = el('div', { class: 'right' });
    this.vTitle = el('div', { class: 'ptitle grow', text: 'Select a song on the left (or Open file…).' });
    this.vOverride = el('div', { class: 'caption', style: { flex: '0 1 45%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textAlign: 'right' } });
    const head = el('div', { class: 'row' }, this.vTitle, this.vOverride);

    this.btnPlay = B('▶ Play', () => this.togglePlay(), 'Play / pause (Space).\nSpace: play / pause · keys 1–9: play checkpoint N ' +
      '(an unset number is set at the needle) · 0: next checkpoint by number · ` or ~: next checkpoint in the song after the ' +
      'needle · Up/Down or W/S: previous / next song\nClick a waveform to jump, drag it to scrub · Ctrl+click the overview ' +
      'to add or remove a checkpoint', 'accent');
    this.btnPlay.style.width = '96px';
    this.vTime = el('span', { class: 'muted', style: { fontVariantNumeric: 'tabular-nums', minWidth: '130px' }, text: '0:00.0 / 0:00.0' });
    this.vol = el('input', { type: 'range', min: 0, max: 100, step: 1, value: this.s.playback_volume ?? 80, style: { width: '110px' },
      hint: 'Playback volume (logarithmic). Mouse wheel: ±2 %.' });
    this.volTxt = el('span', { class: 'muted', style: { width: '44px', cursor: 'text' }, hint: 'Click to type a level.' });
    this.vol.addEventListener('input', () => this.setVolume(this.vol.value, false));
    this.vol.addEventListener('change', () => this.setVolume(this.vol.value, true));
    this.vol.addEventListener('wheel', (e) => { e.preventDefault(); this.setVolume(Number(this.vol.value) + (e.deltaY < 0 ? 2 : -2), true); });
    this.volTxt.addEventListener('click', () => this.editVolume());
    this.btnMix = B('Mix', () => this.setSource(false), 'Play the song as it is.', 'small');
    this.btnVoc = B('🎤 Vocals', () => this.setSource(true), '', 'small');
    hint(this.btnVoc, () => (this.vocals ? 'Play only the vocals (isolated by the GPU aligner): easier to hear where each ' +
      'line and word starts. The timing is the same as the song.' : 'Vocals only: available once this song has been ' +
      'aligned on the GPU (Auto-align, or Align on the GPU). The aligner keeps the isolated vocals for the player.'));
    const transport = el('div', { class: 'row' }, this.btnPlay,
      B('−5s', () => this.nudge(-5), 'Back 5 seconds.', 'small'), B('+5s', () => this.nudge(5), 'Forward 5 seconds.', 'small'),
      this.vTime, el('span', { text: '🔊' }), this.vol, this.volTxt,
      el('div', { class: 'row', style: { gap: '0', marginLeft: '6px' } }, this.btnMix, this.btnVoc));
    this.vAudioNote = el('div', { class: 'caption' });

    // overview + lane labels
    this.ovLbl = el('canvas', { class: 'lbl' });
    this.ov = el('canvas', { class: 'main' });
    const ovWrap = el('div', { class: 'cv' }, this.ov);
    const ovRow = el('div', { class: 'timeline' }, this.ovLbl, ovWrap);
    this.cpbar = el('div', { id: 'cpbar' });
    this.cpLen = el('input', { type: 'number', min: 0.2, max: 15, step: 0.1, value: this.s.checkpoint_seconds ?? 1,
      style: { width: '64px' }, hint: 'How many seconds before and after a checkpoint to play.' });
    this.cpLen.addEventListener('change', () => this.app.setSetting({ checkpoint_seconds: Number(this.cpLen.value) || 1 }));
    this.cpLoop = el('input', { type: 'checkbox' });
    this.cpLoop.checked = !!this.s.loop_checkpoints;
    this.cpLoop.addEventListener('change', () => this.app.setSetting({ loop_checkpoints: this.cpLoop.checked }));
    const cpRow = el('div', { class: 'row' }, el('span', { text: 'Checkpoints: play ±' }), this.cpLen, el('span', { text: 's' }),
      el('label', { class: 'check', hint: 'Repeat a checkpoint until you stop it.' }, this.cpLoop, 'Loop'),
      B('Reset points', () => this.call('default_checkpoints'), 'Back to the default checkpoints: the first and last lyric line.', 'small'));

    // scrolling view
    this.scLbl = el('canvas', { class: 'lbl' });
    this.sc = el('canvas', { class: 'main' });
    this.tools = el('div', { id: 'tools' });
    const tTarget = el('button', { hint: () => `Move the next lyric here: the first lyric after the needle starts at ${fmtTs(this.pos())}.\n` +
      'Shift+click: the next word in the line being sung starts here.' });
    tTarget.innerHTML = svgTarget();
    tTarget.addEventListener('click', (e) => (e.shiftKey ? this.call('next_word_to_needle', this.pos()) : this.call('next_line_to_needle', this.pos())));
    const tCopy = el('button', { hint: () => `Copy the needle's time (${fmtTs(this.pos())}) to paste into a lyric's timestamp (click it, Ctrl+V).` });
    tCopy.innerHTML = svgCopy();
    tCopy.addEventListener('click', () => { const t = fmtTs(this.pos()); window.api.copy(t); this.call('note', `Copied ${t}: click a timestamp and paste it (Ctrl+V).`); });
    this.tools.append(tTarget, tCopy);
    const scWrap = el('div', { class: 'cv' }, this.sc, this.tools);
    const scRow = el('div', { class: 'timeline' }, this.scLbl, scWrap);

    // timing card
    this.inOffset = el('input', { type: 'number', step: 50, value: 0, hint: 'Shift all new lyric times (milliseconds; + is later). Pinned and edited lines stay put.' });
    this.inStretch = el('input', { type: 'number', step: 0.05, value: 100, hint: 'Stretch the new lyric timing (100 % = unchanged).' });
    const timing = debounce(() => this.call('set_timing', Number(this.inOffset.value) || 0, Number(this.inStretch.value) || 100), 150);
    this.inOffset.addEventListener('input', timing);
    this.inStretch.addEventListener('input', timing);
    this.vAnchors = el('span', { class: 'muted' });
    this.btnInterp = B('Interpolate', async () => { const m = await this.call('toggle_interpolate'); if (m) alertBox(m); },
      "Re-time every line you haven't pinned or edited by stretching the original timing between your pins. Updates live as you add pins; click again to turn off.");
    this.swRipple = el('input', { type: 'checkbox' });
    this.swRipple.addEventListener('change', () => this.app.setSetting({ ripple: this.swRipple.checked }));
    this.vPins = el('span', { class: 'muted' });
    this.btnFromAligned = B('⇣ Aligned → Edited', () => this.call('transfer_all', 'aligned'),
      'Bring every lyric down from the Aligned timeline into Edited (the GPU\'s timing). Times you set yourself stay. To bring only some: select them in the Aligned timeline (click, Ctrl+click, or drag along it) and drag them down onto Edited.');
    this.btnAI = B(GPU + ' Auto-align (GPU)', () => this.autoAlign(), 'Locate these lyrics in this song\'s vocals on the NVIDIA GPU (⚡). If enough lyric words are heard being sung (Aligned lyrics must match, Find lyrics tab), the lines it placed get those times and show in the Edited lane (purple, brighter = more sure); the others follow. The result is the Aligned timeline; ⇣ Found → Edited goes back to the found timing.');
    this.btnAIMenu = B('▾', (e) => {
      const r = this.btnAIMenu.getBoundingClientRect();
      menu([{ label: GPU + ' Auto-align lines', action: () => this.autoAlign(false) },
        { label: GPU + ' Auto-align lines and words (word-synced)', action: () => this.autoAlign(true) }], r.left, r.bottom + 2);
    }, 'Auto-align to the line, or to the line and every word (word-synced lyrics: less accurate, from the GPU only — review the whole song before saving).');
    this.btnAIMenu.style.width = '28px';
    this.btnAIMenu.style.padding = '0';
    this.vAINote = el('div', { class: 'caption' });
    const card = el('div', { class: 'card timing' },
      el('div', { class: 'row' }, el('span', { text: 'Offset (ms)' }), this.inOffset, el('span', { text: 'Stretch (%)' }), this.inStretch,
        B('Stretch to song length', () => this.stretchToLength(), "Scale the timing by this song's length ÷ the length of the version the lyrics were made for (from LRCLIB or the [length:] tag; asks if unknown).", 'small'),
        this.vAnchors),
      el('div', { class: 'row' },
        B(HAND + ' Line to playhead', async () => { const m = await this.call('align_line', this.st ? this.st.sel_line : null, this.pos()); if (m) alertBox(m); },
          "Align the selected line to the playhead: click a line in Edited, play to where it's actually sung, then press. One aligned line sets the offset, two set offset and stretch.", 'small'),
        this.btnInterp,
        B('Clear pins', () => this.call('clear_pins'), 'Remove all pins (edited times stay).', 'small'),
        el('label', { class: 'switch', hint: 'When a time is edited (by you or the aligner), move the lines around it too: lines between two pinned or edited lines are stretched to fit, and lines before the first / after the last move by the same amount, squeezed so they stay within the song.' }, this.swRipple, 'Ripple changes'),
        this.vPins),
      el('div', { class: 'row' },
        B('⇣ Found → Edited', () => this.call('transfer_all', 'new'), 'Start Edited over from the Found timeline: clears offset, stretch, pins, edited times and word times. To bring only some lyrics: select them in Found and drag them down onto Edited.', 'small'),
        this.btnFromAligned, el('div', { class: 'row', style: { gap: '0' } }, this.btnAI, this.btnAIMenu)),
      this.vAINote);
    this.btnInterp.classList.add('small');
    this.btnFromAligned.classList.add('small');

    // lyric panes
    this.curPane = el('div', { class: 'card pane' });
    this.curLabel = el('h2', { class: 'grow', text: 'Current (embedded)', style: { margin: 0 } });
    this.curList = el('div', { class: 'lyrics' });
    this.curPane.append(el('div', { class: 'row' }, this.curLabel,
      B('◂ Hide', () => this.app.setSetting({ show_current: false }), 'Hide the current lyrics to give the new ones the whole width.', 'small')), this.curList);
    this.newPane = el('div', { class: 'card pane' });
    this.btnShowCur = B('▸ Current', () => this.app.setSetting({ show_current: true }), "Show the song's current lyrics beside the new ones.", 'small');
    this.cand = el('select', { class: 'grow', hint: 'Lyrics to compare and choose from: what Find found, sidecars, collection, LRCLIB, the current lyrics (to re-time), the originals, or none.' });
    this.cand.addEventListener('change', () => this.call('select_candidate', Number(this.cand.value)));
    this.vFit = el('div', { class: 'caption', hint: 'How well these lyrics fit this recording (length, lines past the end, spread over the song, lines starting on silence).' });
    this.newList = el('div', { class: 'lyrics' });
    this.choice = {};
    const choiceRow = el('div', { class: 'choice' });
    for (const [k, h] of [['keep', "Leave this song's lyrics as they are (Apply skips it, later runs too)."],
      ['found', 'Write the lyrics as found, with their found timing (the Found timeline). The % is how well they match the song.'],
      ['aligned', "Write the lyrics with the GPU's timing (the Aligned timeline). The % is how many of the lyric's words were heard being sung."],
      ['edited', 'Write the Edited timeline: everything you moved, stretched, typed or brought down from Found or Aligned (word times too). Chosen by itself once you change it.']]) {
      this.choice[k] = B(k, () => this.call('use_level', k), h);
      choiceRow.append(this.choice[k]);
    }
    this.newPane.append(el('div', { class: 'row' }, this.btnShowCur, el('h2', { text: HAND + ' Edited', style: { margin: 0 } }), this.cand),
      this.vFit, this.newList, choiceRow);
    this.panes = el('div', { class: 'panes' }, this.curPane, this.newPane);
    this.vChoiceNote = el('span', { class: 'muted grow' });
    const act = el('div', { class: 'row' },
      B('Reset to default (clear my choice)', () => this.call('clear'), 'Forget your choice for this song; the default (what Find picked) applies again.', 'small'),
      this.vChoiceNote,
      B('Search LRCLIB', async () => { const m = await this.call('search_lrclib'); if (m) alertBox(m); }, "Search LRCLIB for more versions of this song's lyrics.", 'small'));

    this.root.append(head, transport, this.vAudioNote, ovRow, this.cpbar, cpRow, scRow, card, this.panes, act);
    this.setVolume(this.vol.value, false);
    this.showCurrent(!!this.s.show_current);

    for (const c of [this.ov, this.sc]) {
      c.addEventListener('mousedown', (e) => this.press(c, e));
      c.addEventListener('mousemove', (e) => { if (!this.drag) this.hover(c, e); });
      c.addEventListener('mouseleave', () => { if (!this.drag) hideTip(); });
      c.addEventListener('contextmenu', (e) => this.canvasMenu(c, e));
    }
    window.addEventListener('mousemove', (e) => { if (this.drag) this.motion(e); });
    window.addEventListener('mouseup', (e) => { if (this.drag) this.release(e); });
    for (const lst of [this.curList, this.newList]) {
      lst.addEventListener('wheel', () => { lst._user = performance.now(); });
      lst.addEventListener('scroll', () => this.paneScrolled(lst));
    }
    new ResizeObserver(() => { this.ovCache = null; this.sizeCanvases(); this.drawCpBar(); }).observe(this.root);
  }

  showCurrent(on) {
    this.curPane.classList.toggle('hidden', !on);
    this.btnShowCur.classList.toggle('hidden', on);
  }

  settingsChanged() {
    this.swRipple.checked = !!this.s.ripple;
    this.cpLoop.checked = !!this.s.loop_checkpoints;
    if (document.activeElement !== this.cpLen) this.cpLen.value = this.s.checkpoint_seconds;
    this.showCurrent(!!this.s.show_current);
  }

  sizeCanvases() {
    const dpr = window.devicePixelRatio || 1;
    for (const [c, h] of [[this.ov, OV.height], [this.ovLbl, OV.height], [this.sc, SC.height], [this.scLbl, SC.height]]) {
      const w = c.classList.contains('lbl') ? 70 : Math.max(2, c.parentElement.clientWidth);
      c.style.height = h + 'px';
      if (c.classList.contains('lbl')) c.style.width = '70px';
      if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
        c.width = Math.round(w * dpr);
        c.height = Math.round(h * dpr);
      }
      c._w = w;
      c._h = h;
    }
    this.drawLabels();
  }

  // ================================================================== state from the backend
  setState(st) {
    const prevPath = this.st ? this.st.path : '';
    this.st = st;
    this.lanes = Object.fromEntries(LANES.map((k) => [k, (st.lanes[k] || []).map(bar)]));
    if (st.path !== prevPath) { this.sel = { lane: null, keys: new Set() }; this.cpRange = null; this.cpCycle = -1; }
    if (this.sel.lane) {
      const have = new Set(this.lanes[this.sel.lane].map((b) => b.key));
      for (const k of [...this.sel.keys]) if (!have.has(k)) this.sel.keys.delete(k);
    }
    this.vTitle.textContent = st.title;
    this.vOverride.textContent = st.override;
    this.vOverride.title = st.override;
    this.audioNote = st.audio_note || this.audioNote;
    this.vAudioNote.textContent = st.audio_note || '';
    this.curLabel.textContent = st.cur_label;
    // candidates
    const opts = st.cands.length ? st.cands : ['(no candidates — try Search LRCLIB)'];
    if (this.cand._sig !== opts.join('\n')) {
      this.cand.replaceChildren(...opts.map((l, i) => el('option', { value: i, text: l })));
      this.cand._sig = opts.join('\n');
    }
    this.cand.value = Math.max(0, st.cand);
    this.vFit.textContent = st.fit_note;
    this.vFit.classList.toggle('hidden', !st.fit_note);
    if (document.activeElement !== this.inOffset) this.inOffset.value = st.offset_ms;
    if (document.activeElement !== this.inStretch) this.inStretch.value = st.stretch_pct;
    this.vAnchors.textContent = st.anchors_note;
    this.vPins.textContent = st.pins_note;
    this.btnInterp.textContent = st.interp.text;
    this.btnInterp.classList.toggle('on', st.interp.on);
    this.swRipple.checked = st.ripple;
    this.btnFromAligned.disabled = !st.has_aligned;
    this.btnAI.disabled = st.ai_busy || !st.path;
    this.btnAIMenu.disabled = st.ai_busy || !st.path;
    this.btnAI.textContent = st.ai_busy ? (st.ai_pct !== null && st.ai_pct !== undefined ? `${GPU} Aligning… ${Math.round(st.ai_pct)}%` : `${GPU} Aligning…`) : GPU + ' Auto-align (GPU)';
    this.vAINote.textContent = st.ai_note;
    this.vAINote.classList.toggle('hidden', !st.ai_note);
    for (const [k, b] of Object.entries(this.choice)) {
      const c = st.choice.buttons[k];
      b.textContent = (c.on ? '✓  ' : '') + c.label;
      b.classList.toggle('on', c.on);
      b.disabled = !c.avail;
    }
    this.vChoiceNote.textContent = st.choice.note;
    this.renderLines(this.curList, st.cur_lines.map(([t, x]) => [t, x, null, '', false]), st.cur_empty, 'cur');
    this.renderLines(this.newList, st.new_lines, st.new_empty, 'new');
    this.ovCache = null;
    this.drawLabels();
    this.drawCpBar();
    this.app.list.setViewing(st.path);
  }

  setAudio(ev) {
    if (ev.token !== (this.st ? this.st.token : ev.token) && this.st && ev.path !== this.audioPath) { /* newer song coming */ }
    this.token = ev.token;
    this.duration = ev.duration || 0;
    this.peaks = ev.peaks ? Uint8Array.from(atob(ev.peaks), (c) => c.charCodeAt(0)) : null;
    this.peakRate = ev.rate || 100;
    this.ovCache = null;
    this.vocals = ev.vocals || '';
    this.buffers = { mix: null, vocals: null };
    this.audioPath = ev.path;
    this.audio.unload(this.duration);
    this.vAudioNote.textContent = ev.error || '';
    this.cpRange = null;
    this.updateSourceButtons();
    if (!ev.error) this.loadBuffer(this.useVocals && this.vocals ? 'vocals' : 'mix');
  }

  setVocals(ev) {
    if (ev.path !== this.audioPath) return;
    this.vocals = ev.vocals || '';
    this.buffers.vocals = null;
    if (!this.vocals && this.useVocals) this.setSource(false);
    this.updateSourceButtons();
  }

  updateSourceButtons() {
    const voc = this.useVocals && !!this.vocals;
    this.btnMix.classList.toggle('on', !voc);
    this.btnVoc.classList.toggle('on', voc);
    this.btnVoc.disabled = !this.vocals;
  }

  async loadBuffer(which) {
    const path = this.audioPath;
    const token = this.token;
    const file = which === 'vocals' ? this.vocals : path;
    try {
      let data;
      try {
        data = await window.api.readAudio(file);
        if (token !== this.token) return;
        await this.audio.load(data);
      } catch (e) {
        if (which === 'vocals') throw e;
        // a format the window can't play itself: the backend hands over a WAV of it
        const wav = await window.api.call('rv.audio_wav');
        if (token !== this.token || !wav) throw e;
        data = await window.api.readAudio(wav);
        await this.audio.load(data);
      }
      if (token !== this.token) return;
      this.buffers[which] = this.audio.buffer;
      this.duration = this.audio.duration || this.duration;
    } catch (e) {
      if (token === this.token) this.vAudioNote.textContent = `Could not play ${which === 'vocals' ? 'the vocals' : 'this song'}: ${e.message || e}`;
    }
  }

  async setSource(vocals) {
    if (vocals && !this.vocals) return;
    if (vocals === this.useVocals && this.audio.ready) return;
    this.useVocals = vocals;
    this.updateSourceButtons();
    const which = vocals ? 'vocals' : 'mix';
    const pos = this.pos(), playing = this.audio.playing;
    this.audio.pause();
    if (this.buffers[which]) {
      this.audio.buffer = this.buffers[which];
    } else {
      await this.loadBuffer(which);
    }
    this.audio.seek(pos);
    if (playing) this.audio.play();
  }

  // ================================================================== lyric panes
  renderLines(list, lines, empty, which) {
    const sig = JSON.stringify([lines, empty, which === 'new' && this.st ? this.st.sel_line : null,
      which === 'new' && this.st ? this.st.word_rows : null]);
    if (list._sig === sig) return;
    list._sig = sig;
    if (list._editing) return;                        // the inline editor is open: redraw after it
    const top = list.scrollTop;
    const frag = document.createDocumentFragment();
    if (!lines.length) frag.append(el('div', { class: 'muted', text: empty }));
    const words = (this.st && this.st.word_rows) || {};
    lines.forEach(([t, text, pin, tag, lyric], i) => {
      const ln = el('div', { class: 'ln' + (which === 'new' && this.st && this.st.sel_line === i ? ' pick' : '') });
      ln.dataset.i = i;
      if (pin) {
        const p = el('span', { class: 'pin ' + pin, text: pin === 'pinned' ? '📌' : pin === 'marker' ? '' : '○' });
        if (pin !== 'marker') {
          hint(p, pin === 'pinned' ? 'Pinned: this line keeps its time. Click to unpin.' : 'Click to pin this line at its time.');
          p.addEventListener('click', (e) => { e.stopPropagation(); this.call('toggle_pin', i); });
        }
        ln.append(p);
      }
      if (t !== null && t !== undefined) {
        const ts = el('span', { class: 'ts ' + (tag || ''), text: fmtTs(t) });
        if (which === 'new' && this.st && this.st.synced) {
          ts.addEventListener('click', (e) => { e.stopPropagation(); this.editTime(ln, ts, i, t); });
          hint(ts, 'Click to type a time (m:ss.xx); empty resets it. Right-click the line for more.');
        }
        ln.append(ts);
      }
      ln.append(el('span', { class: 'tx', text: text + (which === 'new' && words[i] ? '  ·w' : '') }));
      ln.addEventListener('click', () => {
        if (which === 'new') this.call('select_line', i);
        if (t !== null && t !== undefined) { this.cpRange = null; this.audio.seek(t); }
      });
      if (which === 'new') ln.addEventListener('contextmenu', (e) => this.lineMenu(e, i, t, lyric));
      frag.append(ln);
    });
    list.replaceChildren(frag);
    list.scrollTop = top;
    this.nowIdx[which] = -1;
  }

  editTime(ln, ts, i, t) {
    const list = this.newList;
    const inp = el('input', { type: 'text', value: fmtTs(t) });
    ts.replaceWith(inp);
    list._editing = true;
    inp.focus();
    inp.select();
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      list._editing = false;
      if (save) {
        const err = await this.call('set_line_time_text', i, inp.value);
        if (err) alertBox(err);
      }
      list._sig = null;
      if (this.st) this.renderLines(list, this.st.new_lines, this.st.new_empty, 'new');
    };
    inp.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Enter') finish(true);
      if (e.key === 'Escape') finish(false);
    });
    inp.addEventListener('blur', () => finish(true));      // clicking anywhere else keeps what was typed
  }

  lineMenu(e, i, t, lyric) {
    e.preventDefault();
    if (t === null || t === undefined || !this.st || !this.st.synced) return;
    const row = this.st.new_lines[i];
    const pinned = row[2] === 'pinned';
    const wordRow = this.lanes.edit.find((b) => b.row === i);
    menu([
      { label: 'Edit time…', action: () => { const ln = this.newList.querySelector(`.ln[data-i="${i}"] .ts`); if (ln) ln.click(); } },
      { label: `Set to playhead (${fmtTime(this.pos())})`, action: () => this.call('set_line_time', i, this.pos()) },
      { label: pinned ? 'Unpin' : 'Pin at this time', action: () => this.call('toggle_pin', i), disabled: row[2] === 'marker' },
      '-',
      { label: 'Reset this timestamp', action: () => this.call('set_line_time', i, null), disabled: !(pinned || row[3] === 'edited' || row[3] === 'ai_edited') },
      '-',
      { label: "Time this line's words", action: () => this.timeWords(i), disabled: !lyric,
        hint: 'Give every word of this line a time of its own (shared out by length), then drag the word marks in the Edited timeline below the waveform.' },
      { label: 'Clear my word times on this line', action: () => this.call('clear_word_times', i),
        disabled: !(this.st.word_rows && this.st.word_rows[i]) },
      '-',
      { label: 'Insert a line before…', action: () => this.insertLine(i, false), disabled: !lyric },
      { label: 'Insert a line after…', action: () => this.insertLine(i, true), disabled: !lyric },
      { label: 'Delete this line', action: () => this.call('delete_line', i), disabled: !lyric },
    ].filter(Boolean), e.clientX, e.clientY);
    void wordRow;
  }

  async timeWords(i) {
    const m = await this.call('time_words', i);
    if (m) alertBox(m);
  }

  async insertLine(i, after) {
    const text = await promptBox('Lyric line to insert (leave empty for a gap):', '');
    if (text !== null) this.call('insert_line', i, after, text);
  }

  paneScrolled(lst) {
    // you scrolled one pane: bring the other to the same point in the song
    if (!lst._user || performance.now() - lst._user > 400 || !this.st) return;
    const other = lst === this.curList ? this.newList : this.curList;
    const lines = lst === this.curList ? this.st.cur_lines : this.st.new_lines;
    const olines = other === this.curList ? this.st.cur_lines : this.st.new_lines;
    if (!lines.length || !olines.length || other.classList.contains('hidden')) return;
    const first = [...lst.children].find((c) => c.offsetTop + c.offsetHeight > lst.scrollTop);
    const i = first ? Number(first.dataset.i) : 0;
    const t = lines[i] ? lines[i][0] : null;
    if (t === null || t === undefined) return;
    let j = 0;
    olines.forEach((l, k) => { if (l[0] !== null && l[0] <= t + 1e-6) j = k; });
    const target = other.querySelector(`.ln[data-i="${j}"]`);
    if (target) other.scrollTo({ top: target.offsetTop - other.offsetTop, behavior: 'smooth' });
  }

  highlight(list, which, lines, pos) {
    const i = currentIndex(lines, pos);
    if (i === this.nowIdx[which]) return;
    this.nowIdx[which] = i;
    for (const c of list.querySelectorAll('.ln.now')) c.classList.remove('now');
    if (i < 0) return;
    const ln = list.querySelector(`.ln[data-i="${i}"]`);
    if (!ln) return;
    ln.classList.add('now');
    if (list._user && performance.now() - list._user < 1500) return;      // you're scrolling it yourself
    const top = ln.offsetTop - list.offsetTop - list.clientHeight / 2 + ln.offsetHeight / 2;
    list.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
  }

  // ================================================================== playback
  togglePlay() {
    if (!this.audio.ready) return;
    this.cpRange = null;
    this.audio.toggle();
  }

  nudge(sec) {
    if (!this.audio.ready) return;
    this.cpRange = null;
    this.audio.seek(this.pos() + sec);
  }

  seek(t) {
    if (!this.audio.ready && !this.dur()) return;
    this.cpRange = null;
    this.audio.seek(t);
  }

  setVolume(v, save) {
    v = Math.max(0, Math.min(100, Math.round(Number(v) || 0)));
    this.vol.value = v;
    this.volTxt.textContent = `${v}%`;
    this.audio.setVolume(v);
    if (save) this.app.setSetting({ playback_volume: v });
  }

  editVolume() {
    const inp = el('input', { type: 'text', value: String(this.vol.value), style: { width: '44px', height: '24px' } });
    this.volTxt.replaceWith(inp);
    inp.focus();
    inp.select();
    let done = false;
    const close = (save) => {
      if (done) return;
      done = true;
      inp.replaceWith(this.volTxt);
      const v = parseFloat(inp.value.replace('%', ''));
      if (save && Number.isFinite(v)) this.setVolume(v, true);
    };
    inp.addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Enter') close(true); if (e.key === 'Escape') close(false); });
    inp.addEventListener('blur', () => close(true));
  }

  // ---------------------------------------------------------------- checkpoints
  checkpoints() { return (this.st && this.st.checkpoints) || []; }

  playCheckpoint(i) {
    if (!this.audio.ready || i < 0) return;
    const cps = this.checkpoints();
    if (i >= cps.length || cps[i] === null) { this.call('add_checkpoint', this.pos(), i); return; }   // an unset key
    this.cpCycle = i;
    const span = Math.max(0.1, Number(this.s.checkpoint_seconds) || 1);
    const start = Math.max(0, cps[i] - span), end = Math.min(this.dur(), cps[i] + span);
    this.audio.seek(start);
    this.cpRange = [start, end];
    this.audio.play();
  }

  nextCheckpoint() {
    const idx = this.checkpoints().map((t, i) => (t === null ? -1 : i)).filter((i) => i >= 0);
    if (idx.length) this.playCheckpoint(idx.find((i) => i > this.cpCycle) ?? idx[0]);
  }

  cycleCheckpoint() {
    if (!this.audio.ready) return;
    const cps = this.checkpoints();
    let pos = this.pos();
    if (this.cpRange && this.cpCycle >= 0 && cps[this.cpCycle] !== null && cps[this.cpCycle] !== undefined) pos = cps[this.cpCycle];
    const order = cps.map((t, i) => [t, i]).filter(([t]) => t !== null).sort((a, b) => a[0] - b[0]);
    if (!order.length) return;
    const nxt = order.find(([t]) => t > pos + 0.05) || order[0];
    this.playCheckpoint(nxt[1]);
  }

  drawCpBar() {
    this.cpbar.replaceChildren();
    const d = this.dur(), w = this.cpbar.clientWidth;
    if (!d || !w) return;
    this.checkpoints().forEach((t, i) => {
      if (t === null) return;
      const b = el('button', { text: i < 9 ? `▶${i + 1}` : '▶',
        hint: `Play checkpoint ${i + 1} at ${fmtTime(t)}` + (i < 9 ? ` (key ${i + 1})` : '') + '. Right-click to delete it.' });
      b.style.left = Math.min(Math.max((t / d) * w, 16), w - 16) + 'px';
      b.addEventListener('click', () => this.playCheckpoint(i));
      b.addEventListener('contextmenu', (e) => { e.preventDefault(); this.call('delete_checkpoint', i); });
      b.addEventListener('mousedown', (e) => e.preventDefault());
      this.cpbar.append(b);
    });
  }

  // ---------------------------------------------------------------- Auto-align, stretch
  async autoAlign(words) {
    if (words === undefined) words = !!this.s.sync_words;
    if (words && !(await this.app.warnWordSync())) return;
    if (!(await this.app.ensureAI())) return;
    const m = await this.call('auto_align', words);
    if (m) alertBox(m);
  }

  async stretchToLength() {
    let r = await this.call('stretch_to_length');
    if (r && r.ask) {
      const v = await promptBox(r.ask, '');
      if (v === null) return;
      r = await this.call('stretch_to_length', v);
    }
    if (r && r.info) alertBox(r.info);
  }

  // ================================================================== timelines: geometry
  ovX(t) { return (t / (this.dur() || 1)) * this.ov._w; }
  ovT(x) { return Math.max(0, Math.min(this.dur(), (x / Math.max(1, this.ov._w)) * this.dur())); }
  scPPS() { return this.sc._w / WINDOW; }
  scX(t) { return this.sc._w / 2 + (t - this.disp) * this.scPPS(); }
  scT(x) { return this.disp + (x - this.sc._w / 2) / this.scPPS(); }
  tAt(c, x) { return c === this.ov ? this.ovT(x) : this.scT(x); }
  xAt(c, t) { return c === this.ov ? this.ovX(t) : this.scX(t); }
  geo(c) { return c === this.ov ? OV : SC; }

  laneAt(c, y) {
    const g = this.geo(c);
    for (const k of ['new', 'aligned', 'edit']) {
      if (y >= g.lanes[k] - 1 && y <= g.lanes[k] + g.h + 1 && (k !== 'aligned' || this.lanes.aligned.length)) return k;
    }
    return null;
  }

  barAt(c, lane, x) {
    const t = this.tAt(c, x);
    return this.lanes[lane].find((b) => b.key && b.start <= t && t < b.end) || null;
  }

  edgeAt(x, y) {
    // (row, "start" | "end") on a stretch handle of a lyric in Edited (scrolling view only)
    if (!this.st || y < SC.lanes.edit || y > SC.lanes.edit + SC.h) return null;
    const rows = this.st.new_lines;
    let best = null;
    for (const b of this.lanes.edit) {
      if (b.row === null) continue;
      const nxt = b.row + 1;
      const gapAfter = nxt >= rows.length || !String(rows[nxt][1]).trim();
      for (const [edge, t] of [['end', b.end], ['start', b.start]]) {
        if (edge === 'end' && !gapAfter) continue;
        const d = Math.abs(x - this.scX(t));
        if (d <= EDGE_PX && (!best || d < best[0])) best = [d, b.row, edge];
      }
    }
    return best ? { row: best[1], edge: best[2] } : null;
  }

  wordAt(x, y) {
    // a word mark (the start of word 2, 3, …) in Edited: drag to move it
    if (y < SC.lanes.edit || y > SC.lanes.edit + SC.h) return null;
    let best = null;
    for (const b of this.lanes.edit) {
      if (b.row === null || !b.words || b.words.length < 2) continue;
      b.words.forEach(([ws, , w], j) => {
        if (!j) return;
        const d = Math.abs(x - this.scX(ws));
        if (d <= 5 && (!best || d < best.d)) best = { d, row: b.row, word: j, text: w, bar: b };
      });
    }
    return best;
  }

  // ================================================================== timelines: mouse
  press(c, e) {
    if (e.button !== 0) return;
    hideTip();
    document.activeElement && document.activeElement.blur && document.activeElement.blur();
    const r = c.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    const ctrl = e.ctrlKey || e.metaKey;
    if (c === this.sc && this.audio.ready || c === this.sc && this.dur()) {
      const edge = this.edgeAt(x, y);
      if (edge) {
        this.audio.pause();
        this.call('resize_begin', edge.row, edge.edge);
        this.drag = { kind: 'resize', c, edge, last: 0 };
        return;
      }
      const wd = this.wordAt(x, y);
      if (wd) {
        this.audio.pause();
        this.drag = { kind: 'word', c, row: wd.row, word: wd.word, last: 0 };
        return;
      }
    }
    const lane = this.laneAt(c, y);
    if (lane) { this.lanePress(c, x, y, lane, ctrl); return; }
    if (c === this.ov) {
      if (ctrl) { this.ovCtrlClick(x); return; }
      this.drag = { kind: 'ovscrub', c, x, moved: false };
    } else {
      this.drag = { kind: 'scscrub', c, x, pos: this.pos(), moved: false };
    }
  }

  nearestCheckpoint(x) {
    const cps = this.checkpoints(), d = this.dur(), w = this.ov._w;
    if (!d) return null;
    let best = null;
    cps.forEach((t, i) => { if (t !== null) { const dx = Math.abs((t / d) * w - x); if (!best || dx < best[0]) best = [dx, i]; } });
    return best && best[0] <= Math.max(12, w * 0.01) ? best[1] : null;
  }

  ovCtrlClick(x) {
    const i = this.nearestCheckpoint(x);
    if (i !== null) { this.deleteCheckpoint(i); return; }
    this.call('add_checkpoint', Math.round(this.ovT(x) * 100) / 100);
  }

  deleteCheckpoint(i) {
    if (this.cpRange) { this.audio.pause(); this.cpRange = null; }
    this.cpCycle = -1;
    this.call('delete_checkpoint', i);
  }

  canvasMenu(c, e) {
    e.preventDefault();
    const r = c.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    if (c === this.ov) {
      const i = this.nearestCheckpoint(x);
      if (i !== null) this.deleteCheckpoint(i);              // right-click removes a checkpoint (never adds)
      return;
    }
    if (this.laneAt(c, y) !== 'edit' || !this.st || !this.st.synced) return;
    const b = this.barAt(c, 'edit', x);
    if (!b || b.row === null) return;
    menu([
      { label: `“${b.text.slice(0, 40)}”`, disabled: true },
      '-',
      { label: "Time this line's words", action: () => this.timeWords(b.row) },
      { label: 'Next word starts at the needle', action: () => this.call('next_word_to_needle', this.pos()), disabled: !(b.words && b.words.length) },
      { label: 'Clear my word times on this line', action: () => this.call('clear_word_times', b.row),
        disabled: !(this.st.word_rows && this.st.word_rows[b.row]) },
      '-',
      { label: `Line starts at the needle (${fmtTime(this.pos())})`, action: () => this.call('set_line_time', b.row, this.pos()) },
    ], e.clientX, e.clientY);
  }

  lanePress(c, x, y, lane, ctrl) {
    const b = this.barAt(c, lane, x);
    if (this.sel.lane !== lane) this.sel = { lane, keys: new Set() };
    const wasSelected = !!b && this.sel.keys.has(b.key);
    const before = new Set(this.sel.keys);
    if (b) {
      if (ctrl) { this.sel.keys.has(b.key) ? this.sel.keys.delete(b.key) : this.sel.keys.add(b.key); } else if (!this.sel.keys.has(b.key)) this.sel.keys = new Set([b.key]);
      if (lane === 'edit') this.audio.pause();
      this.drag = { kind: 'bar', c, lane, x, y, moved: false, ctrl, t0: this.tAt(c, x), bar: b, wasSelected, before, last: 0 };
    } else {
      const base = ctrl ? new Set(this.sel.keys) : new Set();
      if (!ctrl) this.sel.keys = new Set();
      this.drag = { kind: 'band', c, lane, x0: x, moved: false, base, ctrl };
    }
    this.ovCache = null;
  }

  motion(e) {
    const d = this.drag;
    const r = d.c.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    d.cx = x; d.cy = y;
    if (d.kind === 'ovscrub') {
      if (d.moved || Math.abs(x - d.x) > 4) { d.moved = true; this.cpRange = null; this.audio.seek(this.ovT(x)); }
      return;
    }
    if (d.kind === 'scscrub') {
      if (d.moved || Math.abs(x - d.x) > 3) {
        d.moved = true;
        this.cpRange = null;
        this.audio.seek(d.pos - ((x - d.x) / Math.max(1, this.sc._w)) * WINDOW);     // drag the waveform like tape
      }
      return;
    }
    if (d.kind === 'resize' || d.kind === 'word') {
      d.pending = Math.round(this.scT(x) * 100) / 100;
      this.throttle(d, () => (d.kind === 'resize' ? this.call('resize', d.pending) : this.call('set_word_time', d.row, d.word, d.pending)));
      return;
    }
    if (d.kind === 'band') {
      if (!d.moved && Math.abs(x - d.x0) <= 3) return;
      d.moved = true;
      const [t0, t1] = [this.tAt(d.c, d.x0), this.tAt(d.c, x)].sort((a, b) => a - b);
      this.sel.lane = d.lane;
      this.sel.keys = new Set([...d.base, ...this.lanes[d.lane].filter((b) => b.key && b.end > t0 && b.start < t1).map((b) => b.key)]);
      d.t0 = t0; d.t1 = t1;
      this.ovCache = null;
      return;
    }
    if (d.kind === 'bar') {
      if (!d.moved && Math.abs(x - d.x) <= 4 && Math.abs(y - d.y) <= 4) return;
      if (!d.moved) {
        const along = Math.abs(x - d.x) > Math.abs(y - d.y);
        if ((d.lane !== 'edit' && along) || (d.lane === 'edit' && !d.wasSelected)) {
          this.drag = { kind: 'band', c: d.c, lane: d.lane, x0: d.x, moved: false, ctrl: d.ctrl, base: d.ctrl ? d.before : new Set() };
          this.motion(e);
          return;
        }
        if (d.lane === 'edit') this.call('move_begin', [...this.sel.keys]);
      }
      d.moved = true;
      if (d.lane === 'edit') {
        d.pending = this.tAt(d.c, x) - d.t0;
        this.throttle(d, () => this.call('move', d.pending));
      }
    }
  }

  throttle(d, fn) {
    // at most one request in flight; the latest position is sent when it's back (live, ~30 a second)
    if (d.busy) { d.again = true; return; }
    d.busy = true;
    d.again = false;
    Promise.resolve(fn()).finally(() => {
      d.busy = false;
      if (d.again && this.drag === d) this.throttle(d, fn);
    });
  }

  release(e) {
    const d = this.drag;
    this.drag = null;
    const r = d.c.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    if (d.kind === 'ovscrub') { if (!d.moved) this.seek(this.ovT(x)); return; }
    if (d.kind === 'scscrub') {
      if (!d.moved) this.seek(this.pos() + ((x - this.sc._w / 2) / Math.max(1, this.sc._w)) * WINDOW);
      return;
    }
    if (d.kind === 'resize') { const t = Math.round(this.scT(x) * 100) / 100; this.whenIdle(d, () => this.call('resize_end', t)); return; }
    if (d.kind === 'word') { const t = Math.round(this.scT(x) * 100) / 100; this.whenIdle(d, () => this.call('set_word_time', d.row, d.word, t)); return; }
    if (d.kind === 'band') {
      if (!d.moved && !d.ctrl) { this.sel = { lane: null, keys: new Set() }; this.seek(this.tAt(d.c, x)); }
      this.ovCache = null;
      return;
    }
    if (d.kind === 'bar') {
      if (!d.moved) {
        if (!d.ctrl) { this.sel.keys = new Set([d.bar.key]); this.seek(d.bar.start); }
        this.ovCache = null;
        return;
      }
      if (d.lane === 'new' || d.lane === 'aligned') {
        const g = this.geo(d.c);
        if (y >= g.lanes.edit - 4 && y <= g.lanes.edit + g.h + 4) this.call('transfer', d.lane, [...this.sel.keys]);
        return;
      }
      const dt = this.tAt(d.c, x) - d.t0;
      this.whenIdle(d, () => this.call('move_end', dt));
    }
  }

  whenIdle(d, fn) {
    d.again = false;
    if (!d.busy) { fn(); return; }
    const wait = () => (d.busy ? setTimeout(wait, 10) : fn());
    wait();
  }

  hover(c, e) {
    if (!this.st || !this.dur()) return;
    const r = c.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    const t = this.tAt(c, x);
    if (t < 0 || t > this.dur()) { hideTip(); return; }
    const parts = [fmtTime(t)];
    for (const k of LANES) {
      const bars = this.lanes[k];
      if (!bars.length) continue;
      const b = bars.find((z) => z.start <= t && t < z.end);
      if (b) {
        const w = (b.words || []).find(([ws, we]) => ws <= t && t < we);
        const conf = k === 'aligned' && this.st.aligned_conf[b.key] !== undefined ? `  (${Math.round(this.st.aligned_conf[b.key] * 100)}% sure)` : '';
        parts.push(`${LANE_LABEL[k]}: ${b.text}` + (w ? `   [${w[2]}]` : '') + conf);
      } else parts.push(`${LANE_LABEL[k]}: —`);
    }
    const eb = this.lanes.edit.find((z) => z.start <= t && t < z.end);
    if (eb && eb.key) {
      if (this.st.ai_keys.includes(eb.key)) parts.push(`Edited line from the Aligned timeline (${GPU})`);
      else if (this.st.edited.includes(eb.key)) parts.push(`Edited line: your time (${HAND})`);
      else if (this.st.has_edits) parts.push('Edited line: moved along with the others');
    }
    const lane = this.laneAt(c, y);
    let cursor = lane ? 'pointer' : (c === this.ov ? 'pointer' : 'ew-resize');
    if (c === this.sc) {
      const edge = this.edgeAt(x, y);
      const wd = !edge && this.wordAt(x, y);
      if (edge) { cursor = 'col-resize'; parts.push('Drag to stretch this lyric' + (edge.edge === 'end' ? ' (its end: a gap follows)' : ' (its start)')); }
      if (wd) { cursor = 'col-resize'; parts.push(`Drag to move where “${wd.text}” starts`); }
    }
    c.style.cursor = cursor;
    if (lane === 'new' || lane === 'aligned') parts.push('Click: select · Ctrl+click: add/remove · drag along the timeline: select a range · drag selected lyrics down onto Edited: use their times');
    else if (lane === 'edit') parts.push('Click: select · Ctrl+click: add/remove · drag selected lyrics left/right to move them · drag a lyric\'s start (or its end before a gap) to stretch it · drag a word mark to move that word · right-click: word timing');
    else parts.push('Click: jump · drag: scrub' + (c === this.ov ? ' · Ctrl+click: add/remove checkpoint' : ' · buttons above the needle: next lyric (Shift: next word) here, copy the time'));
    showTip(parts.join('\n'), e.clientX, e.clientY);
  }

  // ================================================================== drawing
  displayPos(actual) {
    // jumps (seek, scrub, checkpoint) glide over ANIM seconds
    const now = performance.now() / 1000;
    if (this.lastActual !== null) {
      const expected = this.lastActual + (this.audio.playing ? now - this.lastT : 0);
      if (Math.abs(actual - expected) > 0.15 && !(this.drag && (this.drag.kind === 'scscrub' || this.drag.kind === 'ovscrub'))) this.jump = [this.disp - actual, now];
    }
    this.lastActual = actual;
    this.lastT = now;
    let disp = actual;
    if (this.jump) {
      const p = (now - this.jump[1]) / ANIM;
      if (p >= 1) this.jump = null; else disp = actual + this.jump[0] * (1 - easeOut(p));
    }
    this.disp = disp;
    return disp;
  }

  tick() {
    requestAnimationFrame(() => this.tick());
    if (!this.root.isConnected || this.root.offsetParent === null) return;
    if (this.cpRange) {
      const p = this.pos();
      if (p >= this.cpRange[1] - 0.005 || !this.audio.playing) {
        if (this.s.loop_checkpoints && this.audio.playing) this.audio.seek(this.cpRange[0]);
        else { this.audio.pause(); this.cpRange = null; }
      }
    }
    const pos = this.pos();
    const disp = this.displayPos(pos);
    this.vTime.textContent = `${fmtTime(pos)} / ${fmtTime(this.dur())}`;
    const play = this.audio.playing ? '⏸ Pause' : '▶ Play';
    if (this.btnPlay.textContent !== play) this.btnPlay.textContent = play;
    this.btnPlay.disabled = !this.audio.ready;
    if (!this.sc._w) this.sizeCanvases();
    this.drawOverview(disp, pos);
    this.drawScroll(disp, pos);
    if (this.st) {
      this.highlight(this.curList, 'cur', this.st.cur_lines, pos);
      this.highlight(this.newList, 'new', this.st.new_lines, pos);
    }
  }

  ctx(c) {
    const g = c.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    return g;
  }

  drawLabels() {
    const shown = { cur: !!this.lanes.cur.length, new: true, aligned: !!this.lanes.aligned.length, edit: true };
    for (const [c, g, small] of [[this.ovLbl, OV, true], [this.scLbl, SC, false]]) {
      if (!c._w) continue;
      const x = this.ctx(c);
      x.fillStyle = BG;
      x.fillRect(0, 0, 70, g.height);
      x.font = `${small ? 10 : 11}px ${getComputedStyle(document.body).fontFamily}`;
      x.textAlign = 'right';
      x.textBaseline = 'middle';
      for (const k of LANES) {
        if (!shown[k]) continue;
        x.fillStyle = LABEL_COLOR[k];
        x.fillRect(65, g.lanes[k], 3, g.h);
        x.fillStyle = RULER_FG;
        x.fillText(LANE_LABEL[k], 62, g.lanes[k] + g.h / 2 + 0.5);
      }
      if (!small) { x.fillStyle = RULER_TICK; x.fillText(this.useVocals && this.vocals ? 'Vocals' : 'Waveform', 62, 30); }
    }
  }

  peakAt(i) { return this.peaks && i >= 0 && i < this.peaks.length ? this.peaks[i] / 255 : 0; }

  envelope(t0, t1, w) {
    // the loudest peak in each pixel column of [t0, t1)
    const out = new Float32Array(Math.max(1, Math.round(w)));
    if (!this.peaks) return out;
    const n = out.length, rate = this.peakRate;
    for (let i = 0; i < n; i++) {
      const a = Math.floor((t0 + ((t1 - t0) * i) / n) * rate), b = Math.max(a + 1, Math.floor((t0 + ((t1 - t0) * (i + 1)) / n) * rate));
      let m = 0;
      for (let j = Math.max(0, a); j < Math.min(b, this.peaks.length); j++) if (this.peaks[j] > m) m = this.peaks[j];
      out[i] = m / 255;
    }
    return out;
  }

  ruler(x, t0, t1, toX, y0, width, step) {
    if (t1 <= t0) return;
    step = step || rulerStep(t1 - t0, width);
    const minor = step >= 5 ? step / 5 : step > 1 ? step / 2 : 0.5;
    x.strokeStyle = RULER_TICK;
    x.fillStyle = RULER_FG;
    x.lineWidth = 1;
    x.beginPath();
    x.moveTo(toX(t0), y0 + 0.5);
    x.lineTo(toX(t1), y0 + 0.5);
    x.stroke();
    x.font = `9px ${getComputedStyle(document.body).fontFamily}`;
    x.textAlign = 'left';
    x.textBaseline = 'top';
    const k0 = Math.max(0, Math.floor(t0 / minor));
    for (let k = k0; k * minor <= t1 + 1e-6; k++) {
      const t = k * minor;
      if (t < t0 - 1e-6) continue;
      const px = Math.round(toX(t)) + 0.5;
      const major = Math.abs(t / step - Math.round(t / step)) < 1e-6;
      x.beginPath();
      x.moveTo(px, y0);
      x.lineTo(px, y0 + (major ? 6 : 3));
      x.stroke();
      if (major) x.fillText(fmtClock(t), px + 2, y0 + 3);
    }
  }

  drawBars(x, lane, bars, g, toX, active, { text = false, view = null } = {}) {
    const y0 = g.lanes[lane], h = g.h;
    for (const b of bars) {
      const x0 = toX(b.start), x1 = Math.max(x0 + 1, toX(b.end) - 1);
      if (view && (x1 < view[0] || x0 > view[1])) continue;
      x.fillStyle = b === active && !text ? ON[lane] : b.fill;
      x.fillRect(x0, y0, x1 - x0, h);
    }
    return y0;
  }

  drawOverview(disp) {
    const c = this.ov, w = c._w, h = OV.height, d = this.dur();
    if (!w) return;
    const sig = [w, this.st && this.st.lanes_ver, this.st && this.st.path, [...this.sel.keys].join(','), this.sel.lane,
      JSON.stringify(this.checkpoints()), d, this.peaks && this.peaks.length, this.drag && this.drag.kind === 'band' ? [this.drag.t0, this.drag.t1] : 0,
      this.drag && this.drag.kind === 'bar' && this.drag.moved ? [this.drag.cx, this.drag.cy] : 0].join('|');
    if (!this.ovCache || this.ovCache.sig !== sig || this.ovCache.c !== c) {
      const off = this.ovCache && this.ovCache.canvas.width === c.width ? this.ovCache.canvas : document.createElement('canvas');
      off.width = c.width;
      off.height = c.height;
      const x = off.getContext('2d');
      const dpr = window.devicePixelRatio || 1;
      x.setTransform(dpr, 0, 0, dpr, 0, 0);
      x.fillStyle = BG;
      x.fillRect(0, 0, w, h);
      if (d) {
        const env = this.envelope(0, d, w);
        x.fillStyle = WAVE;
        for (let i = 0; i < env.length; i++) { const a = env[i] * OV.amp; if (a > 0.3) x.fillRect(i, OV.mid - a, 1, a * 2); }
        const toX = (t) => (t / d) * w;
        for (const k of LANES) {
          this.drawBars(x, k, this.lanes[k], OV, toX, null);
          x.fillStyle = BG;
          for (const b of this.lanes[k]) for (const [ws] of (b.words || []).slice(1)) x.fillRect(toX(ws), OV.lanes[k], 1, OV.h);
        }
        this.drawSelection(x, OV, toX);
        this.ruler(x, 0, d, toX, OV.ruler, w);
        x.strokeStyle = CP_COLOR;
        x.fillStyle = CP_COLOR;
        x.setLineDash([3, 2]);
        x.font = `bold 10px ${getComputedStyle(document.body).fontFamily}`;
        this.checkpoints().forEach((t, i) => {
          if (t === null) return;
          const px = Math.round(toX(t)) + 0.5;
          x.beginPath(); x.moveTo(px, 0); x.lineTo(px, h); x.stroke();
          if (i < 9) x.fillText(String(i + 1), px + 3, 10);
        });
        x.setLineDash([]);
        this.drawDragOverlay(x, c, OV, toX);
      }
      this.ovCache = { sig, canvas: off, c };
    }
    const x = this.ctx(c);
    x.setTransform(1, 0, 0, 1, 0, 0);
    x.drawImage(this.ovCache.canvas, 0, 0);
    const dpr = window.devicePixelRatio || 1;
    x.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (!d) return;
    const toX = (t) => (t / d) * w;
    for (const k of LANES) {
      const b = this.lanes[k].find((z) => z.start <= this.lastActual && this.lastActual < z.end);
      if (b) { x.fillStyle = ON[k]; x.fillRect(toX(b.start), OV.lanes[k], Math.max(1, toX(b.end) - toX(b.start) - 1), OV.h); }
    }
    x.fillStyle = PLAYHEAD;
    x.fillRect(toX(disp) - 1, 0, 2, h);
  }

  drawSelection(x, g, toX) {
    if (!this.sel.lane || !this.sel.keys.size) return;
    x.strokeStyle = SEL;
    x.lineWidth = 2;
    const y0 = g.lanes[this.sel.lane];
    for (const b of this.lanes[this.sel.lane]) {
      if (!this.sel.keys.has(b.key)) continue;
      const x0 = toX(b.start), x1 = Math.max(x0 + 2, toX(b.end) - 1);
      x.strokeRect(x0, y0 - 1, x1 - x0, g.h + 2);
    }
    x.lineWidth = 1;
  }

  drawDragOverlay(x, c, g, toX) {
    const d = this.drag;
    if (!d || d.c !== c) return;
    if (d.kind === 'band' && d.moved && d.t1 !== undefined) {
      x.strokeStyle = SEL;
      x.setLineDash([3, 2]);
      x.strokeRect(toX(d.t0), g.lanes[d.lane] - 3, toX(d.t1) - toX(d.t0), g.h + 6);
      x.setLineDash([]);
    }
    if (d.kind === 'bar' && d.moved && d.lane !== 'edit') {
      const dy = (d.cy || d.y) - d.y;
      x.strokeStyle = GHOST;
      x.lineWidth = 2;
      x.setLineDash([4, 2]);
      for (const b of this.lanes[d.lane]) {
        if (!this.sel.keys.has(b.key)) continue;
        x.strokeRect(toX(b.start), g.lanes[d.lane] + dy, toX(b.end) - toX(b.start) - 1, g.h);
      }
      x.setLineDash([]);
      x.lineWidth = 1;
      if (d.cy >= g.lanes.edit - 4 && d.cy <= g.lanes.edit + g.h + 4) {
        x.strokeStyle = ON.edit;
        x.strokeRect(toX(0), g.lanes.edit - 2, toX(this.dur()) - toX(0), g.h + 4);
      }
    }
  }

  drawScroll(disp, pos) {
    const c = this.sc, w = c._w, h = SC.height, d = this.dur();
    if (!w) return;
    const x = this.ctx(c);
    x.fillStyle = BG;
    x.fillRect(0, 0, w, h);
    if (!d) return;
    const pps = w / WINDOW;
    const left = disp - WINDOW / 2, right = disp + WINDOW / 2;
    const toX = (t) => (t - left) * pps;
    const env = this.envelope(left, right, w);
    x.fillStyle = this.useVocals && this.vocals ? WAVE_VOC : WAVE;
    for (let i = 0; i < env.length; i++) { const a = env[i] * SC.amp; if (a > 0.3) x.fillRect(i, SC.mid - a, 1, a * 2); }
    x.font = `11px ${getComputedStyle(document.body).fontFamily}`;
    x.textBaseline = 'middle';
    x.textAlign = 'left';
    const words = (this.st && this.st.word_rows) || {};
    for (const k of LANES) {
      const y0 = SC.lanes[k], hh = SC.h;
      for (const b of this.lanes[k]) {
        const x0 = toX(b.start), x1 = toX(b.end) - 1;
        if (x1 < 0 || x0 > w) continue;
        x.fillStyle = b.fill;
        x.fillRect(x0, y0, Math.max(1, x1 - x0), hh);
        const active = b.start <= pos && pos < b.end && b.text.trim();
        if (active) {                                              // progress through the line
          x.fillStyle = ON[k];
          x.fillRect(x0, y0, Math.max(0, toX(pos) - x0), hh);
        }
        const mine = k === 'edit' && b.row !== null && words[b.row];
        (b.words || []).forEach(([ws, we, wt], j) => {
          if (j) {
            const wx = toX(ws);
            x.fillStyle = mine && mine.includes(j) ? '#ffe14d' : BG;
            x.fillRect(Math.round(wx), y0, mine && mine.includes(j) ? 2 : 1, hh);
            if (k === 'edit') { x.fillStyle = '#ffffff'; x.fillRect(Math.round(wx) - 2, y0 - 3, 5, 3); }   // a handle to drag
          }
          if (active && ws <= pos && pos < we) { x.strokeStyle = '#ffffff'; x.strokeRect(toX(ws) + 0.5, y0 + 0.5, toX(we) - toX(ws) - 1, hh - 1); }
        });
        // the text, kept at the left edge for a line that started before the view
        const tx0 = Math.max(x0, 0) + 3, room = x1 - tx0 - 2;
        if (room > 14 && b.text.trim()) {
          x.save();
          x.beginPath();
          x.rect(tx0, y0, room, hh);
          x.clip();
          x.fillStyle = k === 'edit' || k === 'aligned' ? '#ffffff' : '#111111';
          x.fillText(b.text, tx0, y0 + hh / 2 + 0.5);
          x.restore();
        }
      }
    }
    this.drawSelection(x, SC, toX);
    this.drawDragOverlay(x, c, SC, toX);
    this.ruler(x, Math.max(0, left), Math.min(d, right), toX, SC.ruler, w, pps >= 55 ? 1 : 2);
    x.fillStyle = PLAYHEAD;
    x.fillRect(w / 2 - 1, 0, 2, h);
    this.tools.classList.toggle('hidden', !this.audio.ready);
  }

  // ================================================================== keys
  key(e) {
    // keys for the player while Review & player is shown; true when used
    const k = e.key;
    if (k === ' ') { this.togglePlay(); return true; }
    if (/^[0-9]$/.test(k)) { const n = Number(k); n === 0 ? this.nextCheckpoint() : this.playCheckpoint(n - 1); return true; }
    if (e.code === 'Backquote' || k === '`' || k === '~') { this.cycleCheckpoint(); return true; }
    if (k === 'ArrowUp' || k === 'w' || k === 'W') { this.app.list.step(-1); return true; }
    if (k === 'ArrowDown' || k === 's' || k === 'S') { this.app.list.step(1); return true; }
    if (k === 'ArrowLeft' && e.shiftKey) { this.nudge(-5); return true; }
    if (k === 'ArrowRight' && e.shiftKey) { this.nudge(5); return true; }
    return false;
  }

  stop() { this.audio.pause(); this.cpRange = null; }
}

export { currentIndex };
void $;
