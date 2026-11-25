"""Per-file review: gather lyric candidates for one song and apply the user's choice.

A choice made here is stored as a per-file override and takes precedence over
the saved settings and the options of any later run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import lrc as lrcmod
from . import user_agent
from .collection import LrcCollection
from .config import Settings
from .engine import lookup_key
from .output import LRC, read_lrc_file
from .paths import state_key, to_long_path
from .names import best_ratio, name_variants, primary_artist
from .providers import HttpClient, LrclibClient
from .sidecar import load_lrc
from .state import KEEP, PIN, State
from .tags import Meta, TagFile


@dataclass
class Candidate:
    key: str
    label: str
    text: str | None        # None = remove lyrics
    source: str
    path: str = ""          # sidecar file, if any
    src_length: float = 0.0  # length of the version the lyrics were timed for (0 = unknown)

    @property
    def synced(self) -> bool:
        return lrcmod.is_synced(self.text)


@dataclass
class SongInfo:
    path: str
    meta: Meta
    duration: float
    current: str
    candidates: list[Candidate] = field(default_factory=list)
    override: tuple | None = None
    prefs: dict = field(default_factory=dict)


def gather(path: str, settings: Settings, collection: LrcCollection | None = None) -> SongInfo:
    path = state_key(path)
    tf = TagFile(to_long_path(path))
    meta = tf.read()
    dur = tf.duration
    embedded = next((t for t in meta.lyrics if t), "")
    lrc_mode = settings.output_mode == LRC
    current = (read_lrc_file(tf.path) or "") if lrc_mode else embedded
    state = State(settings.resolved_state_db())
    try:
        info = SongInfo(path, meta, dur, current, override=state.get_override(path),
                        prefs=state.get_song_prefs(path))
        cands = info.candidates
        pend = state.get_pending(path)
        found = state.get_candidates(path)
        if pend:
            plen = next((c[3] for c in found.values() if lrcmod.same_lyrics(c[1], pend[2])), 0.0)
            cands.append(Candidate("pending", f"Found: {pend[1]}", pend[2], pend[0], pend[3] or "",
                                   plen or (lrcmod.analyze(pend[2]).length_header or 0.0)))
        for kind, (label, text, lrc_path, length) in found.items():
            if not (pend and lrcmod.same_lyrics(text, pend[2])):
                cands.append(Candidate(f"found-{kind}", f"Found ({kind}): {label}", text,
                                       "lrclib" if kind == "online" else "local .lrc", lrc_path, length))
        cands += sidecar_candidates(path, meta)
        if collection is not None and len(collection) and meta.title:
            hit, _ = collection.find(meta.title, meta.artist, dur, settings.match_threshold)
            if hit:
                cands.append(Candidate(f"collection:{hit.entry.lrc.path}",
                                       f"Collection: {os.path.basename(hit.entry.lrc.path)} "
                                       f"(match {hit.score:.0f}%)", hit.entry.lrc.text, "collection", "",
                                       hit.entry.lrc.info.length_header or 0.0))
        cached = state.get_lookup(lookup_key(meta, dur)) if meta.title else None
        if cached and cached[0] == "found" and cached[1].get("synced"):
            if not any(lrcmod.same_lyrics(c.text, cached[1]["synced"]) for c in cands if c.text):
                cands.append(Candidate(f"lrclib:{cached[1].get('id')}", f"LRCLIB #{cached[1].get('id')} (cached)",
                                       cached[1]["synced"], "lrclib", "", float(cached[1].get("duration") or 0)))
        if lrc_mode and embedded and not lrcmod.same_lyrics(embedded, current):
            cands.append(Candidate("embedded", "Embedded lyrics (save as .lrc)", embedded, "embedded"))
        if current:
            where = "Current .lrc file" if lrc_mode else "Current embedded lyrics"
            cands.append(Candidate("current", f"{where} (to re-time)", current, "current"))
        backup = state.get_backup(path)
        if backup and not lrcmod.same_lyrics(backup, current):
            cands.append(Candidate("backup", "Before Mass Synced Lyrics (original)", backup, "backup"))
        if current:
            cands.append(Candidate("remove", "No lyrics (delete the .lrc)" if lrc_mode else "No lyrics (remove)",
                                   None, "remove"))
    finally:
        state.close()
    return info


def sidecar_candidates(path: str, meta: Meta, limit: int = 5) -> list[Candidate]:
    folder = os.path.dirname(path)
    try:
        names = [f for f in os.listdir(to_long_path(folder)) if f.lower().endswith(".lrc")]
    except OSError:
        return []
    stem = os.path.splitext(os.path.basename(path))[0]
    av = name_variants(stem, meta.artist) | (name_variants(meta.title, meta.artist) if meta.title else set())
    scored = []
    for n in names:
        l = load_lrc(os.path.join(folder, n))
        if l.error or not l.text:
            continue
        score = 100.0 if os.path.splitext(n)[0].casefold() == stem.casefold() else best_ratio(av, l.variants)
        if score >= 60:
            scored.append((score, l))
    scored.sort(key=lambda x: -x[0])
    out = []
    for score, l in scored[:limit]:
        kind = "" if l.info.synced else ", plain"
        out.append(Candidate(f"sidecar:{l.path}", f"Sidecar: {os.path.basename(l.path)} (match {score:.0f}%{kind})",
                             l.text, "local .lrc", l.path, l.info.length_header or 0.0))
    return out


def search_lrclib(meta: Meta, duration: float, settings: Settings) -> list[Candidate]:
    client = LrclibClient(HttpClient(user_agent(), settings.requests_per_minute))
    recs = client.search(meta.title, meta.artist)
    pa = primary_artist(meta.artist)
    if not recs and pa and pa != meta.artist:
        recs = client.search(meta.title, pa)
    out = []
    for r in recs:
        if not r.get("syncedLyrics"):
            continue
        diff = (r.get("duration") or 0) - duration
        album = f" [{r.get('albumName')}]" if r.get("albumName") else ""
        out.append((abs(diff), Candidate(
            f"lrclib:{r.get('id')}",
            f"LRCLIB #{r.get('id')}: {r.get('artistName')} – {r.get('trackName')}{album} ({diff:+.1f}s)",
            lrcmod.clean_text(r["syncedLyrics"]), "lrclib", "", float(r.get("duration") or 0))))
    out.sort(key=lambda x: x[0])
    return [c for _, c in out]


def choose(path: str, text: str | None, label: str, settings: Settings, lrc_path: str = "") -> None:
    """Record the user's lyrics for this file. Nothing is written until the final apply step."""
    path = state_key(path)
    state = State(settings.resolved_state_db())
    try:
        state.set_override(path, PIN, text or "", label, lrc_path)
    finally:
        state.close()


