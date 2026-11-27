"""Audio decoding (PyAV) and waveform peaks. Playback happens in the window (Electron)."""
from __future__ import annotations

import threading
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


VOLUME_RANGE_DB = 40.0


def volume_gain(level: float) -> float:
    """Slider level 0–100 → amplitude gain on a decibel (logarithmic) scale:
    100 = 0 dB, 50 = −20 dB, 25 = −30 dB … 0 = silent."""
    level = max(0.0, min(100.0, float(level)))
    if level <= 0:
        return 0.0
    return 10 ** (-VOLUME_RANGE_DB * (1 - level / 100.0) / 20.0)
