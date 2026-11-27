// End-to-end: the real window and the real backend, driven like a person would (Playwright).
//   node --test test/e2e.test.js        (needs Python with the app's requirements; MSL_PYTHON picks it)
// Screenshots go to test/shots/ (MSL_SHOTS=0 to skip).
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { _electron: electron } = require('playwright-core');

const ROOT = path.resolve(__dirname, '..', '..');
const PY = process.env.MSL_PYTHON || (process.platform === 'win32' ? 'py' : 'python3');
const PYARGS = process.platform === 'win32' && PY === 'py' ? ['-3.12'] : [];
const SHOTS = path.join(__dirname, 'shots');

function py(code) {
  return execFileSync(PY, [...PYARGS, '-c', code], { cwd: path.join(ROOT, 'tests'), encoding: 'utf-8',
    env: { ...process.env, PYTHONPATH: path.join(ROOT, 'src') + path.delimiter + path.join(ROOT, 'tests') } });
}

async function until(page, fn, arg, timeout = 20000) {
  await page.waitForFunction(fn, arg, { timeout, polling: 50 });
}

test('find, review, edit, play and apply in the real window', { timeout: 180000 }, async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'msl-e2e-'));
  const music = path.join(tmp, 'music');
  const song = path.join(music, 'Some Song.flac');
  py(`
import os, conftest
conftest.make_track(${JSON.stringify(song)}, "flac")
conftest.write_lrc(${JSON.stringify(path.join(music, 'Some Song.lrc'))}, "[ti:Some Song]\\n[00:01.00]First line here\\n[00:04.50]Second line\\n[00:08.20]Third line")
`);
  const env = { ...process.env, APPDATA: path.join(tmp, 'appdata'), XDG_CONFIG_HOME: path.join(tmp, 'appdata'),
    LOCALAPPDATA: path.join(tmp, 'la'), MSL_PYTHON: PY, MSL_HIDDEN: process.env.MSL_SHOW ? '' : '1' };
  if (process.platform === 'win32' && process.env.APPDATA && !process.env.PYTHONUSERBASE) {
    env.PYTHONUSERBASE = path.join(process.env.APPDATA, 'Python');   // packages installed with pip --user
  }
  fs.mkdirSync(path.join(tmp, 'appdata', 'mass-synced-lyrics'), { recursive: true });
  fs.writeFileSync(path.join(tmp, 'appdata', 'mass-synced-lyrics', 'settings.json'), JSON.stringify({
    music_folder: music, use_online: false, use_acoustid: false, lrc_action: 'keep', session_restore: 'fresh',
    state_db: path.join(tmp, 'state.sqlite3'), log_dir: path.join(tmp, 'logs') }));
  // MSL_APP_EXE: test a built app (its own backend inside) instead of the source tree
  const args = process.env.MSL_APP_EXE ? [] : [path.join(__dirname, '..')];
  if (process.platform === 'linux') args.unshift('--no-sandbox');
  const t0 = Date.now();
  const app = await electron.launch({ args, env, executablePath: process.env.MSL_APP_EXE || undefined });
  const page = await app.firstWindow();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  await page.setViewportSize({ width: 1500, height: 960 });
  const shot = async (name) => {
    if (process.env.MSL_SHOTS === '0') return;
    fs.mkdirSync(SHOTS, { recursive: true });
    await page.screenshot({ path: path.join(SHOTS, name + '.png') });
  };
  try {
    await until(page, () => window.app && window.app.list && document.title.startsWith('Mass Synced Lyrics'));
    console.log(`# window ready in ${Date.now() - t0} ms`);
    assert.equal(await page.inputValue('#find input.grow'), music);
    await shot('1-find');

    // Find lyrics → switches to Review & player with the song open
    await page.click('text=Find lyrics >> nth=1');
    await until(page, () => window.app.tab === 'review' && window.app.player.st && window.app.player.st.path.endsWith('Some Song.flac'));
    await until(page, () => window.app.player.audio.ready, null, 30000);
    const st = await page.evaluate(() => window.app.player.st);
    assert.deepEqual(st.new_lines.map((l) => l[0]), [1, 4.5, 8.2]);
    assert.deepEqual(st.checkpoints, [1, 8.2]);
    assert.ok(await page.isVisible('text=Apply: embed into 1 file(s)'));
    await page.waitForTimeout(300);
    await shot('2-review');

    // Space plays and pauses; position moves
    await page.click('#review .ptitle');
    await page.keyboard.press(' ');
    await page.waitForTimeout(400);
    const p1 = await page.evaluate(() => window.app.player.pos());
    assert.ok(await page.evaluate(() => window.app.player.audio.playing));
    assert.ok(p1 > 0.1, `position ${p1}`);
    await page.keyboard.press(' ');
    assert.ok(!(await page.evaluate(() => window.app.player.audio.playing)));
    // key 2 plays checkpoint 2 (8.2 s ± 1 s)
    await page.keyboard.press('2');
    await page.waitForTimeout(100);
    const cp = await page.evaluate(() => window.app.player.cpRange);
    assert.ok(cp && Math.abs(cp[0] - 7.2) < 0.01, JSON.stringify(cp));
    await page.keyboard.press(' ');

    // type a timestamp: click it, type, Enter
    await page.click('#review .panes .ln[data-i="1"] .ts');
    await page.keyboard.press('Control+a');
    await page.keyboard.type('00:05.00');
    await page.keyboard.press('Enter');
    await until(page, () => window.app.player.st.new_lines[1][0] === 5);
    assert.equal(await page.evaluate(() => window.app.player.st.new_lines[1][3]), 'edited');

    // word timing: give line 1 its own word times, then drag a word mark in the Edited timeline
    await page.click('#review .panes .ln[data-i="0"]', { button: 'right' });
    await page.click('text=Time this line\'s words');
    await until(page, () => (window.app.player.lanes.edit[0].words || []).length === 3);
    await page.evaluate(() => { window.app.player.audio.seek(1.5); });
    await page.waitForTimeout(400);
    const geo = await page.evaluate(() => {
      const p = window.app.player, b = p.lanes.edit[0];
      const r = p.sc.getBoundingClientRect();
      return { x: r.left + p.scX(b.words[1][0]), y: r.top + 121 + 8, to: r.left + p.scX(b.words[1][0] + 0.3), w0: b.words[1][0] };
    });
    await page.mouse.move(geo.x, geo.y);
    await page.mouse.down();
    await page.mouse.move(geo.to, geo.y, { steps: 6 });
    await page.mouse.up();
    await until(page, (w0) => Math.abs(window.app.player.lanes.edit[0].words[1][0] - (w0 + 0.3)) < 0.06, geo.w0);
    assert.ok((await page.evaluate(() => window.app.player.st.word_rows))['0']);
    await shot('3-words');

    // drag a lyric in Edited (select, then move it later)
    const mv = await page.evaluate(() => {
      const p = window.app.player, r = p.ov.getBoundingClientRect();
      return { x: r.left + p.ovX(9.0), y: r.top + 80 + 5, to: r.left + p.ovX(9.0) + 10 };
    });
    await page.mouse.click(mv.x, mv.y);
    await page.mouse.move(mv.x, mv.y);
    await page.mouse.down();
    await page.mouse.move(mv.to, mv.y, { steps: 5 });
    await page.mouse.up();
    await until(page, () => window.app.player.st.new_lines[2][0] > 8.25);

    // the choice switches to Edited by itself; Apply writes it
    await until(page, () => window.app.player.st.choice.level === 'edited', null, 10000);
    await page.click('#applybar button.accent');
    await page.click('#modal-back button.accent');
    await until(page, () => /Done: 1 written/.test(document.querySelector('#applybar').textContent), null, 30000);
    const written = py(`
from mass_synced_lyrics.tags import TagFile
print(TagFile(${JSON.stringify(song)}).read().lyrics[0])`);
    assert.match(written, /\[00:05\.00\]Second line/);
    assert.match(written, /\[00:01\.00\]<00:01\.00>First <00:02\.3[0-9]>line/);

    // dark mode
    await page.click('#dark-switch');
    await until(page, () => document.documentElement.dataset.theme === 'dark');
    await page.waitForTimeout(200);
    await shot('4-review-dark');
    await page.click('#tab-find');
    await shot('5-find-dark');
    assert.deepEqual(errors, []);
  } finally {
    await app.close();
  }
  // the session was saved on close
  assert.ok(fs.existsSync(path.join(tmp, 'appdata', 'mass-synced-lyrics', 'session.json')));
});
