// Playback for the player (Web Audio): sample-accurate position, instant seeking, logarithmic volume.

export function volumeGain(level) {
  // 0–100 on a decibel scale: 100 = 0 dB, 50 = −20 dB, 25 = −30 dB … 0 = silent
  level = Math.max(0, Math.min(100, Number(level) || 0));
  return level <= 0 ? 0 : 10 ** (-40 * (1 - level / 100) / 20);
}

export class AudioPlayer {
  constructor() {
    this.ctx = null;
    this.gain = null;
    this.buffer = null;
    this.source = null;
    this.playing = false;
    this.offset = 0;
    this.startedAt = 0;
    this.level = 80;
    this.duration = 0;          // the song's length (from the backend while the audio loads)
  }

  _ensure() {
    if (this.ctx) return;
    this.ctx = new AudioContext({ latencyHint: 'interactive' });
    this.gain = this.ctx.createGain();
    this.gain.gain.value = volumeGain(this.level);
    this.gain.connect(this.ctx.destination);
  }

  async load(arrayBuffer) {
    this._ensure();
    this.stop();
    this.buffer = await this.ctx.decodeAudioData(arrayBuffer);
    this.duration = this.buffer.duration;
    return this.duration;
  }

  unload(duration = 0) {
    this.stop();
    this.buffer = null;
    this.duration = duration;
  }

  get ready() { return this.buffer !== null; }

  _latency() {
    return this.ctx ? (this.ctx.outputLatency || 0) + (this.ctx.baseLatency || 0) : 0;
  }

  position() {
    if (!this.playing || !this.ctx) return this.offset;
    const t = this.offset + Math.max(0, this.ctx.currentTime - this.startedAt - this._latency());
    return Math.min(t, this.duration || t);
  }

  play() {
    if (!this.buffer || this.playing) return;
    this._ensure();
    if (this.ctx.state === 'suspended') this.ctx.resume();
    if (this.offset >= this.buffer.duration - 0.01) this.offset = 0;
    const src = this.ctx.createBufferSource();
    src.buffer = this.buffer;
    src.connect(this.gain);
    src.onended = () => {
      if (this.source !== src) return;            // stopped for a seek or a pause
      this.playing = false;
      this.offset = this.buffer ? this.buffer.duration : 0;
      this.source = null;
    };
    this.source = src;
    this.startedAt = this.ctx.currentTime;
    src.start(0, this.offset);
    this.playing = true;
  }

  pause() {
    if (!this.playing) return;
    this.offset = this.position();
    this._kill();
    this.playing = false;
  }

  stop() {
    this._kill();
    this.playing = false;
    this.offset = 0;
  }

  _kill() {
    const s = this.source;
    this.source = null;
    if (s) { try { s.stop(); } catch { /* not started */ } s.disconnect(); }
  }

  toggle() { this.playing ? this.pause() : this.play(); }

  seek(t) {
    t = Math.max(0, Math.min(Number(t) || 0, this.duration || 0));
    if (this.playing) {
      this._kill();
      this.playing = false;
      this.offset = t;
      this.play();
    } else {
      this.offset = t;
    }
  }

  setVolume(level) {
    this.level = Math.max(0, Math.min(100, Number(level) || 0));
    if (this.gain) this.gain.gain.setTargetAtTime(volumeGain(this.level), this.ctx.currentTime, 0.01);
  }
}
