"""The processing pipeline shared by the GUI and the command line."""
from __future__ import annotations

import csv
import json
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from . import lrc as lrcmod
from . import user_agent
from .collection import LrcCollection
from .config import Settings
from .names import norm
from .paths import display_path, to_long_path  # noqa: F401  (re-exported)
from .output import LRC, VerifyError, current_texts, dispose, same_file, write_output
from .output import lrc_path_for, read_lrc_file
from .providers import (AcoustIdClient, Cancelled, HttpClient, LrclibClient, LyricsResult,
                        MusicBrainzClient, ServiceError, find_fpcalc, find_synced)
from .sidecar import AudioEntry, SidecarMatch, match_folder
from .state import FINAL, KEEP, MISSES, State
from .tags import AUDIO_EXTENSIONS, Meta, TagFile, UnsupportedFile

CSV_FIELDS = ["time", "status", "source", "path", "duration", "artist", "title", "album",
              "lrc_file", "score", "detail"]

STATUS_LABELS = {
    "embedded": "Embedded",
    "already_embedded": "Already embedded",
    "has_synced": "Already had synced lyrics",
    "instrumental": "Instrumental",
    "no_synced": "Only plain lyrics online (left alone)",
    "not_found": "Not found",
    "no_metadata": "No title/artist",
    "ambiguous": "Ambiguous .lrc match",
    "rejected": "Lyrics rejected",
    "unsupported": "Unsupported file",
    "error": "Error",
    "cached": "Skipped (done in earlier run)",
    "locked": "Per-file choice kept",
    "proposed": "Lyrics found (not written yet)",
    "lrc_written": ".lrc saved",
    "unchanged": "Unchanged",
    "chosen": "Your choice (not written yet)",
    "restored": "Original restored",
}

Emit = Callable[[dict], None]


def lookup_key(meta: Meta, duration: float | None) -> str:
    return json.dumps([norm(meta.artist), norm(meta.title), norm(meta.album), int(round(duration or 0))])


@dataclass
class Result:
    path: str
    status: str
    source: str = ""
    meta: Meta | None = None
    duration: float | None = None
    lrc_file: str = ""
    score: float | None = None
    detail: str = ""
    local: str = ""      # local candidate kind: sidecar | collection | embedded
    online: str = ""     # LRCLIB outcome: found | instrumental | plain only | not found | ""
    choice: str = ""     # what Apply will do: local | online | keep | custom


ONLINE_LABELS = {"found": "found", "instrumental": "instrumental", "no_synced": "plain only",
                 "not_found": "not found"}
LOCAL_SOURCES = {"local .lrc": "sidecar", "collection": "collection", "embedded": "embedded"}


