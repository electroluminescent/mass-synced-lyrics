// Electron main process: the window, the Python backend, and what the page may ask of the system.
'use strict';

const { app, BrowserWindow, ipcMain, dialog, shell, clipboard, Menu, nativeTheme, screen } = require('electron');
const fs = require('fs');
const path = require('path');
const { Backend } = require('./backend');

const TITLE = 'Mass Synced Lyrics';
let win = null;
let backend = null;
let quitting = false;
let backendLog = '';

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
  });
}

if (process.platform === 'win32') app.setAppUserModelId('com.electroluminescent.masssyncedlyrics');

// ---------------------------------------------------------------- window position, remembered
const statePath = () => path.join(app.getPath('userData'), 'window.json');

function loadWindowState() {
  try {
    const s = JSON.parse(fs.readFileSync(statePath(), 'utf-8'));
    const area = screen.getDisplayMatching(s).workArea;
    if (s.width >= 900 && s.height >= 600 && s.x + 100 < area.x + area.width && s.y + 50 < area.y + area.height) return s;
  } catch { /* first start */ }
  return { width: 1500, height: 960, maximized: false };
}

function saveWindowState() {
  if (!win || win.isDestroyed()) return;
  try {
    const b = win.getNormalBounds();
    fs.mkdirSync(path.dirname(statePath()), { recursive: true });
    fs.writeFileSync(statePath(), JSON.stringify({ ...b, maximized: win.isMaximized() }));
  } catch { /* not important */ }
}

// ---------------------------------------------------------------- backend
function send(channel, ...args) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, ...args);
}

function startBackend() {
  backend = new Backend(app, (name, data) => send('backend-event', name, data), (code, why) => {
    backendLog = why || '';
    if (!quitting) send('backend-exit', { code, log: backendLog });
  });
  backend.start();
}

function createWindow() {
  const st = loadWindowState();
  win = new BrowserWindow({
    title: TITLE,
    x: st.x, y: st.y, width: st.width, height: st.height,
    minWidth: 1100, minHeight: 740,
    show: false,
    backgroundColor: nativeTheme.shouldUseDarkColors ? '#1c1c1c' : '#fafafa',
    icon: path.join(__dirname, 'renderer', 'icon.png'),
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
      backgroundThrottling: false,
    },
  });
  if (st.maximized) win.maximize();
  win.once('ready-to-show', () => {
    if (process.env.MSL_HIDDEN) {            // tests: drawn and animated like a shown window, but off screen
      win.setSkipTaskbar(true);
      win.setPosition(-30000, -30000);
      win.showInactive();
    } else win.show();
  });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (e) => e.preventDefault());
  win.on('close', async (e) => {
    if (quitting) return;
    e.preventDefault();
    let busy = { finding: false };
    try { busy = await backend.call('app.busy'); } catch { /* gone */ }
    if (busy.finding) {
      const r = await dialog.showMessageBox(win, { type: 'question', buttons: ['Stop and quit', 'Cancel'],
        defaultId: 1, cancelId: 1, title: TITLE, message: 'A run is in progress. Stop it and quit?' });
      if (r.response !== 0) return;
    }
    saveWindowState();
    quitting = true;
    send('closing');
    await backend.stop();
    win.destroy();
    app.quit();
  });
  for (const ev of ['resize', 'move']) win.on(ev, () => { clearTimeout(win._st); win._st = setTimeout(saveWindowState, 500); });
}

// ---------------------------------------------------------------- what the page may ask for
ipcMain.handle('rpc', (_e, method, params) => backend.call(method, params));

ipcMain.handle('restart-backend', () => {
  if (backend && !backend.exited) return true;
  startBackend();
  return true;
});

ipcMain.handle('backend-log', () => backendLog);

const AUDIO_EXT = new Set(['.mp3', '.flac', '.ogg', '.oga', '.opus', '.wav', '.m4a', '.mp4', '.aac', '.aiff', '.aif',
  '.wma', '.wv', '.ape', '.mpc', '.dsf', '.alac', '.webm', '.mka']);

ipcMain.handle('read-audio', async (_e, file) => {
  // the song in the player (or the backend's WAV copy of it); nothing else is read for the page
  if (typeof file !== 'string' || !AUDIO_EXT.has(path.extname(file).toLowerCase())) throw new Error('not an audio file');
  const buf = await fs.promises.readFile(file);
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
});

ipcMain.handle('pick-folder', async (_e, title, start) => {
  const r = await dialog.showOpenDialog(win, { title, defaultPath: start || undefined, properties: ['openDirectory'] });
  return r.canceled ? null : r.filePaths[0];
});

ipcMain.handle('pick-files', async (_e, title, filters, multi) => {
  const r = await dialog.showOpenDialog(win, { title, filters,
    properties: multi ? ['openFile', 'multiSelections'] : ['openFile'] });
  return r.canceled ? [] : r.filePaths;
});

ipcMain.handle('open-path', async (_e, p) => {
  const err = await shell.openPath(p);
  if (err) throw new Error(err);
  return true;
});

ipcMain.handle('show-item', (_e, p) => { shell.showItemInFolder(p); return true; });

ipcMain.handle('clipboard-write', (_e, text) => { clipboard.writeText(String(text)); return true; });

ipcMain.handle('set-theme', (_e, mode) => {
  nativeTheme.themeSource = mode === 'dark' ? 'dark' : 'light';
  if (win) win.setBackgroundColor(mode === 'dark' ? '#1c1c1c' : '#fafafa');
  return true;
});

ipcMain.handle('devtools', () => { if (win) win.webContents.toggleDevTools(); });

app.whenReady().then(() => {
  Menu.setApplicationMenu(null);
  startBackend();
  createWindow();
});

app.on('window-all-closed', () => app.quit());
app.on('before-quit', () => { if (backend && !backend.exited && !quitting) { quitting = true; backend.stop(3000); } });
