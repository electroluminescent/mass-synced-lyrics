// The song list's logic (no DOM): what each column says, row colours, filters and grouping.
// Kept separate so it can be tested with node (test/listmodel.test.js).

export const GPU = '⚡';
export const HAND = '✎';

export const FILTERS = {
  'All': null,
  'Needs attention': new Set(['ambiguous', 'not_found', 'no_synced', 'no_metadata', 'error', 'rejected', 'unsupported']),
  'Found (to apply)': new Set(['proposed', 'chosen', 'locked', 'aligned']),
  'Saved': new Set(['embedded', 'lrc_written', 'already_embedded', 'unchanged']),
  'Changed': new Set(['embedded', 'lrc_written', 'locked', 'chosen']),
  'Per-file choices': new Set(['locked', 'chosen']),
  'Already had lyrics': new Set(['has_synced', 'already_embedded']),
};

export const STATUS_LABELS = {
  embedded: 'Embedded', already_embedded: 'Already embedded', has_synced: 'Already had synced lyrics',
  instrumental: 'Instrumental', no_synced: 'Only plain lyrics online (left alone)', not_found: 'Not found',
  no_metadata: 'No title/artist', ambiguous: 'Ambiguous .lrc match', rejected: 'Lyrics rejected',
  unsupported: 'Unsupported file', error: 'Error', cached: 'Skipped (done in earlier run)',
  aligned: 'Aligned on the GPU (not written yet)', locked: 'Per-file choice kept', proposed: 'Lyrics found (not written yet)',
  lrc_written: '.lrc saved', unchanged: 'Unchanged', chosen: 'Your choice (not written yet)', restored: 'Original restored',
};

export const SHORT_STATUS = {
  proposed: 'Found', embedded: 'Saved', lrc_written: '.lrc saved', already_embedded: 'Up to date',
  has_synced: 'Has lyrics', not_found: 'Not found', no_synced: 'Plain only online', instrumental: 'Instrumental',
  no_metadata: 'No tags', ambiguous: 'Ambiguous', error: 'Error', unsupported: 'Unsupported', locked: 'Your choice',
  chosen: 'Your choice', unchanged: 'Unchanged', rejected: 'Rejected', restored: 'Restored', aligned: GPU + ' Aligned',
  searching: 'Looking up…', '': '',
};
export const CHOICE_TEXT = { local: 'Local', online: 'Online', keep: "Don't change", custom: 'Custom',
  remove: 'Remove lyrics', '': '' };
export const DONE_STATUSES = new Set(['embedded', 'lrc_written', 'already_embedded', 'unchanged']);
export const SAVED_STATUSES = new Set(['embedded', 'lrc_written']);
export const PROBLEM_STATUSES = new Set(['error', 'ambiguous', 'unsupported', 'rejected']);
export const FOUND_STATUSES = new Set(['proposed', 'chosen', 'locked', 'aligned']);

export const LEGEND = [
  ['c_found', 'Found', 'Will write the lyrics as found, with their found timing.'],
  ['c_aligned', GPU + ' Aligned', "Will write the lyrics with the GPU's timing (the Aligned timeline)."],
  ['c_edited', HAND + ' Edited', 'Will write your Edited timeline (times or lines you changed).'],
  ['c_keep', 'Not changed', 'Not changed: nothing will be written (kept as it is, or nothing found).'],
  ['c_saved', 'Saved', 'Saved: this app wrote the lyrics.'],
  ['c_check', 'Check', 'Check it: the GPU heard too little to be sure, or its timing is far from the found one.'],
  ['c_problem', 'Problem', 'Needs a look: error, ambiguous .lrc match, or unsupported file.'],
];

export const COLUMN_HINTS = {
  name: 'Songs by Artist ▸ Album or by folder (switch with the box above). Ctrl/Shift+click selects several songs; ' +
        'a group row counts as all its songs. Right-click for Save now, sources, Reset, Restore original, opening the ' +
        "file's location and Remove from the list (also the Delete key).",
  status: 'What happened to the song so far.',
  local: 'Lyrics found locally: a sidecar .lrc beside the song, your collection, or embedded lyrics.',
  online: 'Result of the LRCLIB lookup: found, plain lyrics only, instrumental, or not found.',
  choice: 'What Apply will do. Click a Choice cell to cycle Local → Online → Don\'t change. On an artist/album row ' +
          'or a multi-selection it changes all of them.',
  match: 'How sure it is that the lyrics in Choice belong to this song (100% = exact title, artist, album and length). ' +
         'Click the header to filter by it.',
  fit: 'How well the lyrics in Choice fit this recording: its length, lines past the end, how the lines spread over ' +
       'the song and, once the audio has been checked, lines that start on silence. Click the header to filter by it.',
  find: 'Find lyrics: ↻ being looked up now · ✓ done · ✗ error.',
  align: 'Alignment. ⚡ = on the GPU: ↻ 42% aligning now · ⚡✓ re-timed · ⚡≈ partly: fewer words heard than Aligned ' +
         'lyrics must match, so only the lines it was sure of were placed (check it) · ⚡– heard too little, kept as ' +
         'found · ⚡✗ error. ✎ = timing you set yourself in the player. Empty = not aligned.',
  save: 'Saving (Apply / Save now): ↻ writing now · ✓ written · – already up to date · ✗ error.',
};

