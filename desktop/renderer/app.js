// The window: header (activity), Find lyrics page, Review & player page, the Apply bar, and the connection to
// the backend. Song list → list.js, player → player.js.
import { SongList } from './list.js';
import * as M from './listmodel.js';
import { Player } from './player.js';
import { $, alertBox, confirmBox, debounce, dialog, el, errorBox, hint, menu, modalIsOpen, TITLE } from './ui.js';

const api = window.api;
const GPU = '⚡';
const LRC_ACTIONS = [['recycle', 'Move to Recycle Bin'], ['delete', 'Delete permanently'], ['keep', 'Keep']];
const SESSION_MODES = [['ask', 'Ask whether to load the last session'], ['load', 'Load the last session'], ['fresh', 'Start with an empty list']];
const WORD_SYNC_WARNING = 'Word-synced lyrics are less accurate than line-synced ones.\n\n' +
  "Online lyrics are almost never timed to the word, so every word's time comes only from the GPU aligner listening to " +
  "the song, and there is little reference to check it against. Words it can't hear clearly are spread out between the " +
  'ones it can.\n\nEvery song with found lyrics is aligned on the GPU (songs aligned before are redone from the cached ' +
  'result, without the GPU).\n\nReview every song in Review & player before saving. You can move any word in the ' +
  'Edited timeline.';

class App {
  constructor() {
    this.settings = {};
    this.activity = [];
    this.findState = {};
    this.bar = {};
    this.pendingSelect = null;
  }

  // ================================================================== start
  async start() {
    api.on((name, data) => this.event(name, data));
    api.onExit((info) => this.backendGone(info));
    api.onClosing(() => this.player && this.player.stop());
    let init;
    try {
      init = await api.call('app.init');
    } catch (e) {
      this.backendGone({ log: String(e.message || e) });
      return;
    }
    this.settings = init.settings;
    this.version = init.version;
    document.title = `${TITLE} ${init.version}`;
    $('#subtitle').textContent = `Find, review and apply time-synced lyrics · v${init.version}`;
    this.applyTheme();
    this.list = new SongList(this);
    this.list.ui.onlyFound = !!this.settings.only_found;
    this.player = new Player(this);
    this.buildFind();
    this.buildReview();
    this.buildApplyBar();
    this.bindGlobal();
    this.loadSettingsIntoFind();
    const rows = await api.call('rows.get');
    this.list.setRows({ clear: true, upsert: rows.rows });
    if (init.ui) this.applySessionUI(init.ui);
    if (init.loaded) this.showTab('review');
    if (init.session) this.offerSession(init.session);
  }

  rv(name, ...args) { return api.call('rv.call', { name, args }); }

  async setSetting(patch) {
    Object.assign(this.settings, patch);
    try {
      this.settings = await api.call('settings.set', { patch });
    } catch (e) { errorBox(String(e.message || e)); }
    this.settingsChanged();
    return this.settings;
  }

  settingsChanged() {
    this.applyTheme();
    this.loadSettingsIntoFind();
    if (this.player) this.player.settingsChanged();
    if (this.list) { this.list.layout(); this.list.refresh(true); this.syncListControls(); }
    this.renderApplyBar();
  }

  applyTheme() {
    const mode = this.settings.theme === 'dark' ? 'dark' : 'light';
    if (document.documentElement.dataset.theme !== mode) {
      document.documentElement.dataset.theme = mode;
      api.setTheme(mode);
    }
    const sw = $('#dark');
    if (sw) sw.checked = mode === 'dark';
  }

  // ================================================================== events from the backend
  event(name, d) {
    switch (name) {
      case 'log': this.log(d); break;
      case 'activity': this.activity = d.rows; this.renderActivity(); break;
      case 'find': this.findState = d; this.renderFind(); break;
      case 'applybar': this.bar = d; this.renderApplyBar(); break;
      case 'rows': this.list.setRows(d); break;
      case 'review': this.player.setState(d); break;
      case 'review_audio': this.player.setAudio(d); break;
      case 'review_vocals': this.player.setVocals(d); break;
      case 'settings': this.settings = d; this.settingsChanged(); break;
      case 'dialog': d.kind === 'error' ? errorBox(d.text) : alertBox(d.text); break;
      case 'select': this.select(d); break;
      case 'align_error': this.alignError(d); break;
      case 'align_waiting': if (this._alignWait) this._alignWait.textContent = this.waitingText(d.waiting); break;
      case 'player': if (d.stop) this.player.stop(); break;
      default: break;
    }
  }

  backendGone(info) {
    const pre = el('pre', { style: { maxHeight: '240px', overflow: 'auto', fontSize: '11px', whiteSpace: 'pre-wrap', userSelect: 'text' },
      text: (info.log || '').slice(-3000) || '(no details)' });
    dialog({ title: 'The app\'s engine stopped', kind: 'error', message: 'The part of the app that finds lyrics, reads and writes ' +
      'files stopped unexpectedly. Nothing is lost: your choices and edits are saved as you go.', body: pre,
    buttons: [{ label: 'Restart', value: true, accent: true }, { label: 'Close', value: false, cancel: true }] }).then(async (again) => {
      if (again) { await api.restartBackend(); location.reload(); }
    });
  }

