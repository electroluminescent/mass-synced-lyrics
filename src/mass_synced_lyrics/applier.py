"""The final step: write every proposal and per-file choice to the chosen output.

Mode "embed" overwrites the audio files' lyrics tags; mode "lrc" saves a .lrc
file with the same name beside each audio file. Per-file choices win over
proposals; files locked with "keep current" are skipped.
"""
from __future__ import annotations

import csv
import os
import threading
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from typing import Callable, Iterable

from . import lrc as lrcmod
from .config import Settings
from .output import (EMBED, LRC, VerifyError, current_texts, dispose, lrc_path_for, read_lrc_file, same_file,
                     write_lyrics, write_lrc_file, write_output)
from .paths import display_path, state_key, to_long_path
from .state import KEEP, PIN, State
from .tags import TagFile, UnsupportedFile


def plan(settings: Settings, paths: Iterable[str]) -> list[str]:
    """Paths (from `paths`) that have something to apply."""
    st = State(settings.resolved_state_db())
    try:
        pending = st.pending_paths()
        kinds = st.override_kinds()
    finally:
        st.close()
    out = []
    for p in paths:
        k = state_key(p)
        kind = kinds.get(k)
        if kind == KEEP:
            continue
        if kind == PIN or k in pending:
            out.append(k)
    return out


def _parallel(settings: Settings, items: list[str], job: Callable[[str, State], object],
              cancel: threading.Event):
    """Run job(item, state) on a pool of worker threads (settings.workers), each with its own
    State connection. Yields (item, result) as each one finishes; stops handing out work on cancel."""
    workers = max(1, min(int(getattr(settings, "workers", 6) or 1), len(items) or 1))
    tls = threading.local()
    states: list[State] = []
    lock = threading.Lock()

    def run(item):
        if cancel.is_set():
            return None
        st = getattr(tls, "state", None)
        if st is None:
            st = tls.state = State(settings.resolved_state_db())
            with lock:
                states.append(st)
        return job(item, st)

    it = iter(items)
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="msl-save") as pool:
            pending = {}
            for item in it:
                pending[pool.submit(run, item)] = item
                if len(pending) >= workers * 2:
                    break
            while pending:
                done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for f in done:
                    item = pending.pop(f)
                    res = f.result()
                    if res is not None:
                        yield item, res
                if cancel.is_set():
                    for f in list(pending):
                        if f.cancel():
                            pending.pop(f)
                    continue
                for item in it:
                    pending[pool.submit(run, item)] = item
                    if len(pending) >= workers * 2:
                        break
    finally:
        for st in states:
            try:
                st.close()
            except Exception:
                pass


class _PathLocks:
    """One lock per output file, so two songs that share an output (song.flac and song.mp3 both
    saving song.lrc) are never written at the same moment."""

    def __init__(self):
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, key: str) -> threading.Lock:
        key = os.path.normcase(os.path.abspath(key))
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())


def plan_counts(settings: Settings, paths: Iterable[str]) -> tuple[int, int]:
    """(songs Apply would write, songs Apply confident would write), from one read of the database."""
    thr = float(settings.confident_threshold)
    fthr = float(getattr(settings, "confident_fit", 80))
    st = State(settings.resolved_state_db())
    try:
        scores = st.pending_scores()
        kinds = st.override_kinds()
    finally:
        st.close()
    n = nc = 0
    for p in paths:
        k = state_key(p)
        kind = kinds.get(k)
        if kind == KEEP:
            continue
        if kind == PIN:
            n += 1
            continue
        sc = scores.get(k)
        if sc is None:
            continue
        n += 1
        c, f = sc
        if c is not None and c >= thr and f is not None and f >= fthr:
            nc += 1
    return n, nc


def plan_confident(settings: Settings, paths: Iterable[str], threshold: float | None = None,
                   min_fit: float | None = None) -> list[str]:
    """Songs that can be written without review: no per-file choice, found lyrics whose match is at
    `threshold`% or better (default settings.confident_threshold) and whose fit with the recording is at
    `min_fit`% or better (default settings.confident_fit)."""
    thr = float(settings.confident_threshold if threshold is None else threshold)
    fthr = float(getattr(settings, "confident_fit", 80) if min_fit is None else min_fit)
    st = State(settings.resolved_state_db())
    try:
        scores = st.pending_scores()
        kinds = st.override_kinds()
    finally:
        st.close()
    out = []
    for p in paths:
        k = state_key(p)
        c, f = scores.get(k, (None, None))
        if k not in kinds and c is not None and c >= thr and f is not None and f >= fthr:
            out.append(k)
    return out


def apply_all(settings: Settings, paths: Iterable[str], emit: Callable[[dict], None] | None = None,
              cancel: threading.Event | None = None) -> dict:
    emit = emit or (lambda e: None)
    cancel = cancel or threading.Event()
    mode = settings.output_mode if settings.output_mode in (EMBED, LRC) else EMBED
    todo = plan(settings, paths)
    counts: Counter = Counter()
    os.makedirs(settings.resolved_log_dir(), exist_ok=True)
    csv_path = os.path.join(settings.resolved_log_dir(),
                            f"apply-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{mode}.csv")
    locks = _PathLocks()

    def job(path: str, state: State):
        out = lrc_path_for(path) if mode == LRC else path
        emit({"type": "apply_file_start", "path": path})
        with locks.get(out):
            try:
                return _apply_one(path, mode, settings, state)
            except Exception as e:                      # one bad file never stops the others
                return "error", "", f"{type(e).__name__}: {e}"

    emit({"type": "apply_start", "total": len(todo), "mode": mode})
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["time", "status", "source", "path", "output", "detail"])
        w.writeheader()
        for i, (path, (status, source, detail)) in enumerate(_parallel(settings, todo, job, cancel), 1):
            counts[status] += 1
            out = lrc_path_for(path) if mode == LRC else path
            w.writerow({"time": datetime.now().isoformat(timespec="seconds"), "status": status,
                        "source": source, "path": display_path(path), "output": display_path(out),
                        "detail": detail})
            fh.flush()
            emit({"type": "apply_progress", "done": i, "total": len(todo), "path": path,
                  "status": status, "source": source, "detail": detail, "counts": dict(counts)})
    summary = {"type": "apply_done", "counts": dict(counts), "csv": csv_path, "mode": mode,
               "cancelled": cancel.is_set(), "total": len(todo)}
    emit(summary)
    return summary