export const COLUMNS = [
  ['status', 84, 'Status'], ['find', 42, 'Find'], ['align', 64, 'Align'], ['save', 42, 'Save'],
  ['choice', 104, 'Choice ↻'], ['match', 66, 'Match'], ['fit', 58, 'Fit'], ['local', 64, 'Local'], ['online', 74, 'Online'],
];
export const OPS = ['find', 'align', 'save'];
export const OP_MARK = { busy: '↻', done: '✓', skip: '–', fail: '✗', partial: '✓', '': '' };
export const ALIGN_MARK = { busy: '↻', done: GPU + '✓', partial: GPU + '≈', skip: GPU + '–', fail: GPU + '✗', '': '' };
export const VIEWS = { artist: 'Artist ▸ Album', folder: 'Folders' };
export const VIEW_HEADINGS = { artist: 'Artist ▸ Album ▸ Song', folder: 'Folder ▸ Song' };

// ------------------------------------------------------------------ rows
export function rowLevel(row) {
  if (row.choice === 'keep' || row.choice === 'remove') return 'keep';
  if (['found', 'aligned', 'edited', 'keep'].includes(row.level)) return row.level;
  if (row.choice === 'custom') return 'edited';
  if (row.status === 'aligned' || row.op_align === 'done' || row.op_align === 'partial') return 'aligned';
  if ((row.choice === 'local' || row.choice === 'online') && FOUND_STATUSES.has(row.status)) return 'found';
  return 'keep';
}

function byChoice(r, prefix) {
  const c = r.choice;
  if (c === 'local') return r[prefix + 'local'] ?? null;
  if (c === 'online') return r[prefix + 'online'] ?? null;
  if (c === 'keep' || c === 'custom' || c === 'remove') return null;
  const vals = [r[prefix + 'local'], r[prefix + 'online']].filter((v) => v !== null && v !== undefined);
  return vals.length ? Math.max(...vals) : null;
}
export const rowConfidence = (r) => byChoice(r, 'conf_');
export const rowFit = (r) => byChoice(r, 'fit_');

export function rowTag(row) {
  if (PROBLEM_STATUSES.has(row.status)) return 'c_problem';
  if (SAVED_STATUSES.has(row.status)) return 'c_saved';
  if (DONE_STATUSES.has(row.status)) return 'c_keep';
  if (row.check) return 'c_check';
  return { found: 'c_found', aligned: 'c_aligned', edited: 'c_edited' }[rowLevel(row)] || 'c_keep';
}

export const isBusy = (r) => OPS.some((op) => r['op_' + op] === 'busy');

export function hasFoundLyrics(r) {
  return Boolean(r.local || r.online === 'found' || r.choice === 'custom' || r.choice === 'remove' ||
    FOUND_STATUSES.has(r.status) || SAVED_STATUSES.has(r.status));
}

export function fmtPct(v) { return v === null || v === undefined ? '—' : `${Math.round(v)}%`; }

export function rowValues(r) {
  const status = SHORT_STATUS[r.status] ?? STATUS_LABELS[r.status] ?? r.status;
  const choice = CHOICE_TEXT[r.choice] ?? r.choice;
  let match, fit;
  if (r.choice === 'custom' || r.choice === 'remove') { match = fit = 'yours'; } else {
    match = fmtPct(rowConfidence(r));
    fit = fmtPct(rowFit(r));
  }
  const v = { status, local: r.local || '—', online: r.online || '—', choice: choice ? `↻ ${choice}` : '', match, fit };
  for (const op of OPS) v[op] = OP_MARK[r['op_' + op] || ''] ?? '';
  const st = r.op_align || '';
  v.align = st === 'busy' && r.align_pct !== null && r.align_pct !== undefined ? `↻ ${Math.round(r.align_pct)}%`
    : (ALIGN_MARK[st] ?? '');
  if (!st && r.status === 'aligned') v.align = ALIGN_MARK.done;
  if (r.hand && r.choice === 'custom') v.align += HAND;
  return v;
}

