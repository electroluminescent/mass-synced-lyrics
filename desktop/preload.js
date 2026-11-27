// The page's window to the rest of the app (contextIsolation: only these functions exist in the page).
'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
  call: (method, params) => ipcRenderer.invoke('rpc', method, params),
  on: (fn) => ipcRenderer.on('backend-event', (_e, name, data) => fn(name, data)),
  onExit: (fn) => ipcRenderer.on('backend-exit', (_e, info) => fn(info)),
  onClosing: (fn) => ipcRenderer.on('closing', () => fn()),
  restartBackend: () => ipcRenderer.invoke('restart-backend'),
  backendLog: () => ipcRenderer.invoke('backend-log'),
  readAudio: (file) => ipcRenderer.invoke('read-audio', file),
  pickFolder: (title, start) => ipcRenderer.invoke('pick-folder', title, start),
  pickFiles: (title, filters, multi) => ipcRenderer.invoke('pick-files', title, filters, multi),
  openPath: (p) => ipcRenderer.invoke('open-path', p),
  showItem: (p) => ipcRenderer.invoke('show-item', p),
  copy: (text) => ipcRenderer.invoke('clipboard-write', text),
  setTheme: (mode) => ipcRenderer.invoke('set-theme', mode),
  devtools: () => ipcRenderer.invoke('devtools'),
  platform: process.platform,
});