  log(d) {
    const box = this.logBox;
    if (!box) return;
    if (d.clear) box.textContent = '';
    if (!d.lines || !d.lines.length) return;
    const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    box.append(document.createTextNode(d.lines.join('\n') + '\n'));
    const text = box.textContent;
    const lines = text.split('\n');
    if (lines.length > 3100) box.textContent = lines.slice(-3000).join('\n');
    if (atEnd) box.scrollTop = box.scrollHeight;
  }

  renderActivity() {
    const box = $('#activity');
    box.replaceChildren(...this.activity.map((r) => {
      const p = el('progress', { max: r.max || 100 });
      if (r.value !== null && r.value !== undefined) p.value = r.value;
      return el('div', { class: 'act' }, el('div', { class: 't', text: r.title }), p, el('div', { class: 'd', text: r.detail, title: r.detail }),
        r.stop ? el('button', { class: 'small', text: 'Stop', hint: 'Stop this operation.', onclick: () => api.call('activity.stop', { key: r.key }) }) : el('span'));
    }));
    this.renderFind();
  }

  installing() { return this.activity.some((r) => r.key === 'install'); }

  select(d) {
    if (d.tab) this.showTab(d.tab);
    this.pendingSelect = d;
    this.list.refresh(true);
  }

  listRebuilt() {
    const d = this.pendingSelect;
    if (!d) return;
    if (d.song) {
      if (this.list.tree.nodes.has(d.song)) {
        this.pendingSelect = null;
        if (!d.only_if_none || !this.list.sel.size) this.list.selectSong(d.song);
      }
    } else {
      this.pendingSelect = null;
      if (d.first && !this.list.sel.size) { const f = this.list.firstSong(); if (f) this.list.selectSong(f); }
    }
  }

  // ================================================================== tabs and keys
  showTab(which) {
    $('#tab-find').classList.toggle('sel', which === 'find');
    $('#tab-review').classList.toggle('sel', which === 'review');
    $('#find').classList.toggle('hidden', which !== 'find');
    $('#review').classList.toggle('hidden', which !== 'review');
    this.tab = which;
    if (which === 'review') { this.list.layout(); this.player.sizeCanvases(); this.player.drawCpBar(); }
  }

  bindGlobal() {
    $('#tab-find').onclick = () => this.showTab('find');
    $('#tab-review').onclick = () => this.showTab('review');
    this.showTab('find');
    $('#dark').addEventListener('change', (e) => this.setSetting({ theme: e.target.checked ? 'dark' : 'light' }));
    hint($('#dark-switch'), 'Switch between light and dark appearance.');
    document.addEventListener('keydown', (e) => {
      if (e.key === 'F12') { api.devtools(); return; }
      if (this.tab !== 'review' || modalIsOpen() || e.ctrlKey || e.altKey || e.metaKey) return;
      const t = e.target;
      const typing = t instanceof HTMLInputElement && !['checkbox', 'radio', 'range', 'button'].includes(t.type) ||
        t instanceof HTMLTextAreaElement || t instanceof HTMLSelectElement && (e.key === 'ArrowUp' || e.key === 'ArrowDown');
      if (typing) return;
      if (t === this.list.root && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) return;    // the list moves its own selection
      if (this.player.key(e)) {
        e.preventDefault();
        if (t instanceof HTMLElement && t !== document.body && t.blur) t.blur();         // Space never presses a button again
      }
    }, true);
  }

