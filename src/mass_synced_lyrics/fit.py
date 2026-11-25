"""How well a synced lyric fits a particular recording ("Fit", 0-100).

Without the audio: the length the lyrics were timed for against the file's length, lines that run
past the end, and whether the lyrics cover a plausible stretch of the song.
With the audio's peak envelope (100 values per second, 0..1) it also checks that lines start where
there is sound rather than silence, and that the lyrics don't run on into the trailing silence.
"""
from __future__ import annotations

from . import lrc as lrcmod

PEAKS_PER_SECOND = 100


def fit_score(text: str | None, duration: float | None, source_length: float | None = None,
              peaks=None) -> tuple[float | None, list[str]]:
    """(score 0-100 or None when it can't be judged, reasons for each deduction)."""
    if not text or not duration or duration <= 0 or not lrcmod.is_synced(text):
        return None, []
    times = [t for t, body in lrcmod.display_lines(text) if t is not None and body.strip()]
    if len(times) < 2:
        return None, []
    score, why = 100.0, []

    def take(points: float, reason: str) -> None:
        nonlocal score
        if points > 0.5:
            score -= points
            why.append(f"{reason} (−{points:.0f})")

    length = source_length or lrcmod.analyze(text).length_header
    if length:
        diff = abs(float(length) - duration)
        if diff > 0.5:
            take(min(40.0, 6.0 * (diff - 0.5)), f"timed for a {diff:.1f} s {'longer' if length > duration else 'shorter'} version")
    first, last = times[0], times[-1]
    if last > duration + 0.5:
        take(min(50.0, 20.0 + 5.0 * (last - duration)), f"last line at {last:.0f} s, after the song ends")
    elif last < 0.45 * duration:
        take(15.0, f"lyrics end early ({last:.0f} s of {duration:.0f} s)")
    if first > 0.5 * duration:
        take(15.0, f"first line only at {first:.0f} s")

    if peaks is not None and len(peaks) > PEAKS_PER_SECOND:
        try:
            _audio_checks(times, duration, peaks, take)
        except Exception:
            pass
    return round(max(0.0, min(100.0, score)), 1), why


def _audio_checks(times, duration, peaks, take) -> None:
    import numpy as np

    p = np.asarray(peaks, dtype=np.float32)
    floor, level = float(np.percentile(p, 10)), float(np.percentile(p, 90))
    if level - floor < 0.02:
        return                                    # flat / near-silent file: nothing to learn
    thr = max(0.02, floor + 0.08 * (level - floor))
    loud = np.nonzero(p > thr)[0]
    if not len(loud):
        return
    sound_end = loud[-1] / PEAKS_PER_SECOND
    silent = 0
    for i, t in enumerate(times):
        nxt = times[i + 1] if i + 1 < len(times) else t + 3.0
        a = int(t * PEAKS_PER_SECOND)
        b = int(min(t + min(3.0, max(0.5, nxt - t)), duration) * PEAKS_PER_SECOND)
        if a >= len(p):
            silent += 1
            continue
        if float(p[a:max(a + 1, b)].max()) <= thr:
            silent += 1
    frac = silent / len(times)
    if frac > 0.05:
        take(min(40.0, 80.0 * frac), f"{silent} of {len(times)} lines start on silence")
    if times[-1] > sound_end + 3.0:
        take(20.0, f"lyrics continue {times[-1] - sound_end:.0f} s into the silence at the end")


def peaks_for(path: str):
    """Decode the file and return its peak envelope, or None (no decoder / unreadable)."""
    try:
        from .player import decode
    except Exception:
        return None
    try:
        return decode(path).peaks
    except Exception:
        return None
