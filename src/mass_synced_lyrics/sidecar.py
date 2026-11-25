"""Match existing .lrc files in a folder to the audio files beside them."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import lrc as lrcmod
from .names import best_ratio, name_variants
from .tags import Meta

TIE_MARGIN = 2.0


@dataclass
class LocalLrc:
    path: str
    text: str
    info: lrcmod.LrcInfo
    variants: set[str] = field(default_factory=set)
    error: str = ""


@dataclass
class SidecarMatch:
    lrc: LocalLrc
    score: float
    exact: bool


@dataclass
class AudioEntry:
    path: str
    meta: Meta | None
    duration: float | None


def load_lrc(path: str) -> LocalLrc:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        return LocalLrc(path, "", lrcmod.analyze(""), error=f"unreadable: {e}")
    text = lrcmod.clean_text(lrcmod.decode_bytes(data))
    info = lrcmod.analyze(text)
    stem = os.path.splitext(os.path.basename(path))[0]
    variants = name_variants(stem, info.headers.get("ar"))
    if info.headers.get("ti"):
        variants |= name_variants(info.headers["ti"])
    return LocalLrc(path, text, info, variants)


def _stem(p: str) -> str:
    return os.path.splitext(os.path.basename(p))[0].casefold()


def match_folder(
    audio: list[AudioEntry],
    lrc_paths: list[str],
    threshold: float,
) -> tuple[dict[str, SidecarMatch], dict[str, str]]:
    """Return ({audio_path: match}, {audio_path: note}) for one folder.

    Exact stem matches win; otherwise each .lrc is assigned to at most one
    track by descending fuzzy score, and near-ties are left unassigned.
    """
    matches: dict[str, SidecarMatch] = {}
    notes: dict[str, str] = {}
    if not lrc_paths or not audio:
        return matches, notes

    lrcs = [load_lrc(p) for p in lrc_paths]
    used: set[str] = set()

    # Pass 1: identical file stems.
    by_stem = {_stem(l.path): l for l in lrcs}
    for a in audio:
        l = by_stem.get(_stem(a.path))
        if not l:
            continue
        reason = l.error or lrcmod.check_against_duration(l.info, a.duration)
        if reason:
            notes[a.path] = f"local .lrc rejected ({os.path.basename(l.path)}): {reason}"
            used.add(l.path)  # do not offer an obviously paired file to others
            continue
        matches[a.path] = SidecarMatch(l, 100.0, True)
        used.add(l.path)

    # Pass 2: fuzzy matching on file stem and title tag.
    pairs: list[tuple[float, AudioEntry, LocalLrc]] = []
    for a in audio:
        if a.path in matches or a.path in notes:
            continue
        artist = a.meta.artist if a.meta else None
        av = name_variants(os.path.splitext(os.path.basename(a.path))[0], artist)
        if a.meta and a.meta.title:
            av |= name_variants(a.meta.title, artist)
        for l in lrcs:
            if l.path in used or l.error:
                continue
            score = best_ratio(av, l.variants)
            if score < threshold:
                continue
            reason = lrcmod.check_against_duration(l.info, a.duration)
            if reason:
                notes.setdefault(a.path, f"near-match {os.path.basename(l.path)} rejected: {reason}")
                continue
            pairs.append((score, a, l))

    pairs.sort(key=lambda p: -p[0])
    for score, a, l in pairs:
        if a.path in matches or l.path in used:
            continue
        rivals = [
            p for p in pairs
            if p[0] >= score - TIE_MARGIN
            and ((p[1].path == a.path and p[2].path != l.path and p[2].path not in used)
                 or (p[2].path == l.path and p[1].path != a.path and p[1].path not in matches))
        ]
        if rivals:
            names = sorted({os.path.basename(p[2].path) for p in rivals} | {os.path.basename(l.path)})
            notes[a.path] = "ambiguous local .lrc match: " + ", ".join(names)
            continue
        matches[a.path] = SidecarMatch(l, score, False)
        notes.pop(a.path, None)
        used.add(l.path)
    return matches, notes