  // ================================================================== Find lyrics page
  buildFind() {
    const page = $('#find');
    const sw = (key, text, tip, onchange) => {
      const inp = el('input', { type: 'checkbox' });
      inp.dataset.key = key;
      inp.addEventListener('change', onchange || (() => this.setSetting({ [key]: inp.checked })));
      return el('label', { class: 'switch', hint: tip }, inp, text);
    };
    const num = (key, lo, hi, tip) => {
      const inp = el('input', { type: 'number', min: lo, max: hi, step: 5, hint: tip });
      inp.dataset.key = key;
      inp.addEventListener('change', () => {
        const v = M.snap5(inp.value, lo, hi);
        inp.value = v;
        this.setSetting({ [key]: v });
      });
      return inp;
    };
    const sel = (key, options, tip) => {
      const s = el('select', { hint: tip }, ...options.map(([v, l]) => el('option', { value: v, text: l })));
      s.dataset.key = key;
      s.addEventListener('change', () => this.setSetting({ [key]: s.value }));
      return s;
    };
    this.findInputs = page;

    // left: library, collection, scores and alignment
    this.inFolder = el('input', { type: 'text', class: 'grow', hint: 'The music folder to scan. Sub-folders are included.' });
    this.inFolder.addEventListener('change', () => this.setSetting({ music_folder: this.inFolder.value.trim() }));
    this.collBox = el('div', { class: 'listbox grow', hint: "Folders and .lrc files from your own collection. They're matched by their [ar:]/[ti:] tags or an 'Artist - Title.lrc' name, plus song length." });
    this.collSel = new Set();
    const lib = el('div', { class: 'card' },
      el('h2', { text: 'Music library' }),
      el('div', { class: 'row' }, this.inFolder, el('button', { text: 'Browse…', hint: 'Choose the music folder to scan.', onclick: async () => {
        const d = await api.pickFolder('Choose music folder', this.settings.music_folder);
        if (d) this.setSetting({ music_folder: d });
      } })),
      el('div', { class: 'sep' }),
      el('h2', { text: 'Your own LRC collection' }),
      sw('use_collection', 'Use these folders / files', 'Look in your own .lrc files (listed below) before searching online. Collection files are never moved or deleted.'),
      el('div', { class: 'row', style: { alignItems: 'flex-start', marginTop: '6px' } }, this.collBox,
        el('div', { class: 'col' },
          el('button', { text: 'Add folder…', hint: 'Add a folder of .lrc files (searched recursively) to your collection.', onclick: async () => {
            const d = await api.pickFolder('Add folder of .lrc files');
            if (d && !this.settings.collection.includes(d)) this.setSetting({ collection: [...this.settings.collection, d], use_collection: true });
          } }),
          el('button', { text: 'Add files…', hint: 'Add individual .lrc files to your collection.', onclick: async () => {
            const files = await api.pickFiles('Add .lrc files', [{ name: 'LRC lyrics', extensions: ['lrc'] }], true);
            const add = files.filter((f) => !this.settings.collection.includes(f));
            if (add.length) this.setSetting({ collection: [...this.settings.collection, ...add], use_collection: true });
          } }),
          el('button', { text: 'Remove', hint: 'Remove the selected folders/files from the collection list (nothing is deleted).', onclick: () => {
            this.setSetting({ collection: this.settings.collection.filter((_, i) => !this.collSel.has(i)) });
            this.collSel.clear();
          } }))),
      el('div', { class: 'sep' }),
      el('h2', { text: 'Scores and alignment' }),
      el('div', { class: 'scores' },
        'Match confidence (70–100)', num('match_threshold', 70, 100, 'How closely names must match for a sidecar or collection .lrc to be used automatically. Lower finds more, higher is stricter. Steps of 5%.'),
        'High confidence (50–100)', num('confident_threshold', 50, 100, 'Songs whose found lyrics match at this % or better count as high confidence: Apply confident writes them without asking, and Hide low confidence hides the rest. 100% = exact title, artist, album and length.'),
        'Good fit (0–100)', num('confident_fit', 0, 100, 'Found lyrics fit the recording well at this % or better (length, lines past the end, spread over the song, lines on silence). Apply confident needs both High confidence and Good fit; Hide low confidence hides songs below either.'),
        'Aligned lyrics must match (50–100)', num('ai_align_min', 50, 100, "At least this % of the lyric's words heard being sung: the alignment counts as verified (Match raised, Apply confident can write it). Below it, the lines it was sure of are still placed and the rest interpolated (⚡≈ partly aligned), left for you to check. Under 30% heard, or fewer than 2 lines it's sure of, nothing changes. Steps of 5%.")),
      el('div', { class: 'col', style: { marginTop: '8px', gap: '2px' } },
        sw('fit_audio', 'Check lyrics against the audio (slower)', 'While finding, decode each song with found lyrics to see whether lines start on silence or run into the silence at the end; this sharpens the Fit score. Songs opened in the player are always checked.'),
        sw('ai_align', GPU + ' Align low-confidence lyrics (NVIDIA GPU)', 'After Find, re-time the found lyrics that are below High confidence or Good fit: the vocals are isolated (Demucs) and the lyric words are located in them (Whisper). Lines heard clearly are placed, the rest interpolated, and the result is kept only if it reaches Aligned lyrics must match. The isolated vocals are kept for the player\'s Vocals switch. Needs an NVIDIA graphics card and a one-time add-on download of about 3 GB. Off by default.',
          (e) => this.aiToggle(e.target.checked)),
        sw('ai_align_all', GPU + ' Align every song, not only low-confidence ones', 'With Align low-confidence lyrics on: send every song with found lyrics to the GPU, not only those below High confidence or Good fit. Songs already aligned are skipped. Off by default.'),
        sw('ai_keep_loaded', GPU + ' Keep the GPU models loaded', 'Keep the aligner and its speech and vocal models in GPU memory (about 3 GB) between songs and runs, so aligning and Auto-align start at once instead of spending about 9 s starting up. Off: they\'re released after 2 minutes without work. On by default.')),
      this.syncRow());

    // right: options
    this.inKey = el('input', { type: 'password', class: 'grow', hint: (this.defaultKey ? 'Blank = built-in key. ' : '') +
      'AcoustID needs an application key from acoustid.org/new-application (a personal user key is rejected). Only used for files with no title/artist tags.' });
    this.inKey.addEventListener('change', () => this.setSetting({ acoustid_key: this.inKey.value.trim() }));
    const showKey = el('input', { type: 'checkbox' });
    showKey.addEventListener('change', () => { this.inKey.type = showKey.checked ? 'text' : 'password'; });
    const opt = el('div', { class: 'card' },
      el('h2', { text: 'Options' }),
      el('div', { class: 'col', style: { gap: '2px' } },
        sw('use_online', 'Look up missing lyrics online (LRCLIB)', 'Search LRCLIB, a free community lyrics database, for songs without local lyrics.'),
        sw('skip_synced_online', 'No online requests for songs with synced lyrics embedded', 'Files that already carry time-synced lyrics are never sent to LRCLIB or AcoustID. A sidecar .lrc or your collection can still replace them, and in .lrc mode their embedded lyrics are still exported. Turn off to also look them up online, so Review can compare them with LRCLIB.'),
        sw('use_acoustid', 'Identify untagged files with AcoustID', 'Fingerprint files that have no title/artist tags to find out what they are.'),
        el('div', { class: 'row', style: { margin: '2px 0 8px' } }, el('span', { text: 'AcoustID key' }), this.inKey,
          el('label', { class: 'check', hint: 'Show or hide the key.' }, showKey, 'Show'))),
      el('div', { class: 'opts' },
        'After using a sidecar .lrc', sel('lrc_action', LRC_ACTIONS, 'What happens to a sidecar .lrc after its lyrics are applied: Recycle Bin, delete, or keep. A <song>.lrc that is itself the output is never removed.'),
        'When the app starts', sel('session_restore', SESSION_MODES, 'What to do with the last review session when the app opens.')),
      el('div', { class: 'col', style: { gap: '2px', marginTop: '8px' } },
        sw('tidy_trim', 'Trim spaces from each lyric line', 'Remove spaces at the start and end of every lyric line (after its timestamp) in the lyrics that get written.'),
        sw('tidy_quotes', 'Straighten smart quotes and punctuation', "Typographic characters become plain ones in the lyrics that get written: ‘ ’ → '   “ ” → \"   … → ...   – — → -   and unusual or invisible spaces → normal spaces."),
        sw('restore_mtime', 'Keep original file modified dates', "After writing, put each file's modified date back so cloud sync and music libraries don't treat it as changed."),
        sw('retry_misses', 'Retry songs not found in earlier runs', 'Search online again for songs that had no lyrics last time.'),
        sw('ignore_cache', 'Ignore cached results', 'Work everything out again instead of reusing earlier results: songs Find already handled, online lookups, and what the GPU heard (songs are aligned again from their found timing). Slower. Off by default. To redo only some songs, select them in Review & player and use Clear cached data.'),
        sw('only_found', 'List only songs with lyrics found', "Review & player lists only the songs that have lyrics to write. Songs with nothing found (or already synced) are left out. Turn the list's Lyrics found box off to see them.")));

    // bottom: actions + log
    this.btnStart = el('button', { class: 'accent', text: '  Find lyrics  ', hint: 'Scan the music folder and find lyrics: sidecar .lrc files, your collection, then LRCLIB. Nothing is written until you press Apply at the bottom.', onclick: () => this.findStart() });
    this.btnStop = el('button', { text: 'Stop', disabled: true, hint: 'Stop after the current song. Run Find lyrics again to continue where it stopped.', onclick: () => api.call('find.stop') });
    this.btnCsv = el('button', { text: 'Open last CSV', disabled: true, hint: 'Open the CSV log of the last run (status, source and details for each song).',
      onclick: async () => { const p = await api.call('app.paths'); if (p.csv) api.openPath(p.csv); } });
    this.findProgress = el('progress', { max: 1, value: 0 });
    this.findStatus = el('div', { class: 'muted', text: 'Ready.' });
    this.logBox = el('div', { id: 'log' });
    const actions = el('div', { class: 'card', id: 'find-actions' },
      el('div', { class: 'row' }, this.btnStart, this.btnStop,
        el('button', { text: GPU + ' Align low-confidence (GPU)', hint: 'Align the songs in the list that are below High confidence or Good fit on the NVIDIA GPU (switches the option on if needed). Progress shows at the top of the window.', onclick: () => this.alignCommand('low') }),
        el('div', { class: 'grow' }), this.btnCsv,
        el('button', { text: 'Open logs folder', hint: 'Open the folder with the CSV logs of every run and apply.', onclick: async () => { const p = await api.call('app.paths'); api.openPath(p.logs); } })),
      el('div', { style: { margin: '12px 0 4px' } }, this.findProgress), this.findStatus,
      el('div', { style: { height: '8px' } }), this.logBox);
    page.append(lib, opt, actions);
  }