def _apply_one(path: str, mode: str, s: Settings, state: State) -> tuple[str, str, str]:
    ov = state.get_override(path)
    lrc_src = ""
    if ov is not None and ov[0] == PIN:
        text, source, label, lrc_src = (ov[1] or None), "your choice", ov[2], ov[3] or ""
    else:
        pend = state.get_pending(path)
        if pend is None:
            return "unchanged", "", "nothing to apply"
        source, label, text, lrc_src = pend
    text = s.tidy(text) if text else text
    lp = to_long_path(path)
    try:
        tf = TagFile(lp)
    except UnsupportedFile as e:
        return "error", source, str(e)
    except OSError as e:
        return "error", source, f"{type(e).__name__}: {e}"
    meta = tf.read()
    cur = current_texts(tf, mode, meta.lyrics)
    same = (text and any(lrcmod.same_lyrics(t, text) for t in cur)) or (not text and not cur)
    notes = []
    if same:
        status = "unchanged"
    else:
        state.record_original(path, mode, meta.lyrics, read_lrc_file(lp) if mode == LRC else None)
        try:
            write_output(tf, text, mode, s.restore_mtime, s.lrc_action)
        except (VerifyError, OSError) as e:
            return "error", source, str(e)
        status = "lrc_written" if mode == LRC else "embedded"
    if lrc_src and s.lrc_action != "keep" and not (mode == LRC and same_file(lrc_src, lrc_path_for(path))):
        note = dispose(lrc_src, s.lrc_action)
        if note:
            notes.append(note)
    state.clear_pending(path)
    try:
        st = os.stat(lp)
        info = state.get_file_info(path)
        state.set_file(path, st.st_size, st.st_mtime_ns, status if status != "unchanged" else "already_embedded",
                       label, source if status != "unchanged" else mode, meta.artist, meta.title,
                       info.get("local", ""), info.get("online", ""), meta.album)
    except OSError:
        pass
    state.commit()
    return status, source, "; ".join([label] + notes) if label else "; ".join(notes)


RESTORABLE = {"embedded", "lrc_written", "locked", "already_embedded"}


def restore_originals(settings: Settings, paths: Iterable[str], emit: Callable[[dict], None] | None = None,
                      cancel: threading.Event | None = None) -> dict:
    """Put back what each file held before this app first wrote to it: the original embedded
    lyrics (or none) and/or the original <song>.lrc (or delete the one we created).
    Sidecar .lrc files that were moved to the Recycle Bin are not brought back."""
    emit = emit or (lambda e: None)
    cancel = cancel or threading.Event()
    counts: Counter = Counter()
    todo = [state_key(p) for p in paths]
    locks = _PathLocks()

    def job(path: str, state: State):
        emit({"type": "restore_file_start", "path": path})
        with locks.get(lrc_path_for(path)):
            try:
                return _restore_one(path, settings, state)
            except Exception as e:
                return "error", f"{type(e).__name__}: {e}"

    emit({"type": "restore_start", "total": len(todo)})
    for i, (path, (status, detail)) in enumerate(_parallel(settings, todo, job, cancel), 1):
        counts[status] += 1
        emit({"type": "restore_progress", "done": i, "total": len(todo), "path": path,
              "status": status, "detail": detail, "counts": dict(counts)})
    summary = {"type": "restore_done", "counts": dict(counts), "cancelled": cancel.is_set()}
    emit(summary)
    return summary


def _restore_one(path: str, s: Settings, state: State) -> tuple[str, str]:
    emb = state.get_backup(path)
    lrc_b = state.get_lrc_backup(path)
    if emb is None and lrc_b is None:
        return "nothing", "no record of an earlier write"
    lp = to_long_path(path)
    notes = []
    try:
        if emb is not None:
            tf = TagFile(lp)
            cur = tf.read().lyrics
            if (emb and not any(lrcmod.same_lyrics(t, emb) for t in cur)) or (not emb and cur):
                write_lyrics(tf, emb or None, s.restore_mtime)
                notes.append("original lyrics put back" if emb else "added lyrics removed")
        if lrc_b is not None:
            existed, text = lrc_b
            cur = read_lrc_file(lp)
            if existed and not lrcmod.same_lyrics(cur, text):
                write_lrc_file(lp, text)
                notes.append("original .lrc put back")
            elif not existed and cur is not None:
                write_lrc_file(lp, None, "recycle")
                notes.append(".lrc created by the app moved to the Recycle Bin")
    except (UnsupportedFile, VerifyError, OSError) as e:
        return "error", f"{type(e).__name__}: {e}"
    state.clear_backups(path)
    try:
        st = os.stat(lp)
        info = state.get_file_info(path)
        state.set_file(path, st.st_size, st.st_mtime_ns, "restored", "; ".join(notes) or "already original",
                       "", info.get("artist", ""), info.get("title", ""), info.get("local", ""),
                       info.get("online", ""), info.get("album", ""))
    except OSError:
        pass
    state.commit()
    return ("restored" if notes else "unchanged"), "; ".join(notes) or "already original"
