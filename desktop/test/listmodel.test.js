// node --test test/listmodel.test.js : the song list's columns, colours, filters and grouping.
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const { pathToFileURL } = require('url');

const load = () => import(pathToFileURL(path.join(__dirname, '..', 'renderer', 'listmodel.js')).href);

const blank = (o) => ({ status: '', source: '', artist: '', title: '', local: '', online: '', choice: '', album: '',
  conf_local: null, conf_online: null, fit_local: null, fit_online: null, op_find: '', op_align: '', op_save: '',
  hand: false, level: '', check: false, ...o });

const opts = (o) => ({ filter: 'All', onlyFound: false, hideSaved: false, hideLow: false, minMatch: 0, minFit: 0,
  search: '', confT: 95, fitT: 80, view: 'artist', root: '/music', ...o });

test('columns and colours follow what will be written', async () => {
  const m = await load();
  const found = blank({ status: 'proposed', local: 'sidecar', online: 'found', choice: 'local', conf_local: 100, fit_local: 95 });
  assert.equal(m.rowTag(found), 'c_found');
  assert.deepEqual([m.rowValues(found).match, m.rowValues(found).fit, m.rowValues(found).choice], ['100%', '95%', '↻ Local']);
  assert.equal(m.rowTag(blank({ status: 'proposed', choice: 'keep' })), 'c_keep');
  assert.equal(m.rowTag(blank({ status: 'aligned', choice: 'online' })), 'c_aligned');
  assert.equal(m.rowTag(blank({ status: 'chosen', choice: 'custom' })), 'c_edited');
  assert.equal(m.rowTag(blank({ status: 'embedded' })), 'c_saved');
  assert.equal(m.rowTag(blank({ status: 'already_embedded' })), 'c_keep');
  assert.equal(m.rowTag(blank({ status: 'proposed', choice: 'online', check: true })), 'c_check');
  assert.equal(m.rowTag(blank({ status: 'error' })), 'c_problem');
  assert.equal(m.rowValues(blank({ status: 'searching', op_find: 'busy' })).status, 'Looking up…');
  assert.equal(m.rowValues(blank({ op_align: 'busy', align_pct: 37 })).align, '↻ 37%');
  assert.equal(m.rowValues(blank({ op_align: 'done' })).align, '⚡✓');
  assert.equal(m.rowValues(blank({ op_align: 'partial' })).align, '⚡≈');
  assert.equal(m.rowValues(blank({ choice: 'custom', hand: true })).align, '✎');
  assert.equal(m.rowValues(blank({ choice: 'custom' })).match, 'yours');
  assert.equal(m.rowValues(blank({ status: 'aligned' })).status, '⚡ Aligned');
});

test('filters: lyrics found, hide saved, hide low confidence, score minimums, search, busy always shown', async () => {
  const m = await load();
  const rows = {
    '/music/a.flac': blank({ status: 'proposed', online: 'found', choice: 'online', conf_online: 100, fit_online: 100, title: 'Exact' }),
    '/music/b.flac': blank({ status: 'proposed', online: 'found', choice: 'online', conf_online: 88, fit_online: 60, title: 'Loose' }),
    '/music/c.flac': blank({ status: 'not_found', online: 'not found', choice: 'keep', title: 'Missing' }),
    '/music/d.flac': blank({ status: 'embedded', online: 'found', choice: 'online', conf_online: 100, title: 'Saved' }),
    '/music/e.flac': blank({ status: 'searching', op_find: 'busy' }),
  };
  const shown = (o) => Object.keys(rows).filter((p) => m.matches(p, rows[p], opts(o))).map((p) => m.basename(p));
  assert.deepEqual(shown({ onlyFound: true }), ['a.flac', 'b.flac', 'd.flac', 'e.flac']);
  assert.deepEqual(shown({ onlyFound: true, hideLow: true }), ['a.flac', 'd.flac', 'e.flac']);
  assert.deepEqual(shown({ hideSaved: true }), ['a.flac', 'b.flac', 'c.flac', 'e.flac']);
  assert.deepEqual(shown({ minFit: 80 }), ['a.flac', 'c.flac', 'd.flac', 'e.flac']);
  assert.deepEqual(shown({ minMatch: 90 }), ['a.flac', 'c.flac', 'd.flac', 'e.flac']);
  assert.deepEqual(shown({ search: 'loose' }), ['b.flac', 'e.flac']);
  assert.deepEqual(shown({ filter: 'Needs attention' }), ['c.flac', 'e.flac']);
  assert.deepEqual(shown({ filter: 'Saved' }), ['d.flac', 'e.flac']);
});