  syncRow() {
    const tip = 'To the line: each line gets a time (standard synced lyrics, as found online).\nTo the word: every word gets a time too (enhanced LRC, for karaoke-style players). Online lyrics are almost never timed to the word, so the word times come only from the GPU aligner listening to the song: less accurate, with little to check them against. Every song is aligned; review each one before saving (you can move any word in the Edited timeline).';
    this.rbLine = el('input', { type: 'radio', name: 'sync' });
    this.rbWord = el('input', { type: 'radio', name: 'sync' });
    this.rbLine.addEventListener('change', () => this.syncToggled(false));
    this.rbWord.addEventListener('change', () => this.syncToggled(true));
    return el('div', { class: 'row', style: { marginTop: '8px' } }, el('span', { text: 'Synced lyrics', hint: tip }),
      el('label', { class: 'radio', hint: tip }, this.rbLine, 'To the line'),
      el('label', { class: 'radio', hint: tip }, this.rbWord, GPU + ' To the word'));
  }

  loadSettingsIntoFind() {
    const s = this.settings;
    if (!this.findInputs) return;
    for (const inp of this.findInputs.querySelectorAll('[data-key]')) {
      const k = inp.dataset.key;
      if (document.activeElement === inp) continue;
      if (inp.type === 'checkbox') inp.checked = !!s[k];
      else inp.value = s[k] ?? '';
    }
    if (document.activeElement !== this.inFolder) this.inFolder.value = s.music_folder || '';
    if (document.activeElement !== this.inKey) this.inKey.value = s.acoustid_key || '';
    this.rbLine.checked = !s.sync_words;
    this.rbWord.checked = !!s.sync_words;
    this.collBox.replaceChildren(...(s.collection || []).map((p, i) => {
      const d = el('div', { class: this.collSel.has(i) ? 'sel' : '', text: p, title: p });
      d.addEventListener('click', (e) => {
        if (!(e.ctrlKey || e.metaKey)) this.collSel.clear();
        this.collSel.has(i) ? this.collSel.delete(i) : this.collSel.add(i);
        this.loadSettingsIntoFind();
      });
      return d;
    }));
  }