class Engine:
    def __init__(self, settings: Settings, emit: Emit | None = None,
                 cancel: threading.Event | None = None,
                 lrclib: LrclibClient | None = None,
                 acoustid: AcoustIdClient | None = None,
                 musicbrainz: MusicBrainzClient | None = None):
        self.s = settings
        self.emit = emit or (lambda e: None)
        self.cancel = cancel or threading.Event()
        self.counts: Counter = Counter()
        ua = user_agent()
        self.lrclib = lrclib
        if self.lrclib is None and settings.use_online:
            self.lrclib = LrclibClient(HttpClient(ua, settings.requests_per_minute, self.cancel))
        self.musicbrainz = musicbrainz
        if self.musicbrainz is None and settings.use_online:
            self.musicbrainz = MusicBrainzClient(HttpClient(ua, 50, self.cancel))
        self.acoustid = acoustid
        if self.acoustid is None and settings.use_online and settings.use_acoustid:
            key, fp = settings.resolved_acoustid_key(), find_fpcalc(settings.fpcalc_path)
            if key and fp:
                self.acoustid = AcoustIdClient(HttpClient(ua, 150, self.cancel), key, fp)
            else:
                self._acoustid_missing = "no API key" if not key else "fpcalc not found"
        self.collection: LrcCollection | None = None

    # ------------------------------------------------------------------ run
    def log(self, text: str) -> None:
        self.emit({"type": "log", "text": text})

    def run(self) -> dict:
        s = self.s
        root = s.music_folder
        if not root or not os.path.isdir(root):
            raise ValueError(f"Music folder not found: {root}")
        os.makedirs(s.resolved_log_dir(), exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        csv_path = os.path.join(s.resolved_log_dir(), f"run-{stamp}{'-dryrun' if s.dry_run else ''}.csv")
        state = State(s.resolved_state_db())
        started = time.monotonic()
        cancelled = False
        if s.dry_run:
            self.log("Finding lyrics only: nothing is written now.")
        if getattr(self, "_acoustid_missing", ""):
            self.log(f"AcoustID fallback disabled ({self._acoustid_missing}); untagged files will be reported.")
        try:
            if s.use_collection and s.collection:
                self.log("Indexing personal LRC collection…")
                self.collection = LrcCollection(s.collection, self.log, self.cancel.is_set)
                self.log(f"LRC collection: {len(self.collection)} synced files indexed"
                         + (f", {self.collection.skipped} skipped (plain or unreadable)" if self.collection.skipped else ""))
            self.log("Scanning music folder…")
            folders = self._scan(to_long_path(root))
            total = sum(len(a) for _, a, _ in folders)
            self.emit({"type": "scan", "total": total, "folders": len(folders)})
            self.log(f"Found {total} audio files in {len(folders)} folders.")
            done = 0
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
                writer.writeheader()
                for folder, audio, lrcs in folders:
                    if self.cancel.is_set():
                        break
                    for res in self._process_folder(folder, audio, lrcs, state):
                        done += 1
                        self.counts[res.status] += 1
                        self._record_result(state, res)
                        if res.status != "cached":
                            writer.writerow(self._row(res))
                            fh.flush()
                        self.emit({"type": "result", "result": res, "done": done, "total": total,
                                   "counts": dict(self.counts),
                                   "elapsed": time.monotonic() - started})
                        if self.cancel.is_set():
                            break
        except Cancelled:
            pass
        finally:
            state.close()
        cancelled = self.cancel.is_set()
        summary = {"type": "done", "counts": dict(self.counts), "csv": csv_path,
                   "cancelled": cancelled, "elapsed": time.monotonic() - started}
        self.emit(summary)
        return summary

    def _scan(self, root: str) -> list[tuple[str, list[str], list[str]]]:
        out = []
        n = 0
        for dirpath, dirs, files in os.walk(root):
            if self.cancel.is_set():
                break
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            audio, lrcs = [], []
            for f in sorted(files):
                ext = os.path.splitext(f)[1].lower()
                if ext in AUDIO_EXTENSIONS:
                    audio.append(os.path.join(dirpath, f))
                elif ext == ".lrc":
                    lrcs.append(os.path.join(dirpath, f))
            if audio:
                out.append((dirpath, audio, lrcs))
                n += len(audio)
                if n and n % 2000 < len(audio):
                    self.emit({"type": "scanning", "found": n})
        return out

    # -------------------------------------------------------------- folders
    def _process_folder(self, folder, audio_paths, lrc_paths, state):
        tagfiles: dict[str, TagFile | Exception] = {}

        def open_tf(p):
            if p not in tagfiles:
                try:
                    tagfiles[p] = TagFile(p)
                except UnsupportedFile as e:
                    tagfiles[p] = e
            return tagfiles[p]

        matches: dict[str, SidecarMatch] = {}
        notes: dict[str, str] = {}
        if lrc_paths and self.s.output_mode == LRC:
            # with .lrc output, "<song>.lrc" is the song's current lyrics, not a source to import
            outputs = {os.path.normcase(lrc_path_for(a)) for a in audio_paths}
            lrc_paths = [p for p in lrc_paths if os.path.normcase(p) not in outputs]
        if lrc_paths:
            entries = []
            for p in audio_paths:
                tf = open_tf(p)
                if isinstance(tf, TagFile):
                    entries.append(AudioEntry(p, tf.read(), tf.duration))
                else:
                    entries.append(AudioEntry(p, None, None))
            matches, notes = match_folder(entries, lrc_paths, self.s.match_threshold)

        for p in audio_paths:
            if self.cancel.is_set():
                return
            try:
                yield self._process_file(p, open_tf, matches.get(p), notes.get(p, ""), state)
            except Cancelled:
                raise
            except Exception as e:  # keep going on per-file failures
                yield Result(p, "error", detail=f"{type(e).__name__}: {e}")
            tagfiles.pop(p, None)

    # ----------------------------------------------------------------- file
    def _process_file(self, path, open_tf, match: SidecarMatch | None, note: str, state: State) -> Result:
        s = self.s
        mode = s.output_mode
        st = os.stat(path)
        ov = state.get_override(path)
        if ov is not None:
            return self._apply_override(path, open_tf, ov, st, state)
        prev = state.get_file(path)
        unchanged = prev is not None and prev[0] == st.st_size and prev[1] == st.st_mtime_ns
        if prev is not None and prev[2] in ("has_synced", "already_embedded") and prev[4] != mode:
            unchanged = False  # judged for the other output mode
        if mode == LRC and prev is not None and prev[2] in FINAL and os.path.exists(lrc_path_for(path)) \
                and prev[2] in MISSES:
            unchanged = False  # a .lrc appeared since
        if match is None and unchanged and prev[2] in FINAL:
            recheck_collection = prev[2] in MISSES and self.collection is not None and len(self.collection)
            if not (s.retry_misses and prev[2] in MISSES) and not recheck_collection:
                info = state.get_file_info(path)
                return Result(path, "cached", info.get("source", ""),
                              Meta(title=info.get("title", ""), artist=info.get("artist", ""),
                                   album=info.get("album", "")),
                              detail=prev[2], local=info.get("local", ""), online=info.get("online", ""),
                              choice=state.current_choice(path))
            online_allowed = s.retry_misses or prev[2] not in MISSES
        else:
            online_allowed = True

        tf = open_tf(path)
        if isinstance(tf, Exception):
            return Result(path, "unsupported", detail=str(tf))
        meta = tf.read()
        dur = tf.duration or None
        existing = current_texts(tf, mode, meta.lyrics)
        has_synced = any(lrcmod.is_synced(t) for t in existing)
        find = s.dry_run                      # find-only (app): gather every source for the Choice column
        if find:
            state.clear_candidates(path)

        # 1. Local sources: a sidecar .lrc beside the file always wins.
        local: tuple | None = None            # (source, text, lrc_file label, score, lrc_src path)
        if match is not None:
            local = ("local .lrc", s.tidy(match.lrc.text), display_path(match.lrc.path), round(match.score, 1),
                     match.lrc.path)
            if any(lrcmod.same_lyrics(t, local[1]) for t in existing):
                res = Result(path, "already_embedded", mode, meta, dur, local[2], local[3],
                             local="sidecar", choice="local")
                if find and self._will_dispose(match.lrc.path, path):
                    # remember the sidecar so the final apply step can tidy it up
                    state.set_pending(path, "local .lrc", f"sidecar {res.lrc_file}", local[1],
                                      match.lrc.path)
                self._dispose_lrc(match.lrc.path, res)
                return res
        elif has_synced:
            return Result(path, "has_synced", mode, meta=meta, duration=dur, choice="keep")
        elif mode == LRC:
            # .lrc output: lyrics already embedded in the file can simply be exported.
            emb = next((t for t in meta.lyrics if lrcmod.is_synced(t)), None)
            if emb:
                local = ("embedded", s.tidy(emb), "", None, "")
        if local is None and note.startswith("ambiguous"):
            return Result(path, "ambiguous", meta=meta, duration=dur, detail=note, choice="keep")

        # Identity for collection / online lookups.
        ident, how = meta, ""
        if not meta.identified:
            ident, how = self._identify(path, meta)
            if ident is None and local is None:
                return Result(path, "no_metadata", meta=meta, duration=dur,
                              detail=(note + "; " if note else "") + how, choice="keep")

        # 2. Personal LRC collection.
        if local is None and ident is not None and self.collection is not None and len(self.collection):
            hit, why = self.collection.find(ident.title, ident.artist, dur, s.match_threshold)
            if hit:
                local = ("collection", s.tidy(hit.entry.lrc.text), display_path(hit.entry.lrc.path),
                         round(hit.score, 1), "")
            elif why:
                note = (note + "; " if note else "") + why
                if why.startswith("ambiguous"):
                    return Result(path, "ambiguous", meta=ident, duration=dur, detail=note, choice="keep")

        # 3. LRCLIB (also when a local source exists in find mode, so both can be offered).
        found: LyricsResult | None = None
        online_ok = s.use_online and self.lrclib is not None and online_allowed and ident is not None
        if online_ok and (local is None or find):
            found = self._lookup(ident, dur, state)
            if found.status == "found":
                found.synced = s.tidy(found.synced)
        online = ONLINE_LABELS.get(found.status, "") if found else ""
        detail = "; ".join(x for x in (note, how, found.how if found else "") if x)
        shown = ident or meta

        if find:
            if local is not None:
                state.set_candidate(path, "local", self._label(local[0], local[2]), local[1], local[4],
                                    lrcmod.analyze(local[1]).length_header or 0)
            if found is not None and found.status == "found":
                state.set_candidate(path, "online", f"lrclib #{found.record_id}", found.synced, "",
                                    found.duration)

        if local is not None:
            res = Result(path, "embedded", local[0], shown, dur, local[2], local[3], detail,
                         local=LOCAL_SOURCES.get(local[0], local[0]), online=online, choice="local")
            if local[0] == "embedded":
                res.detail = "; ".join(x for x in (detail, "exported from embedded lyrics") if x)
            elif has_synced:
                res.detail = "; ".join(x for x in (detail, "replaces existing synced lyrics") if x)
            elif existing:
                res.detail = "; ".join(x for x in (detail, "replaces plain lyrics") if x)
            self._deliver(tf, local[1], st, state, res, local[4])
            return res
        if found is not None and found.status == "found":
            res = Result(path, "embedded", "lrclib", shown, dur, f"lrclib #{found.record_id}", None, detail,
                         online=online, choice="online")
            if existing:
                res.detail = "; ".join(x for x in (detail, "replaces plain lyrics") if x)
            self._deliver(tf, found.synced, st, state, res)
            return res
        if found is None:
            status = prev[2] if (prev and not online_allowed and prev[2] != "cached") else "not_found"
            why = "online lookup off" if not s.use_online else "earlier online miss"
            return Result(path, status, meta=shown, duration=dur,
                          detail=(note + "; " if note else "") + why, choice="keep")
        return Result(path, found.status, "lrclib", shown, dur, detail=detail, online=online, choice="keep")

    @staticmethod
    def _label(source: str, lrc_file: str) -> str:
        name = os.path.basename(display_path(lrc_file)) if lrc_file else ""
        return {"local .lrc": f"sidecar {name}", "collection": f"collection {name}",
                "embedded": "embedded lyrics"}.get(source, source)

    def _identify(self, path: str, meta: Meta) -> tuple[Meta | None, str]:
        if meta.mbid and self.musicbrainz is not None:
            try:
                m = self.musicbrainz.recording(meta.mbid)
                if m and m.identified:
                    m.album = meta.album or m.album
                    return m, "identified via MusicBrainz ID"
            except ServiceError as e:
                self.log(f"MusicBrainz lookup failed: {e}")
        if self.acoustid is not None:
            try:
                m = self.acoustid.identify(display_path(path))
                if m and m.identified:
                    m.album = meta.album or m.album
                    return m, "identified via AcoustID"
                return None, "AcoustID: no confident match"
            except (ServiceError, OSError, ValueError) as e:
                if "invalid API key" in str(e):
                    self.acoustid = None
                    self.log("AcoustID rejected the API key, so untagged files can't be identified this run. "
                             "It needs an *application* API key (register one at acoustid.org/new-application).")
                    return None, "AcoustID API key rejected"
                return None, f"AcoustID failed: {e}"
        return None, "missing title/artist tags"

    def _lookup(self, meta: Meta, duration: float | None, state: State) -> LyricsResult:
        key = lookup_key(meta, duration)
        cached = state.get_lookup(key)
        if cached and (cached[0] == "found" or not self.s.retry_misses):
            p = cached[1]
            return LyricsResult(cached[0], p.get("synced", ""), p.get("id"), "cached lookup",
                                float(p.get("duration") or 0))
        res = find_synced(self.lrclib, meta, duration)
        state.set_lookup(key, res.status, {"synced": res.synced, "id": res.record_id, "duration": res.duration})
        return res

    # --------------------------------------------------------------- writes
    def _deliver(self, tf: TagFile, text: str, st: os.stat_result, state: State, res: Result,
                 lrc_src: str = "") -> None:
        """Write lyrics to the chosen output, or (dry run / find-only) store them as a proposal."""
        path = tf.path
        label = res.lrc_file if res.source == "lrclib" else self._label(res.source, res.lrc_file)
        if self.s.dry_run:
            state.set_pending(path, res.source, label, text, lrc_src)
            res.status = "proposed"
            return
        mode = self.s.output_mode
        state.record_original(path, mode, tf.read().lyrics, read_lrc_file(path) if mode == LRC else None)
        try:
            write_output(tf, text, mode, self.s.restore_mtime, self.s.lrc_action, st)
        except (VerifyError, OSError) as e:
            res.status, res.detail = "error", str(e)
            return
        res.status = "lrc_written" if mode == LRC else "embedded"
        state.clear_pending(path)
        if lrc_src:
            self._dispose_lrc(lrc_src, res)

    def _will_dispose(self, lrc_src: str, audio_path: str) -> bool:
        if self.s.lrc_action == "keep" or not lrc_src:
            return False
        return not (self.s.output_mode == LRC and same_file(lrc_src, lrc_path_for(audio_path)))

    def _apply_override(self, path, open_tf, ov, st, state: State) -> Result:
        kind, text, label = ov[:3]
        text = self.s.tidy(text) if text else text
        if kind == KEEP:
            return Result(path, "locked", "your choice", detail="keep current lyrics", choice="keep")
        choice = state.choice_kind_for_text(path, text)
        tf = open_tf(path)
        if isinstance(tf, Exception):
            return Result(path, "unsupported", detail=str(tf))
        meta = tf.read()
        cur = current_texts(tf, self.s.output_mode, meta.lyrics)
        if (text and any(lrcmod.same_lyrics(t, text) for t in cur)) or (not text and not cur):
            return Result(path, "locked", "your choice", meta, tf.duration, detail=label, choice=choice)
        res = Result(path, "embedded", "your choice", meta, tf.duration, detail=f"re-applied: {label}",
                     choice=choice)
        if self.s.dry_run:
            res.status, res.detail = "proposed", f"your choice: {label}"
            return res
        try:
            state.record_original(path, self.s.output_mode, meta.lyrics,
                                  read_lrc_file(path) if self.s.output_mode == LRC else None)
            write_output(tf, text or None, self.s.output_mode, self.s.restore_mtime, self.s.lrc_action, st)
            res.status = "lrc_written" if self.s.output_mode == LRC else "embedded"
        except (VerifyError, OSError) as e:
            res.status, res.detail = "error", str(e)
        return res

    def _dispose_lrc(self, lrc_path: str, res: Result) -> None:
        if self.s.dry_run or res.status not in ("embedded", "lrc_written", "already_embedded"):
            return
        if not self._will_dispose(lrc_path, res.path):
            return
        note = dispose(lrc_path, self.s.lrc_action)
        if note:
            res.detail = "; ".join(x for x in (res.detail, note) if x)

    def _record_result(self, state: State, r: Result) -> None:
        if r.status == "cached":
            return
        try:
            st = os.stat(r.path)
        except OSError:
            return
        m = r.meta or Meta()
        state.set_file(r.path, st.st_size, st.st_mtime_ns, r.status, r.detail, r.source, m.artist, m.title,
                       r.local, r.online, m.album)

    @staticmethod
    def _row(r: Result) -> dict:
        m = r.meta or Meta()
        return {
            "time": datetime.now().isoformat(timespec="seconds"),
            "status": r.status, "source": r.source, "path": display_path(r.path),
            "duration": f"{r.duration:.1f}" if r.duration else "",
            "artist": m.artist, "title": m.title, "album": m.album,
            "lrc_file": r.lrc_file, "score": "" if r.score is None else r.score, "detail": r.detail,
        }
