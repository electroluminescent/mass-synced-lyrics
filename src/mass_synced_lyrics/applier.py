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
    state = State(settings.resolved_state_db())
    emit({"type": "apply_start", "total": len(todo), "mode": mode})
    try:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=["time", "status", "source", "path", "output", "detail"])
            w.writeheader()
            for i, path in enumerate(todo, 1):
                if cancel.is_set():
                    break
                status, source, detail = _apply_one(path, mode, settings, state)
                counts[status] += 1
                out = lrc_path_for(path) if mode == LRC else path
                w.writerow({"time": datetime.now().isoformat(timespec="seconds"), "status": status,
                            "source": source, "path": display_path(path), "output": display_path(out),
                            "detail": detail})
                fh.flush()
                emit({"type": "apply_progress", "done": i, "total": len(todo), "path": path,
                      "status": status, "source": source, "detail": detail, "counts": dict(counts)})
    finally:
        state.close()
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
    state = State(settings.resolved_state_db())
    emit({"type": "restore_start", "total": len(todo)})
    try:
        for i, path in enumerate(todo, 1):
            if cancel.is_set():
                break
            status, detail = _restore_one(path, settings, state)
            counts[status] += 1
            emit({"type": "restore_progress", "done": i, "total": len(todo), "path": path,
                  "status": status, "detail": detail, "counts": dict(counts)})
    finally:
        state.close()
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
