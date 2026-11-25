"""A user-supplied collection of .lrc files used before any online lookup.

Files can live anywhere (one or more folders, or individual files). Each is
identified by its [ar:]/[ti:] headers, falling back to an "Artist - Title"
file name, and matched to tracks by title + artist similarity and duration.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Iterable

from rapidfuzz import fuzz, process

from . import lrc as lrcmod
from .names import norm, primary_artist, similarity, strip_trackno
from .sidecar import LocalLrc, load_lrc


@dataclass
class CollectionEntry:
    lrc: LocalLrc
    title: str
    artist: str
    key: str  # loose-normalised title


@dataclass
class CollectionHit:
    entry: CollectionEntry
    score: float


def _identity(l: LocalLrc) -> tuple[str, str]:
    title = l.info.headers.get("ti", "")
    artist = l.info.headers.get("ar", "")
    stem = os.path.splitext(os.path.basename(l.path))[0]
    if not title:
        if " - " in stem:
            left, right = stem.rsplit(" - ", 1)
            title = right
            if not artist:
                # "01 Artist - Title" / "Artist - Album - Title"
                artist = left.split(" - ")[0]
                artist = strip_trackno(artist) if artist[:1].isdigit() else artist
        else:
            title = stem
    return title.strip(), artist.strip()


class LrcCollection:
    def __init__(self, sources: Iterable[str], log: Callable[[str], None] | None = None,
                 cancel: Callable[[], bool] | None = None):
        self.entries: list[CollectionEntry] = []
        self.by_key: dict[str, list[CollectionEntry]] = {}
        self.skipped = 0
        log = log or (lambda s: None)
        paths: list[str] = []
        for src in sources:
            src = (src or "").strip()
            if not src:
                continue
            if os.path.isdir(src):
                for root, dirs, files in os.walk(src):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    paths += [os.path.join(root, f) for f in files if f.lower().endswith(".lrc")]
            elif os.path.isfile(src) and src.lower().endswith(".lrc"):
                paths.append(src)
            else:
                log(f"LRC collection: not found or not .lrc: {src}")
        for p in sorted(set(paths)):
            if cancel and cancel():
                break
            l = load_lrc(p)
            if l.error or not l.info.synced:
                self.skipped += 1
                continue
            title, artist = _identity(l)
            key = norm(title, loose=True)
            if not key:
                self.skipped += 1
                continue
            e = CollectionEntry(l, title, artist, key)
            self.entries.append(e)
            self.by_key.setdefault(key, []).append(e)
        self._keys = list(self.by_key.keys())

    def __len__(self) -> int:
        return len(self.entries)

    def find(self, title: str, artist: str, duration: float | None,
             threshold: float) -> tuple[CollectionHit | None, str]:
        """Best entry for a track, or (None, reason)."""
        if not self.entries or not title:
            return None, ""
        key = norm(title, loose=True)
        cands: list[tuple[CollectionEntry, float]] = []
        if key in self.by_key:
            cands += [(e, 100.0) for e in self.by_key[key]]
        for k, score, _ in process.extract(key, self._keys, scorer=fuzz.ratio,
                                           score_cutoff=threshold, limit=10):
            if k != key:
                cands += [(e, float(score)) for e in self.by_key[k]]

        scored: list[tuple[float, CollectionEntry]] = []
        rejected = ""
        for e, tscore in cands:
            if e.artist:
                ascore = max(similarity(artist, e.artist),
                             similarity(primary_artist(artist), primary_artist(e.artist)))
                if ascore < threshold:
                    continue
            else:
                ascore = threshold  # no artist info in file: title + duration only
            reason = lrcmod.check_against_duration(e.lrc.info, duration)
            if reason:
                rejected = f"collection near-match {os.path.basename(e.lrc.path)} rejected: {reason}"
                continue
            scored.append(((tscore + ascore) / 2, e))
        if not scored:
            return None, rejected
        scored.sort(key=lambda x: -x[0])
        best_score, best = scored[0]
        rivals = [e for s, e in scored[1:]
                  if s >= best_score - 2 and not lrcmod.same_lyrics(e.lrc.text, best.lrc.text)]
        if rivals:
            names = [os.path.basename(best.lrc.path)] + [os.path.basename(e.lrc.path) for e in rivals[:3]]
            return None, "ambiguous collection match: " + ", ".join(names)
        return CollectionHit(best, best_score), ""