  renderFind() {
    const f = this.findState;
    if (!this.btnStart) return;
    const applying = !!this.bar.applying;
    const aligning = this.activity.some((r) => r.key === 'align');
    this.btnStart.disabled = !!f.running || applying || this.installing();
    this.btnStop.disabled = !(f.running && !f.stopping) && !aligning;
    this.btnCsv.disabled = !f.csv;
    this.findStatus.textContent = f.status || '';
    if (f.busy) this.findProgress.removeAttribute('value');
    else { this.findProgress.max = f.max || 1; this.findProgress.value = f.value || 0; }
  }

  async findStart() {
    if (this.settings.sync_words && !this.settings.word_sync_warned) {
      if (!(await this.warnWordSync())) await this.setSetting({ sync_words: false });
    }
    await this.setSetting({ music_folder: this.inFolder.value.trim() });
    const r = await api.call('find.start');
    if (r.error) { errorBox(r.error); return; }
    if (r.info) { alertBox(r.info); return; }
    if (r.started) {
      this.list.ui.onlyFound = !!r.only_found;
      this.list.sel.clear();
      this.syncListControls();
    }
  }

  // ================================================================== GPU switches and questions
  async aiToggle(on) {
    const r = await api.call('ai.toggle', { on });
    if (r.error) errorBox(r.error);
    else if (r.need_install && await confirmBox(r.question)) await api.call('ai.install');
    this.settingsChanged();
  }

  async ensureAI() {
    if (this.settings.ai_align) return true;
    if (this.installing()) { alertBox('The GPU add-on is still installing (see the progress at the top).'); return false; }
    if (!(await confirmBox('Aligning uses the NVIDIA GPU add-on (Align low-confidence lyrics). Switch it on?'))) return false;
    await this.aiToggle(true);
    return !!this.settings.ai_align;
  }

  async warnWordSync() {
    if (this.settings.word_sync_warned) return true;
    const ok = await confirmBox(WORD_SYNC_WARNING + '\n\nSync lyrics to the word?', { ok: 'OK', cancel: 'Cancel', kind: 'warn' });
    if (ok) await this.setSetting({ word_sync_warned: true });
    return ok;
  }

  async syncToggled(words) {
    if (words) {
      if (!(await this.warnWordSync())) { this.settingsChanged(); return; }
      if (!this.settings.ai_align && !this.installing()) {
        if (!(await this.ensureAI()) && !this.installing()) { this.settingsChanged(); return; }
      }
    }
    await this.setSetting({ sync_words: words });
  }

  async alignCommand(which, songs = null, words = null) {
    if (words && !(await this.warnWordSync())) return;
    if (!(await this.ensureAI())) return;
    if (which === 'selected') { songs = this.list.selectedSongs(); which = 'songs'; if (!songs.length) { alertBox('Select songs in the Review & player list first.'); return; } }
    const r = await api.call('align.command', { which, songs, words: words === null ? null : words });
    if (r.info) alertBox(r.info);
  }