export function displayPath(p) {
  if (p.startsWith('\\\\?\\UNC\\')) return '\\\\' + p.slice(8);
  if (p.startsWith('\\\\?\\')) return p.slice(4);
  return p;
}

const sepOf = (p) => (p.includes('\\') ? '\\' : '/');
const casefold = (s) => s.toLocaleLowerCase();
export function dirname(p) {
  const s = sepOf(p);
  const i = p.lastIndexOf(s);
  if (i < 0) return '';
  if (i === 0) return s;
  if (s === '\\' && i === 2 && p[1] === ':') return p.slice(0, 3);
  return p.slice(0, i);
}
export function basename(p) {
  const s = sepOf(p);
  const t = p.endsWith(s) && p.length > 1 ? p.slice(0, -1) : p;
  return t.slice(t.lastIndexOf(s) + 1);
}

export function rowName(path, r) {
  return r.title || basename(displayPath(path));
}

export function matches(path, r, f) {
  if (isBusy(r)) return true;                                   // being worked on right now: always shown
  const statuses = FILTERS[f.filter] || null;
  if (statuses && !statuses.has(r.status)) return false;
  if (f.onlyFound && !hasFoundLyrics(r)) return false;
  if (f.hideSaved && DONE_STATUSES.has(r.status)) return false;
  if (f.hideLow && !DONE_STATUSES.has(r.status)) {
    const c = rowConfidence(r), ft = rowFit(r);
    if ((c !== null && c < f.confT) || (ft !== null && ft < f.fitT)) return false;
  }
  for (const [n, value] of [[f.minMatch, rowConfidence(r)], [f.minFit, rowFit(r)]]) {
    if (n && value !== null && value < n) return false;
  }
  const q = (f.search || '').trim().toLocaleLowerCase();
  return !q || `${r.artist} ${r.album || ''} ${r.title} ${path}`.toLocaleLowerCase().includes(q);
}

// ------------------------------------------------------------------ grouping
export function groupKeys(path, r) {
  const folder = dirname(displayPath(path));
  const artist = r.artist || basename(dirname(folder)) || '(no artist)';
  const album = r.album || basename(folder) || '(no album)';
  return [artist, album];
}

export function folderChain(path, root) {
  const folder = dirname(displayPath(path));
  const sep = sepOf(folder || root || '/');
  const nc = (s) => (sep === '\\' ? casefold(s) : s);
  const trimmed = (root || '').replace(/[\\/]+$/, '');
  if (trimmed && nc(folder + sep).startsWith(nc(trimmed + sep))) {
    const chain = [[basename(trimmed) || trimmed, trimmed]];
    let cur = trimmed;
    const rel = folder.slice(trimmed.length).split(sep).filter(Boolean);
    for (const part of rel) { cur = cur + sep + part; chain.push([part, cur]); }
    return chain;
  }
  const parts = folder.split(sep).filter(Boolean);
  const chain = [];
  let cur = '';
  if (sep === '\\' && /^[A-Za-z]:$/.test(parts[0] || '')) { cur = parts.shift() + '\\'; chain.push([cur, cur]); } else if (sep === '/' && folder.startsWith('/')) { cur = '/'; chain.push(['/', '/']); } else if (folder.startsWith('\\\\')) {
    cur = '\\\\' + parts.shift(); chain.push([cur, cur]);
  }
  for (const part of parts) { cur = cur.endsWith(sep) || !cur ? cur + part : cur + sep + part; chain.push([part, cur]); }
  return chain.length ? chain : [[folder, folder]];
}

/** Build the grouped tree of the rows that pass the filters.
 *  Returns {nodes: Map(id → node), roots: [ids], shown: number}. Song nodes: {id, kind:'song', parent};
 *  group nodes: {id, kind:'group', label, depth, parent, children, folder?, stats}. */
