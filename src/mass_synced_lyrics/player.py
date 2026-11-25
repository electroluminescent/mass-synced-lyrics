"""Audio decoding (PyAV), waveform peaks and playback (sounddevice)."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np

RATE = 44100
PEAKS_PER_SECOND = 100


@dataclass
class AudioData:
    samples: np.ndarray       # int16, shape (frames, 2)
    rate: int
    peaks: np.ndarray         # float32 0..1, PEAKS_PER_SECOND per second

    @property
    def duration(self) -> float:
        return len(self.samples) / self.rate


def decode(path: str, cancel: threading.Event | None = None) -> AudioData:
    import av

    chunks = []
    with av.open(path) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="stereo", rate=RATE)
        for frame in container.decode(stream):
            if cancel is not None and cancel.is_set():
                raise InterruptedError()
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1, 2))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1, 2))
    samples = np.concatenate(chunks) if chunks else np.zeros((0, 2), dtype=np.int16)
    return AudioData(samples.astype(np.int16, copy=False), RATE, compute_peaks(samples, RATE))


def compute_peaks(samples: np.ndarray, rate: int) -> np.ndarray:
    if len(samples) == 0:
        return np.zeros(1, dtype=np.float32)
    mono = np.abs(samples.astype(np.int32).sum(axis=1)) / 2
    step = max(1, rate // PEAKS_PER_SECOND)
    n = int(np.ceil(len(mono) / step))
    padded = np.zeros(n * step, dtype=np.float32)
    padded[: len(mono)] = mono
    peaks = padded.reshape(n, step).max(axis=1)
    top = float(peaks.max()) or 1.0
    return (peaks / top).astype(np.float32)


def envelope(peaks: np.ndarray, start_s: float, end_s: float, width: int) -> np.ndarray:
    """Peak value per pixel column for the time window [start_s, end_s)."""
    width = max(1, int(width))
    edges = np.linspace(start_s * PEAKS_PER_SECOND, end_s * PEAKS_PER_SECOND, width + 1)
    lo = np.floor(edges[:-1]).astype(np.int64)
    out = np.zeros(width, dtype=np.float32)
    valid = np.nonzero((lo >= 0) & (lo < len(peaks)))[0]
    if len(valid) == 0:
        return out
    idx = lo[valid]
    end = min(len(peaks), max(int(np.ceil(edges[valid[-1] + 1])), int(idx[-1]) + 1))
    out[valid] = np.maximum.reduceat(peaks[:end], idx)
    return out


class Player:
    """Plays decoded audio; falls back to a silent clock when no output device exists."""

    def __init__(self):
        self.data: AudioData | None = None
        self._pos = 0              # frames
        self._playing = False
        self._lock = threading.Lock()
        self._stream = None
        self.silent_reason = ""
        self._clock_start = 0.0
        self._clock_pos = 0
        self.volume = 1.0          # 0.0 – 1.0, applied live

    # -- control
    def load(self, data: AudioData) -> None:
        self.stop()
        with self._lock:
            self.data = data
            self._pos = 0

    def play(self) -> None:
        if self.data is None or self._playing:
            return
        if self._pos >= len(self.data.samples):
            self._pos = 0
        self._playing = True
        if self._stream is None and not self.silent_reason:
            try:
                import sounddevice as sd

                self._stream = sd.OutputStream(samplerate=self.data.rate, channels=2, dtype="int16",
                                               callback=self._callback, blocksize=1024)
                self._stream.start()
            except Exception as e:  # no PortAudio / no device
                self._stream = None
                self.silent_reason = f"no audio output ({e.__class__.__name__})"
        elif self._stream is not None:
            try:
                self._stream.start()
            except Exception:
                pass
        self._clock_start, self._clock_pos = time.monotonic(), self._pos

    def pause(self) -> None:
        if not self._playing:
            return
        self._sync_clock()
        self._playing = False
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass

    def toggle(self) -> None:
        self.pause() if self._playing else self.play()

    def stop(self) -> None:
        self.pause()
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def seek(self, seconds: float) -> None:
        if self.data is None:
            return
        with self._lock:
            self._pos = int(max(0.0, min(self.data.duration, seconds)) * self.data.rate)
            self._clock_start, self._clock_pos = time.monotonic(), self._pos

    @property
    def playing(self) -> bool:
        return self._playing

    def position(self) -> float:
        if self.data is None:
            return 0.0
        if self._playing and self._stream is None:
            self._sync_clock()
        return self._pos / self.data.rate

    # -- internals
    def _sync_clock(self) -> None:
        if self._stream is None and self._playing and self.data is not None:
            elapsed = time.monotonic() - self._clock_start
            self._pos = min(len(self.data.samples), self._clock_pos + int(elapsed * self.data.rate))
            if self._pos >= len(self.data.samples):
                self._playing = False

    def _callback(self, outdata, frames, _time, _status) -> None:
        with self._lock:
            data = self.data
            if data is None or not self._playing:
                outdata.fill(0)
                return
            chunk = data.samples[self._pos:self._pos + frames]
            n = len(chunk)
            vol = self.volume
            if vol >= 0.999:
                outdata[:n] = chunk
            elif vol <= 0.001:
                outdata[:n] = 0
            else:
                outdata[:n] = (chunk * vol).astype(np.int16)
            if n < frames:
                outdata[n:] = 0
                self._playing = False
            self._pos += n