  waitingText(n) {
    return `Finding lyrics carries on meanwhile. The song that failed keeps its found lyrics. ${n} song(s) are waiting to be aligned.`;
  }

  async alignError(d) {
    if (!d || this._alignWait) return;                              // one at a time
    this._alignWait = el('div', { class: 'muted', text: this.waitingText(d.waiting) });
    const body = el('div', { class: 'col' }, el('div', { class: 'msg', text: d.error }), this._alignWait);
    const choice = await dialog({ title: 'Aligning on the GPU stopped with an error', kind: 'error', body,
      buttons: [{ label: 'Ignore and keep aligning', value: 'ignore', accent: true }, { label: 'Stop using alignment', value: 'disable' },
        { label: 'Cancel the run', value: 'cancel' }] });
    this._alignWait = null;
    await api.call('align.error_choice', { what: choice || 'ignore' });
  }

  // ================================================================== session
  async offerSession(info) {
    const remember = el('input', { type: 'checkbox' });
    const saved = el('input', { type: 'checkbox' });
    const body = el('div', { class: 'col' },
      el('label', { class: 'check', hint: "Don't ask next time. Change it later under 'When the app starts' on the Find lyrics tab." }, remember, 'Remember my choice'),
      el('label', { class: 'check', hint: 'Bring back songs whose lyrics were already written, shown as Saved. Their files are not changed.' }, saved, 'Also include songs that were already saved'));
    const load = await dialog({ title: 'Load your last session?', message: `${info.n} song${info.n !== 1 ? 's' : ''} from ${info.folder}, saved ${info.when}.`,
      body, buttons: [{ label: 'Load session', value: true, accent: true }, { label: 'Start fresh', value: false, cancel: true }] });
    const r = await api.call('session.load', { load: !!load, remember: remember.checked, include_saved: saved.checked });
    if (r.ui) this.applySessionUI(r.ui);
    if (r.loaded) this.showTab('review');
  }

  applySessionUI(ui) {
    this.list.ui.filter = M.FILTERS[ui.filter] !== undefined ? ui.filter : 'All';
    this.list.ui.search = ui.search || '';
    if ('only_found' in ui) this.list.ui.onlyFound = !!ui.only_found;
    this.syncListControls();
    if (ui.selected) this.pendingSelect = { song: ui.selected };
    this.list.refresh(true);
  }

  sessionUI = debounce(() => api.call('session.ui', { selected: this.player.st ? this.player.st.path : '', filter: this.list.ui.filter,
    search: this.list.ui.search, only_found: !!this.list.ui.onlyFound }), 500);

