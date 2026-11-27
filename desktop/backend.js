// The Python backend: started with the window, spoken to with one JSON object per line on stdin/stdout.
'use strict';

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const readline = require('readline');

function backendCommand(app) {
  // MSL_BACKEND overrides everything (tests): a JSON array [command, ...args]
  if (process.env.MSL_BACKEND) {
    const cmd = JSON.parse(process.env.MSL_BACKEND);
    return { cmd: cmd[0], args: cmd.slice(1), cwd: undefined, env: {} };
  }
  if (app.isPackaged) {
    const dir = path.join(process.resourcesPath, 'backend');
    const exe = path.join(dir, process.platform === 'win32' ? 'msl-backend.exe' : 'msl-backend');
    return { cmd: exe, args: [], cwd: dir, env: {} };
  }
  // development: run the package from the source tree next to this folder
  const src = path.resolve(__dirname, '..', 'src');
  const py = process.env.MSL_PYTHON || (process.platform === 'win32' ? 'py' : 'python3');
  const pre = process.platform === 'win32' && py === 'py' ? ['-3.12'] : [];
  return { cmd: py, args: [...pre, '-m', 'mass_synced_lyrics', '--backend'], cwd: src,
           env: { PYTHONPATH: src } };
}

class Backend {
  constructor(app, onEvent, onExit) {
    this.app = app;
    this.onEvent = onEvent;
    this.onExit = onExit;
    this.nextId = 1;
    this.pending = new Map();
    this.proc = null;
    this.stderr = [];
    this.exited = false;
  }

  start() {
    const { cmd, args, cwd, env } = backendCommand(this.app);
    this.proc = spawn(cmd, args, {
      cwd, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1', ...env },
    });
    this.proc.stdin.setDefaultEncoding('utf-8');
    const rl = readline.createInterface({ input: this.proc.stdout, crlfDelay: Infinity });
    rl.on('line', (line) => this._line(line));
    this.proc.stderr.on('data', (d) => {
      const s = d.toString('utf-8');
      this.stderr.push(s);
      if (this.stderr.length > 200) this.stderr.splice(0, this.stderr.length - 200);
      if (process.env.MSL_DEBUG) process.stderr.write(s);
    });
    this.proc.on('error', (err) => this._exit(-1, String(err)));
    this.proc.on('exit', (code) => this._exit(code, ''));
  }

  _exit(code, why) {
    if (this.exited) return;
    this.exited = true;
    for (const { reject } of this.pending.values()) reject(new Error('The backend stopped.'));
    this.pending.clear();
    this.onExit(code, why || this.stderr.join('').slice(-4000));
  }

  _line(line) {
    let msg;
    try { msg = JSON.parse(line); } catch { return; }
    if (msg.event !== undefined) { this.onEvent(msg.event, msg.data); return; }
    const p = this.pending.get(msg.id);
    if (!p) return;
    this.pending.delete(msg.id);
    if (msg.error !== undefined) p.reject(new Error(msg.error));
    else p.resolve(msg.result);
  }

  call(method, params) {
    if (this.exited || !this.proc) return Promise.reject(new Error('The backend is not running.'));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.proc.stdin.write(JSON.stringify({ id, method, params: params || {} }) + '\n');
    });
  }

  async stop(timeoutMs = 15000) {
    if (this.exited || !this.proc) return;
    const done = new Promise((r) => this.proc.once('exit', r));
    try { await Promise.race([this.call('app.quit'), new Promise((r) => setTimeout(r, timeoutMs))]); } catch { /* gone */ }
    try { this.proc.stdin.end(); } catch { /* closed */ }
    await Promise.race([done, new Promise((r) => setTimeout(r, 3000))]);
    if (!this.exited) { try { this.proc.kill(); } catch { /* gone */ } }
  }
}

module.exports = { Backend, backendCommand, fsExists: (p) => { try { return fs.existsSync(p); } catch { return false; } } };