test('grouping by artist and album, and by folders, with totals', async () => {
  const m = await load();
  const rows = {};
  [[95, 100], [60, 100], [85, 88]].forEach(([fit, conf], i) => {
    rows[`/music/Band/Album ${i % 2}/${i}.flac`] = blank({ status: 'proposed', source: 'lrclib', artist: 'Band',
      title: `Song ${i}`, online: 'found', choice: 'online', album: `Album ${i % 2}`, conf_online: conf, fit_online: fit });
  });
  const t = m.buildTree(rows, opts({ confT: 95, fitT: 80 }));
  assert.equal(t.roots.length, 1);
  const artist = t.nodes.get(t.roots[0]);
  assert.equal(artist.label, 'Band');
  const v = m.groupValues(artist.stats);
  assert.deepEqual([v.status, v.fit, v.match, v.online], ['3 songs', '1 poor', '1 low', '3 found']);
  const album0 = t.nodes.get(artist.children[0]);
  assert.equal(album0.label, 'Album 0');
  assert.equal(m.commonFolder(album0.stats.songs), '/music/Band/Album 0');
  assert.equal(m.commonFolder(artist.stats.songs), '/music/Band');
  // folders: music folder ▸ Band ▸ Album n ▸ song
  const f = m.buildTree(rows, opts({ view: 'folder' }));
  assert.equal(f.roots.length, 1);
  const top = f.nodes.get(f.roots[0]);
  assert.equal(top.label, 'music');
  assert.equal(m.groupValues(top.stats).status, '3 songs');
  const band = f.nodes.get(top.children[0]);
  const alb = f.nodes.get(band.children[0]);
  assert.equal(alb.label, 'Album 0');
  assert.equal(alb.folder, '/music/Band/Album 0');
  // the list order and Up/Down order
  const flat = m.flatten(t, (n) => true).map((n) => n.id);
  assert.equal(flat.length, 6);
  assert.deepEqual(m.songOrder(t), ['/music/Band/Album 0/0.flac', '/music/Band/Album 0/2.flac', '/music/Band/Album 1/1.flac']);
});

test('Windows paths: folders, case-insensitive groups, drive outside the music folder', async () => {
  const m = await load();
  assert.deepEqual(m.folderChain('C:\\Music\\Band\\a.flac', 'C:\\Music'), [['Music', 'C:\\Music'], ['Band', 'C:\\Music\\Band']]);
  assert.deepEqual(m.folderChain('D:\\Other\\a.flac', 'C:\\Music'), [['D:\\', 'D:\\'], ['Other', 'D:\\Other']]);
  assert.deepEqual(m.folderChain('\\\\?\\C:\\Music\\x.flac', 'c:\\music\\'), [['music', 'c:\\music']]);
  assert.equal(m.displayPath('\\\\?\\UNC\\srv\\share\\a.flac'), '\\\\srv\\share\\a.flac');
  assert.equal(m.basename('C:\\Music\\Band\\a.flac'), 'a.flac');
  assert.equal(m.dirname('C:\\a.flac'), 'C:\\');
  assert.deepEqual([0, 2, 3, 82, 87.5, 99, 140].map((x) => m.snap5(x)), [0, 0, 5, 80, 90, 100, 100]);
});