  // ================================================================== Review & player page
  buildReview() {
    const page = $('#review');
    const left = el('div', { class: 'left' });
    this.selView = el('select', { hint: 'Arrange the list by Artist ▸ Album (from the tags) or by the folders on disk.' },
      ...Object.entries(M.VIEWS).map(([v, l]) => el('option', { value: v, text: l })));
    this.selView.addEventListener('change', () => { this.list.open.clear(); this.setSetting({ list_view: this.selView.value }); });
    this.selFilter = el('select', { hint: 'Show only some songs: needing a look, found, changed, your choices…' },
      ...Object.keys(M.FILTERS).map((f) => el('option', { value: f, text: f })));
    this.selFilter.addEventListener('change', () => { this.list.ui.filter = this.selFilter.value; this.list.refresh(true); this.sessionUI(); });
    this.inSearch = el('input', { type: 'search', class: 'grow', placeholder: 'Search', hint: 'Search by artist, album, title or path.' });
    this.inSearch.addEventListener('input', debounce(() => { this.list.ui.search = this.inSearch.value; this.list.refresh(true); this.sessionUI(); }, 250));
    const pills = el('div', { class: 'pills' }, ...M.LEGEND.map(([c, t, h]) => el('span', { class: 'pill ' + c, text: t, hint: h })));
    this.cbFound = el('input', { type: 'checkbox' });
    this.cbFound.addEventListener('change', () => { this.list.ui.onlyFound = this.cbFound.checked; this.list.refresh(true); this.sessionUI(); });
    this.cbSaved = el('input', { type: 'checkbox' });
    this.cbSaved.addEventListener('change', () => this.setSetting({ hide_saved: this.cbSaved.checked }));
    this.cbLow = el('input', { type: 'checkbox' });
    this.cbLow.addEventListener('change', () => this.setSetting({ hide_low_confidence: this.cbLow.checked }));
    const filters = el('div', { class: 'row' },
      el('label', { class: 'check', hint: 'Show only songs with lyrics found that can be written (local or online). The default for each run is set on the Find lyrics tab.' }, this.cbFound, 'Lyrics found'),
      el('label', { class: 'check', hint: 'Hide songs whose lyrics are already saved or already up to date.' }, this.cbSaved, 'Hide saved'),
      el('label', { class: 'check', hint: 'Hide songs whose found lyrics are below the High confidence or Good fit level set on the Find lyrics tab.' }, this.cbLow, 'Hide low confidence'));
    this.vCount = el('span', { class: 'muted', style: { marginLeft: 'auto' }, text: '0 shown / 0 songs' });
    const alignBtn = el('button', { text: GPU + ' Align (GPU) ▾', hint: 'Align lyrics to the vocals on the NVIDIA GPU: the low-confidence songs in this list, the selected songs, or all of them. Songs already aligned are skipped. Progress shows at the top of the window.' });
    alignBtn.addEventListener('click', () => {
      const r = alignBtn.getBoundingClientRect();
      menu([{ label: GPU + ' Align low-confidence songs in the list', action: () => this.alignCommand('low') },
        { label: GPU + ' Align selected songs', action: () => this.alignCommand('selected') },
        { label: GPU + ' Align all songs in the list', action: () => this.alignCommand('all') }], r.left, r.bottom + 2);
    });
    const colBtn = el('button', { text: 'Columns ▾', hint: 'Show or hide columns (also: right-click a column header).' });
    colBtn.addEventListener('click', () => { const r = colBtn.getBoundingClientRect(); this.list.columnsMenu(r.left, r.bottom + 2); });
    left.append(el('div', { class: 'row' }, this.selView, this.selFilter, this.inSearch), pills, filters, this.list.root,
      el('div', { class: 'row' },
        el('button', { text: 'Load earlier results', hint: 'Add every song from earlier runs to the list.', onclick: () => api.call('rows.load_earlier') }),
        el('button', { text: 'Open file…', hint: 'Open any song in the player.', onclick: () => this.openFile() }), colBtn),
      el('div', { class: 'row' }, alignBtn,
        el('button', { text: 'Clear cached data', hint: "Forget what was worked out for the selected songs (online lookup, what the GPU heard, its isolated vocals, Find's record) so the next Find / Align does them again. A GPU alignment is undone. Your choices, edits and backups stay; no files are changed. Also: right-click.", onclick: () => this.clearCache(this.list.selectedSongs()) }),
        this.vCount));
    const split = el('div', { class: 'split' });
    page.append(left, split, this.player.root);
    const w = this.settings.review_split || Math.max(520, Math.min(680, Math.round(innerWidth * 0.4)));
    left.style.width = w + 'px';
    split.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const x0 = e.clientX, w0 = left.offsetWidth;
      const move = (ev) => { left.style.width = Math.max(300, Math.min(innerWidth - 560, w0 + ev.clientX - x0)) + 'px'; };
      const up = () => {
        window.removeEventListener('mousemove', move);
        window.removeEventListener('mouseup', up);
        this.setSetting({ review_split: left.offsetWidth });
      };
      window.addEventListener('mousemove', move);
      window.addEventListener('mouseup', up);
    });
    this.syncListControls();
  }

  syncListControls() {
    if (!this.selView) return;
    this.selView.value = this.settings.list_view === 'folder' ? 'folder' : 'artist';
    this.selFilter.value = this.list.ui.filter;
    if (document.activeElement !== this.inSearch) this.inSearch.value = this.list.ui.search;
    this.cbFound.checked = !!this.list.ui.onlyFound;
    this.cbSaved.checked = !!this.settings.hide_saved;
    this.cbLow.checked = !!this.settings.hide_low_confidence;
  }

  listCount(text) { this.vCount.textContent = text; this.listRebuilt(); }

  listSelected(focus, songs) {
    const one = songs.length > 20 ? songs.slice(0, 1) : songs;
    if (one.length) api.call('prioritize', { paths: one });
    const n = focus && this.list.tree.nodes.get(focus);
    if (n && n.kind === 'song' && (!this.player.st || this.player.st.path !== focus || !this.player.st.loaded)) {
      if (this._loading !== focus) { this._loading = focus; this.player.stop(); api.call('rv.load', { path: focus }); }
    }
    this.sessionUI();
  }

  async openFile() {
    const files = await api.pickFiles('Open a song', [{ name: 'Audio', extensions: ['mp3', 'flac', 'ogg', 'opus', 'm4a', 'mp4', 'aac', 'wav', 'aiff', 'aif', 'wma', 'wv', 'ape'] }, { name: 'All files', extensions: ['*'] }], false);
    if (!files.length) return;
    const p = await api.call('rows.add', { path: files[0] });
    this.list.ui.filter = 'All';
    this.list.ui.search = '';
    this.list.ui.onlyFound = false;
    await this.setSetting({ hide_saved: false, hide_low_confidence: false });
    this.syncListControls();
    this.list.rebuild();
    this.list.selectSong(p);
    this.showTab('review');
  }

  async cycle(path) { await api.call('rows.cycle', { path }); }
  async cycleMany(songs, node) { await api.call('rows.cycle_many', { songs, node }); }
  async setChoice(songs, kind) { await api.call('rows.set_choice', { songs, kind }); }
  async resetChoice(songs) { await api.call('rows.reset', { songs }); }
  async removeSongs(songs) { await api.call('rows.remove', { songs }); }

  async clearCache(songs) {
    if (!songs.length) { alertBox('Select songs in the Review & player list first.'); return; }
    const n = songs.length;
    if (!(await confirmBox(`Clear cached data for ${n} song${n !== 1 ? 's' : ''}?\n\nThe next Find and Align work them out again: the online lookup, what the GPU heard (and its isolated vocals), and Find's record of them. A GPU alignment is undone (the found lyrics go back to their found timing).\n\nYour choices, edits and the backups of original lyrics are kept. No files are changed.`))) return;
    const r = await api.call('cache.clear', { songs });
    if (r.info) alertBox(r.info);
  }

  async restorePaths(songs) {
    if (!songs.length) return;
    if (!(await confirmBox(`Restore the original lyrics of ${songs.length} song(s)?\n\nFiles this app wrote to get back the lyrics (or .lrc file) they had before. Songs it never wrote are skipped.`))) return;
    await api.call('restore.start', { paths: songs });
  }

  async openLocation(id, node) {
    try {
      if (node.kind === 'group') { const f = this.list.groupFolder(id, node); if (f) await api.openPath(f); } else await api.showItem(M.displayPath(id));
    } catch (e) { errorBox(`Could not open the location:\n${e.message || e}`); }
  }

  // ================================================================== Apply bar
  buildApplyBar() {
    const b = $('#applybar');
    this.rbEmbed = el('input', { type: 'radio', name: 'out' });
    this.rbLrc = el('input', { type: 'radio', name: 'out' });
    this.rbEmbed.addEventListener('change', () => this.setSetting({ output_mode: 'embed' }));
    this.rbLrc.addEventListener('change', () => this.setSetting({ output_mode: 'lrc' }));
    this.barText = el('span', { class: 'muted grow', style: { textAlign: 'right' } });
    this.barProgress = el('progress', { style: { width: '160px' } });
    this.barStop = el('button', { text: 'Stop', hint: 'Stop applying after the current song.', onclick: () => api.call('apply.stop') });
    this.btnConf = el('button', { text: 'Apply confident (0)', hint: 'Write, without asking, the found lyrics at or above both the High confidence and Good fit levels (Find lyrics tab). Songs with your own choices or edits are left for Apply.', onclick: () => this.applyConfident() });
    this.btnApply = el('button', { class: 'accent', text: 'Apply', hint: 'Write the chosen lyrics for every song in the list that has something to write. To write just some songs, select them and right-click → Save now.', onclick: () => this.applyPaths(null) });
    b.append(el('h2', { text: 'Final step' }),
      el('label', { class: 'radio', hint: 'Apply writes the lyrics into each audio file\'s tags. Originals are backed up and can be restored.' }, this.rbEmbed, 'Overwrite files (embed lyrics)'),
      el('label', { class: 'radio', hint: "Apply writes '<song name>.lrc' next to each song and leaves the audio files untouched." }, this.rbLrc, 'Save .lrc beside each file'),
      this.barText, this.barProgress, this.barStop, this.btnConf, this.btnApply);
    this.renderApplyBar();
  }

  renderApplyBar() {
    const d = this.bar;
    const b = $('#applybar');
    if (!this.btnApply) return;
    b.classList.toggle('hidden', !d.shown);
    this.rbEmbed.checked = this.settings.output_mode !== 'lrc';
    this.rbLrc.checked = this.settings.output_mode === 'lrc';
    const target = this.settings.output_mode === 'lrc' ? 'save .lrc for' : 'embed into';
    this.btnApply.textContent = `Apply: ${target} ${d.n || 0} file(s)`;
    this.btnApply.disabled = !d.n || d.applying;
    this.btnConf.textContent = `Apply confident (${d.nc || 0})`;
    this.btnConf.disabled = !d.nc || d.applying;
    this.barText.textContent = d.text || '';
    this.barProgress.classList.toggle('hidden', !d.applying);
    this.barStop.classList.toggle('hidden', !d.applying);
    this.barProgress.max = d.max || 1;
    this.barProgress.value = d.value || 0;
    this.renderFind();
  }

  async applyPaths(paths) {
    const plan = await api.call('apply.plan', { paths });
    if (!plan.n) { alertBox(plan.info); return; }
    if (!(await confirmBox(plan.question, { kind: plan.word_synced ? 'warn' : '' }))) return;
    this.player.stop();
    const r = await api.call('apply.start', { paths });
    if (r.info) alertBox(r.info);
  }

  async applyConfident() {
    const plan = await api.call('apply.confident_plan');
    if (!plan.paths.length) return;
    if (plan.question && !(await confirmBox(plan.question, { kind: 'warn' }))) return;
    this.player.stop();
    await api.call('apply.start', { paths: plan.paths });
  }
}

window.app = new App();
window.app.start();