def choice_options(path: str, settings: Settings) -> list[tuple[str, str, str | None, str]]:
    """What the Choice column can cycle through: (kind, label, text, sidecar path)."""
    path = state_key(path)
    state = State(settings.resolved_state_db())
    try:
        found = state.get_candidates(path)
        pend = state.get_pending(path)
    finally:
        state.close()
    opts = []
    for kind in ("local", "online"):
        if kind in found:
            label, text, lrc_path, _ = found[kind]
            opts.append((kind, label, text, lrc_path))
    if pend and pend[0] != "your choice" and not any(lrcmod.same_lyrics(o[2], pend[2]) for o in opts):
        kind = "online" if pend[0] == "lrclib" else "local"
        if kind not in {o[0] for o in opts}:
            opts.append((kind, pend[1], pend[2], pend[3] or ""))
    opts.append(("keep", "Don't change", None, ""))
    return opts


def lock_current(path: str, settings: Settings) -> None:
    state = State(settings.resolved_state_db())
    try:
        state.set_override(path, KEEP, "", "keep current lyrics")
    finally:
        state.close()


def clear_choice(path: str, settings: Settings) -> None:
    state = State(settings.resolved_state_db())
    try:
        state.clear_override(path)
        row = state.get_file(path)
        if row and row[2] == "locked":  # let the next run evaluate it normally
            state.delete_file(path)
    finally:
        state.close()


def save_prefs(path: str, prefs: dict, settings: Settings) -> None:
    state = State(settings.resolved_state_db())
    try:
        state.set_song_prefs(path, prefs)
    finally:
        state.close()


def default_checkpoints(text: str | None, duration: float) -> list[float]:
    lines = [t for t, _ in lrcmod.display_lines(text) if t is not None and t <= duration + 1]
    if not lines:
        return []
    first, last = lines[0], lines[-1]
    return [first] if abs(last - first) < 0.01 else [first, last]