export function buildTree(rows, opts) {
  const nodes = new Map();
  const roots = [];
  const many = Object.keys(rows).length > 600;
  let shown = 0;
  const addGroup = (id, label, parent, depth, folder) => {
    let g = nodes.get(id);
    if (!g) {
      g = { id, kind: 'group', label, parent, depth, children: [], folder: folder || '', open: !many || (opts.view === 'folder' && depth === 0) };
      nodes.set(id, g);
      if (parent) nodes.get(parent).children.push(id); else roots.push(id);
    }
    return g;
  };
  for (const [path, r] of Object.entries(rows)) {
    if (!matches(path, r, opts)) continue;
    let parent = '';
    if (opts.view === 'folder') {
      folderChain(path, opts.root).forEach(([label, folder], depth) => {
        const id = 'grp:dir:' + (folder.includes('\\') ? casefold(folder) : folder);
        addGroup(id, label, parent, depth, folder);
        parent = id;
      });
    } else {
      const [artist, album] = groupKeys(path, r);
      const a = 'grp:' + casefold(artist);
      const b = a + '\x1f' + casefold(album);
      addGroup(a, artist, '', 0);
      addGroup(b, album, a, 1);
      parent = b;
    }
    nodes.set(path, { id: path, kind: 'song', parent, depth: nodes.get(parent).depth + 1 });
    nodes.get(parent).children.push(path);
    shown++;
  }
  // totals, deepest groups first
  const groups = [...nodes.values()].filter((n) => n.kind === 'group').sort((x, y) => y.depth - x.depth);
  for (const g of groups) {
    const st = { count: 0, local: 0, online: 0, choices: new Set(), low: 0, poor: 0, done: { find: 0, align: 0, save: 0 },
      busy: { find: 0, align: 0, save: 0 }, songs: [] };
    for (const c of g.children) {
      const n = nodes.get(c);
      if (n.kind === 'group') {
        const s = n.stats;
        st.count += s.count; st.local += s.local; st.online += s.online; st.low += s.low; st.poor += s.poor;
        s.choices.forEach((x) => st.choices.add(x));
        for (const op of OPS) { st.done[op] += s.done[op]; st.busy[op] += s.busy[op]; }
        st.songs.push(...s.songs);
        continue;
      }
      const r = rows[c];
      st.count++;
      st.songs.push(c);
      if (r.local) st.local++;
      if (r.online === 'found') st.online++;
      st.choices.add(r.choice);
      const cv = rowConfidence(r), fv = rowFit(r);
      if (cv !== null && cv < opts.confT) st.low++;
      if (fv !== null && fv < opts.fitT) st.poor++;
      for (const op of OPS) {
        const v = r['op_' + op] || '';
        if (v === 'done' || v === 'partial') st.done[op]++;
        if (v === 'busy') st.busy[op]++;
      }
    }
    g.stats = st;
  }
  return { nodes, roots, shown };
}

export function groupValues(st) {
  const ch = st.choices.size === 1 ? (CHOICE_TEXT[[...st.choices][0]] ?? '') : 'mixed';
  const v = { status: `${st.count} song${st.count !== 1 ? 's' : ''}`, local: st.local ? `${st.local} local` : '—',
    online: st.online ? `${st.online} found` : '—', choice: ch ? `↻ ${ch}` : '', match: st.low ? `${st.low} low` : '',
    fit: st.poor ? `${st.poor} poor` : '' };
  for (const op of OPS) v[op] = st.busy[op] ? '↻' : (st.done[op] ? `${st.done[op]}✓` : '');
  return v;
}

/** The rows to draw, top to bottom: open groups show their children. */
export function flatten(tree, isOpen) {
  const out = [];
  const walk = (ids) => {
    for (const id of ids) {
      const n = tree.nodes.get(id);
      out.push(n);
      if (n.kind === 'group' && isOpen(n)) walk(n.children);
    }
  };
  walk(tree.roots);
  return out;
}

/** Every song, in list order, whether or not its group is open (for Up/Down between songs). */
export function songOrder(tree) {
  const out = [];
  const walk = (ids) => {
    for (const id of ids) {
      const n = tree.nodes.get(id);
      if (n.kind === 'group') walk(n.children); else out.push(id);
    }
  };
  walk(tree.roots);
  return out;
}

export function commonFolder(paths) {
  if (!paths.length) return '';
  const dirs = paths.map((p) => dirname(displayPath(p)));
  const sep = sepOf(dirs[0]);
  let parts = dirs[0].split(sep);
  for (const d of dirs.slice(1)) {
    const q = d.split(sep);
    let i = 0;
    while (i < parts.length && i < q.length && (sep === '\\' ? casefold(parts[i]) === casefold(q[i]) : parts[i] === q[i])) i++;
    parts = parts.slice(0, i);
  }
  const out = parts.join(sep);
  return out || dirs[0];
}

export function snap5(v, lo = 0, hi = 100) {
  const n = Math.round(Number(v) / 5) * 5;
  return Number.isFinite(n) ? Math.max(lo, Math.min(hi, n)) : lo;
}
