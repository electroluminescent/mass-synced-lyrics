"""What the desktop app does (Find, Align, Apply, the song list, the session), without the window.

The window (Electron) calls the methods listed in RPC and draws the events this sends:
  log, activity, find, applybar, rows, review, review_audio, settings, dialog, select, align_error."""
from __future__ import annotations

import dataclasses
import os
import threading
import time

from . import APP_TITLE, DEFAULT_ACOUSTID_KEY, __version__
from .config import Settings, load_settings, save_settings, snap5
from .engine import STATUS_LABELS, Engine, display_path
from .paths import state_key
from .review_session import GPU, ReviewSession, fmt_left
from .songlist import SongList

MAX_LOG_LINES = 3000
SNAP = {"match_threshold": (70, 100), "confident_threshold": (50, 100), "confident_fit": (0, 100),
        "ai_align_min": (50, 100), "list_min_match": (0, 100), "list_min_fit": (0, 100)}


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


class Activity:
    """One row per operation still running (shown at the top of the window)."""

    def __init__(self, app):
        self.app = app
        self.rows: dict[str, dict] = {}
        self._job = None

    def is_active(self, key: str) -> bool:
        return key in self.rows

    def set(self, key: str, title: str | None = None, value: float | None = None, maximum: float | None = None,
            detail: str | None = None, stop: str | None = None) -> None:
        """Create or update a row. value None = busy without a known amount (moving bar).
        stop: which operation its Stop button stops."""
        row = self.rows.setdefault(key, {"title": "", "detail": "", "value": None, "max": 100.0, "stop": ""})
        if title is not None:
            row["title"] = title
        if detail is not None:
            row["detail"] = detail
        if stop is not None:
            row["stop"] = stop
        if value is None:
            row["value"] = None
        else:
            row["value"] = float(value)
            row["max"] = max(1e-9, float(maximum or 100.0))
        self._changed()

    def detail(self, key: str) -> str:
        return self.rows.get(key, {}).get("detail", "")

    def value(self, key: str):
        return self.rows.get(key, {}).get("value")

    def done(self, key: str) -> None:
        if self.rows.pop(key, None) is not None:
            self._changed()

    def _changed(self) -> None:
        if self._job is None:
            self._job = self.app.loop.after(30, self._send)

    def _send(self) -> None:
        self._job = None
        self.app.emit("activity", {"rows": [dict(v, key=k) for k, v in self.rows.items()]})


class Backend:
    def __init__(self, server, settings: Settings | None = None):
        self.server = server
        self.loop = server.loop
        self.settings = settings or load_settings()
        self.activity = Activity(self)
        self.rows = SongList(self)
        self.review = ReviewSession(self)
        self.worker: threading.Thread | None = None
        self.cancel = threading.Event()
        self.apply_worker: threading.Thread | None = None
        self.apply_cancel = threading.Event()
        self.ai_install_worker = None
        self.ai_install_cancel = None
        self.align_service = None
        self._held_aligner = None
        self._held_lock = threading.Lock()
        self.last_csv = ""
        self._log_buf: list[str] = []
        self._log_job = None
        self._session_dirty = False
        self._session_writer = None
        self._apply_bar_shown = False
        self._apply_bar_refreshed = 0.0
        self._apply_bar_cost = 0.0
        self._apply_counts = (0, 0)
        self._count_worker = None
        self._switched_to_review = True
        self._align_tick_job = None
        self._align_paused_job = None
        self._apply_state = {"running": False, "value": 0, "max": 1, "text": ""}
        self.find_state = {"running": False, "status": "Ready.", "value": 0, "max": 1, "busy": False,
                           "csv": False}
        self._closed = False
        self.loop.after(30000, self._autosave_session)

    # ================================================================ plumbing
    def emit(self, name: str, data=None) -> None:
        self.server.event(name, data)

    def events_put(self, e: dict) -> None:
        """Worker threads (Find, Align, Apply) report here; handled on the loop."""
        self.loop.post(self._handle, e)

    def dialog(self, kind: str, text: str, title: str = APP_TITLE) -> None:
        self.emit("dialog", {"kind": kind, "text": text, "title": title})

    def log(self, text: str) -> None:
        if text:
            text = time.strftime("%H:%M:%S  ") + text
        self._log_buf.append(text)
        if self._log_job is None:
            self._log_job = self.loop.after(50, self._flush_log)

    def _flush_log(self) -> None:
        self._log_job = None
        lines, self._log_buf = self._log_buf[-MAX_LOG_LINES:], []
        if lines:
            self.emit("log", {"lines": lines})

    def _clear_log(self) -> None:
        self._log_buf = []
        self.emit("log", {"clear": True, "lines": []})

    def _find(self, **kw) -> None:
        self.find_state.update(kw)
        self.emit("find", dict(self.find_state))

    def rpc(self, method: str):
        name = RPC.get(method)
        return getattr(self, name) if name else None

    # ================================================================ start, settings
    def rpc_init(self) -> dict:
        from .session import load_session

        out = {"version": __version__, "title": APP_TITLE, "settings": self.settings_dict(),
               "default_key": bool(DEFAULT_ACOUSTID_KEY), "session": None, "loaded": 0}
        data = load_session()
        if data and not self.rows.rows:
            mode = self.settings.session_restore
            if mode == "load":
                out["loaded"] = self.rows.load_session_rows(data, self.settings.session_include_saved)
                self.log(f"Loaded your last session ({out['loaded']} songs).")
                out["ui"] = dict(self.rows.ui)
            elif mode != "fresh":
                import datetime as _dt
                out["session"] = {"n": len(data["rows"]), "folder": data.get("music_folder") or "your music folder",
                                  "when": _dt.datetime.fromtimestamp(data.get("saved", 0)).strftime("%Y-%m-%d %H:%M")}
        self.emit("find", dict(self.find_state))
        return out

    def settings_dict(self) -> dict:
        return dataclasses.asdict(self.settings)

    def _save_settings(self) -> None:
        try:
            save_settings(self.settings)
        except OSError:
            pass

    def rpc_settings_set(self, patch: dict) -> dict:
        s = self.settings
        names = {f.name for f in dataclasses.fields(Settings)}
        changed = set()
        for k, v in (patch or {}).items():
            if k not in names:
                continue
            if k in SNAP:
                v = snap5(v, *SNAP[k])
            elif k in ("collection", "list_columns"):
                v = list(v or [])
            elif k in ("checkpoint_seconds",):
                try:
                    v = max(0.1, float(v))
                except (TypeError, ValueError):
                    continue
            if getattr(s, k) != v:
                setattr(s, k, v)
                changed.add(k)
        self._save_settings()
        if changed & {"confident_threshold", "confident_fit", "output_mode"}:
            self.refresh_apply_bar()
        if "output_mode" in changed:
            self.review.reload()
        if changed & {"tidy_trim", "tidy_quotes"} and self.review.song is not None:
            self.review._recompute()
        if "ripple" in changed:
            self.review.set_ripple(s.ripple)
        if "ai_keep_loaded" in changed:
            self._keep_loaded_changed()
        if "sync_words" in changed:
            self.log("Synced lyrics: to the word — every song is aligned on the GPU; review each before saving."
                     if s.sync_words else "Synced lyrics: to the line.")
        if changed:
            self.emit("settings", self.settings_dict())
        return self.settings_dict()

    def _keep_loaded_changed(self) -> None:
        if not self.settings.ai_keep_loaded:
            self._release_held_aligner()
            svc = self._align_service(create=False)
            if svc is not None:
                svc.release_idle()
            self.log("Align: GPU models are released after 2 minutes without work.")
        else:
            self.log("Align: GPU models stay loaded between songs and runs.")

    def rpc_paths(self) -> dict:
        d = self.settings.resolved_log_dir()
        os.makedirs(d, exist_ok=True)
        return {"logs": d, "csv": self.last_csv if self.last_csv and os.path.exists(self.last_csv) else ""}

    def rpc_busy(self) -> dict:
        return {"finding": bool(self.worker and self.worker.is_alive()),
                "applying": bool(self.apply_worker and self.apply_worker.is_alive())}

    # ================================================================ Find
    def rpc_find_start(self) -> dict:
        if self.activity.is_active("install"):
            return {"info": "The GPU add-on is still installing. Finding starts once it has finished, or after "
                            "you stop the installation (top of the window)."}
        if self.worker and self.worker.is_alive():
            return {}
        s = self.settings
        if not s.music_folder or not os.path.isdir(s.music_folder):
            return {"error": "Choose an existing music folder first."}
        self.activity.set("find", "Finding lyrics", None, detail="starting…", stop="find")
        self.rows.clear()                         # the list shows this run's songs only
        self.rows.ui["only_found"] = s.only_found
        self.session_changed()
        self._save_settings()
        s = dataclasses.replace(s, dry_run=True)   # find only; the bottom bar writes
        self._switched_to_review = False
        svc = self._align_service(create=False)
        if svc is not None:
            svc.reset_counts()
        self.cancel = threading.Event()
        self._clear_log()
        self._find(running=True, status="Scanning…", busy=True, value=0, max=1)
        self.refresh_apply_bar()
        self.worker = threading.Thread(target=self._run_engine, args=(s,), daemon=True)
        self.worker.start()
        return {"started": True, "only_found": s.only_found}

    def _run_engine(self, s: Settings) -> None:
        try:
            Engine(s, self.events_put, self.cancel).run()
        except Exception as e:
            self.events_put({"type": "fatal", "text": f"{type(e).__name__}: {e}"})

    def rpc_find_stop(self) -> None:
        self.cancel.set()
        if self.activity.is_active("find"):
            self.activity.set("find", detail="stopping after the current songs…")
        svc = self._align_service(create=False)
        if svc is not None:
            svc.stop()
            self.align_service = None
        self.activity.done("align")
        self._find(status="Stopping after the current file…", running=bool(self.worker and self.worker.is_alive()),
                   stopping=True)

    # ================================================================ Apply bar
    def show_apply_bar(self) -> None:
        if not self._apply_bar_shown:
            self._apply_bar_shown = True
            self.refresh_apply_bar()

    def refresh_apply_bar(self, background: bool = False) -> None:
        if not self._apply_bar_shown:
            return
        if self.apply_worker and self.apply_worker.is_alive():
            self._emit_applybar()
            return
        self._apply_bar_refreshed = time.monotonic()
        paths = self.rows.paths()
        settings = dataclasses.replace(self.settings)

        def count():
            t0 = time.monotonic()
            try:
                from .applier import plan_counts

                n, nc = plan_counts(settings, paths)
            except Exception:
                n = nc = 0
            return n, nc, time.monotonic() - t0

        if background and len(paths) > 2000:
            prev = self._count_worker
            if prev is not None and prev.is_alive():
                return
            self._count_worker = threading.Thread(
                target=lambda: self.events_put({"type": "apply_counts", "counts": count()}), daemon=True)
            self._count_worker.start()
            return
        self._set_apply_counts(*count())

    def _set_apply_counts(self, n: int, nc: int, cost: float = 0.0) -> None:
        self._apply_bar_cost = cost
        self._apply_counts = (n, nc)
        self._emit_applybar()

    def _emit_applybar(self) -> None:
        n, nc = self._apply_counts
        applying = bool(self.apply_worker and self.apply_worker.is_alive())
        running = bool(self.worker and self.worker.is_alive())
        target = "embed into" if self.settings.output_mode == "embed" else "save .lrc for"
        text = self._apply_state["text"] if applying or self._apply_state["text"] else \
            (f"{n} file(s) ready" + (" — still finding…" if running else ""))
        if not applying and self._apply_state.get("done"):
            text = self._apply_state["text"]
        self.emit("applybar", {"shown": self._apply_bar_shown, "n": n, "nc": nc, "target": target,
                               "applying": applying, "finding": running, "text": text,
                               "value": self._apply_state["value"], "max": self._apply_state["max"],
                               "mode": self.settings.output_mode})

    def _first_reviewable(self, path: str) -> None:
        self._switched_to_review = True
        self.emit("select", {"tab": "review", "song": state_key(path), "only_if_none": True})
        self.log("Lyrics found — opened Review & player. You can review, edit and save songs while the rest "
                 "are still loading.")

    # ================================================================ Apply / restore
    def _word_synced(self, paths) -> int:
        from .ai_align import has_word_times
        from .state import State

        st = State(self.settings.resolved_state_db())
        n = 0
        try:
            for p in paths:
                ov = st.get_override(p)
                row = ov if ov is not None and ov[1] else st.get_pending(p)
                text = (ov[1] if row is ov else row[2]) if row else ""
                n += bool(has_word_times(text))
        finally:
            st.close()
        return n

    def rpc_apply_plan(self, paths: list[str] | None = None) -> dict:
        """What Apply (or Save now) would do: the question to ask before writing."""
        from .applier import plan

        s = self.settings
        paths = self.rows.paths() if paths is None else list(paths)
        planned = plan(s, paths)
        n = len(planned)
        if not n:
            return {"n": 0, "info": "Nothing to write for the selected song(s): their choice is Don't change, or "
                                    "the file already has those lyrics."}
        if s.output_mode == "embed":
            msg = (f"Write lyrics into {n} audio file(s)? Existing lyrics in those files are replaced "
                   "(originals are kept in the app's backup).")
        else:
            msg = f"Save a .lrc file beside {n} audio file(s)? Existing .lrc files with the same name are replaced."
        if s.lrc_action == "delete":
            msg += "\n\nUsed sidecar .lrc files will be permanently deleted."
        elif s.lrc_action == "recycle":
            msg += "\n\nUsed sidecar .lrc files will be moved to the Recycle Bin."
        w = self._word_synced(planned)
        if w:
            msg += (f"\n\n{w} of them have word-synced lyrics: their word times come only from the GPU aligner. "
                    "Reviewing every one in the player first is recommended.")
        return {"n": n, "question": msg, "word_synced": w}

    def rpc_apply_confident_plan(self) -> dict:
        from .applier import plan_confident

        paths = plan_confident(self.settings, self.rows.paths())
        w = self._word_synced(paths) if paths else 0
        out = {"paths": paths, "word_synced": w}
        if w:
            out["question"] = (f"{w} of these {len(paths)} song(s) have word-synced lyrics. Their word times come "
                               "only from the GPU aligner, so reviewing every one in the player before saving is "
                               "recommended.\n\nSave them now anyway?")
        return out

    def rpc_apply_start(self, paths: list[str] | None = None) -> dict:
        if self.apply_worker and self.apply_worker.is_alive():
            return {}
        from .applier import plan

        s = self.settings
        paths = self.rows.paths() if paths is None else list(paths)
        n = len(plan(s, paths))
        if not n:
            return {"info": "Nothing to write."}
        self._save_settings()
        self.emit("player", {"stop": True})
        self.apply_cancel = threading.Event()
        self._apply_state = {"running": True, "value": 0, "max": max(1, n), "text": "Starting…"}

        def work():
            from .applier import apply_all
            try:
                apply_all(s, paths, self.events_put, self.apply_cancel)
            except Exception as e:
                self.events_put({"type": "apply_fatal", "text": f"{type(e).__name__}: {e}"})
        self.apply_worker = threading.Thread(target=work, daemon=True)
        self.apply_worker.start()
        self._emit_applybar()
        self._find()
        return {"started": True}

    def rpc_restore_start(self, paths: list[str]) -> dict:
        if self.apply_worker and self.apply_worker.is_alive():
            return {}
        paths = list(paths or [])
        if not paths:
            return {}
        s = self.settings
        self.emit("player", {"stop": True})
        self.apply_cancel = threading.Event()
        self._apply_state = {"running": True, "value": 0, "max": max(1, len(paths)), "text": "Restoring…"}

        def work():
            from .applier import restore_originals
            try:
                restore_originals(s, paths, self.events_put, self.apply_cancel)
            except Exception as e:
                self.events_put({"type": "apply_fatal", "text": f"{type(e).__name__}: {e}"})
        self.apply_worker = threading.Thread(target=work, daemon=True)
        self.apply_worker.start()
        self._emit_applybar()
        return {"started": True}

    def rpc_apply_stop(self) -> None:
        self.apply_cancel.set()

    def _apply_finished(self, text: str) -> None:
        self.apply_worker = None
        self._apply_state = {"running": False, "value": 0, "max": 1, "text": text, "done": True}
        self.refresh_apply_bar()
        self._find()

    # ================================================================ session
    def session_changed(self) -> None:
        self._session_dirty = True

    def song_shown(self, prev, path) -> None:
        self.rows.ui["selected"] = path

    def save_session_now(self, background: bool = False) -> None:
        if not self.rows.rows:
            return
        from .session import save_session
        d = self.rows.session_data()
        rows = {p: dict(r) for p, r in d["rows"].items()}
        extra = {k: v for k, v in d.items() if k not in ("rows", "selected", "filter", "search")}
        args = (rows, d["selected"], d["filter"], d["search"], self.settings.music_folder)
        self._session_dirty = False

        def write():
            try:
                save_session(*args, extra=extra)
            except OSError:
                self._session_dirty = True
        prev = self._session_writer
        if background:
            if prev is not None and prev.is_alive():
                self._session_dirty = True
                return
            self._session_writer = threading.Thread(target=write, daemon=True)
            self._session_writer.start()
        else:
            if prev is not None and prev.is_alive():
                prev.join(10)
            write()

    def _autosave_session(self) -> None:
        if self._session_dirty:
            self.save_session_now(background=True)
        self.loop.after(30000, self._autosave_session)

    def rpc_session_load(self, load: bool, remember: bool = False, include_saved: bool = False) -> dict:
        from .session import load_session

        if remember:
            self.settings.session_restore = "load" if load else "fresh"
            self.settings.session_include_saved = bool(include_saved)
            self._save_settings()
            self.emit("settings", self.settings_dict())
        if not load:
            return {"loaded": 0}
        data = load_session()
        if not data:
            return {"loaded": 0}
        n = self.rows.load_session_rows(data, include_saved)
        self.log(f"Loaded your last session ({n} songs).")
        return {"loaded": n, "ui": dict(self.rows.ui)}

    def rpc_session_ui(self, **ui) -> None:
        """What the list shows (selection, filter, search), kept in the session file."""
        for k in ("selected", "filter", "search", "only_found"):
            if k in ui and self.rows.ui.get(k) != ui[k]:
                self.rows.ui[k] = ui[k]
                self.session_changed()

    # ================================================================ song list
    def rpc_rows_get(self) -> dict:
        return {"rows": self.rows.rows}

    def rpc_rows_add(self, path: str) -> str:
        p = state_key(os.path.normpath(path))
        if p not in self.rows.rows:
            self.rows.add_row(p, "")
        self.rows.flush()
        self.show_apply_bar()
        return p

    def rpc_rows_remove(self, songs: list[str]) -> int:
        return self.rows.remove(songs)

    def rpc_rows_load_earlier(self) -> int:
        try:
            return self.rows.load_earlier()
        except Exception as e:
            self.dialog("error", f"Could not read earlier results:\n{e}")
            return 0

    def rpc_rows_cycle(self, path: str) -> str:
        return self.rows.cycle_choice(path)

    def rpc_rows_cycle_many(self, songs: list[str], node: str) -> str:
        return self.rows.cycle_many(songs, node)

    def rpc_rows_set_choice(self, songs: list[str], kind: str) -> int:
        return self.rows.set_many(songs, kind)

    def rpc_rows_reset(self, songs: list[str]) -> None:
        self.rows.reset_many(songs)

    def rpc_prioritize(self, paths: list[str]) -> None:
        """The song you selected goes next in every running operation (Find, Align, Save)."""
        from . import priority
        priority.want_first(paths)
        svc = self._align_service(create=False)
        if svc is not None:
            svc.prioritize(paths)

    def rpc_cache_clear(self, songs: list[str]) -> dict:
        from .state import State

        songs = list(songs or [])
        if not songs:
            return {"info": "Select songs in the Review & player list first."}
        rv = self.review
        if rv.song is not None and rv.song.path in songs:
            rv.forget_gpu_timing()
        st = State(self.settings.resolved_state_db())
        cleared = 0
        try:
            from .ai_align import forget_vocals
            for p in songs:
                gone = st.clear_cache(p)
                if forget_vocals(p) or gone:
                    cleared += 1
                if p in self.rows.rows:
                    r = self.rows.rows[p]
                    if r.get("status") == "aligned":
                        r["status"] = "proposed"
                    r["check"] = False
                    if r.get("level") == "aligned":
                        r["level"] = ""
                    self.rows.add_row(p, ops={"align": ""})
        finally:
            st.close()
        self.log(f"Cleared cached data for {cleared} of {len(songs)} song(s). Run Find lyrics or Align to work "
                 "them out again.")
        if rv.song is not None and rv.song.path in songs:
            rv.reload()
        self.refresh_apply_bar()
        return {"cleared": cleared}

    # ================================================================ GPU add-on
    def rpc_ai_toggle(self, on: bool) -> dict:
        from . import ai_align

        if not on:
            self._ai_set(False)
            return {"ok": True}
        gpu = ai_align.nvidia_gpu()
        if gpu is None:
            self._ai_set(False)
            return {"error": "Align low-confidence lyrics needs an NVIDIA graphics card with its driver installed, "
                             "and none was found (nvidia-smi isn't available)."}
        if ai_align.is_installed():
            self._ai_set(True)
            self.log(f"Align: on — {gpu}.")
            return {"ok": True}
        if self.ai_install_worker is not None and self.ai_install_worker.is_alive():
            return {"installing": True}
        self._ai_set(False)
        return {"need_install": True,
                "question": f"Found {gpu}.\n\nAligning needs a one-time add-on: PyTorch with CUDA, Whisper and "
                            f"Demucs, about 3 GB downloaded from pypi.org and download.pytorch.org into\n"
                            f"{ai_align.addon_dir()}\n\nThe speech and vocal-separation models (about 1.6 GB more) "
                            "download the first time it runs. It needs Python 3.10–3.12 (python.org) to be "
                            "installed.\n\nInstall it now?"}

    def rpc_ai_install(self) -> dict:
        from . import ai_align

        if self.ai_install_worker is not None and self.ai_install_worker.is_alive():
            return {}
        self.log("Align: installing the GPU add-on — this takes a while…")
        self.ai_install_cancel = threading.Event()
        self.activity.set("install", "Installing GPU add-on", 0, 100, detail="starting…", stop="install")
        self._find()
        last = {"t": 0.0, "f": -1.0}
        cancel = self.ai_install_cancel

        def progress(frac, title, detail):
            now = time.monotonic()
            if frac >= 1.0 or abs(frac - last["f"]) >= 0.002 or now - last["t"] > 0.5:
                last["t"], last["f"] = now, frac
                self.events_put({"type": "ai_install_progress", "frac": frac, "title": title, "detail": detail})

        def work():
            ok = False
            try:
                ok = ai_align.install(lambda t: self.events_put({"type": "log", "text": t}), cancel, progress)
            except Exception as e:
                self.events_put({"type": "log", "text": f"Align: install failed: {type(e).__name__}: {e}"})
            self.events_put({"type": "ai_installed", "ok": ok})
        self.ai_install_worker = threading.Thread(target=work, daemon=True)
        self.ai_install_worker.start()
        return {"started": True}

    def _ai_install_stop(self) -> None:
        if self.ai_install_cancel is not None:
            self.ai_install_cancel.set()
        self.activity.set("install", detail="stopping…")

    def _ai_set(self, on: bool) -> None:
        if self.settings.ai_align != on:
            self.settings.ai_align = on
            self._save_settings()
        self.emit("settings", self.settings_dict())

    def rpc_ai_state(self) -> dict:
        return {"on": bool(self.settings.ai_align), "installing": self.activity.is_active("install")}

    # ================================================================ GPU aligner
    def _align_service(self, create: bool = True):
        from . import ai_align

        svc = self.align_service
        if svc is None and create:
            svc = self.align_service = ai_align.AlignService(lambda: self.settings, self.events_put,
                                                             self._take_aligner, handoff=self._hold_aligner)
        return svc

    def align_service_get(self):
        return self._align_service()

    def _take_aligner(self, s):
        from . import ai_align

        with self._held_lock:
            al, self._held_aligner = self._held_aligner, None
        if al is not None and al.running() and al.model == getattr(s, "ai_align_model", "medium"):
            return al
        if al is not None:
            al.close()
        return ai_align.Aligner(getattr(s, "ai_align_model", "medium"))

    def _hold_aligner(self, al) -> None:
        with self._held_lock:
            old, self._held_aligner = self._held_aligner, al
        if old is not None and old is not al:
            old.close()

    def _release_held_aligner(self) -> None:
        with self._held_lock:
            al, self._held_aligner = self._held_aligner, None
        if al is not None:
            al.close()

    def queue_alignment(self, paths, force: bool = False, words: bool | None = None) -> int:
        if not self.settings.ai_align or not paths:
            return 0
        n = self._align_service().submit(paths, force=force, words=words)
        if n:
            self._align_activity()
        return n

    def align_paths(self, paths: list[str] | None = None, everything: bool = True) -> None:
        from . import ai_align

        if paths is None:
            paths = ai_align.pick_low_confidence(self.settings, self.rows.paths())
        self.queue_alignment(paths, force=everything)

    def rpc_align_command(self, which: str, songs: list[str] | None = None, words: bool | None = None) -> dict:
        """Align (GPU): "low" = the low-confidence songs in the list, "all" = every song in the list,
        "songs" = these songs. The window has made sure alignment is on (and warned about words)."""
        from . import ai_align

        if not self.settings.ai_align:
            return {"info": "Switch on Align low-confidence lyrics first."}
        if which == "all":
            songs = self.rows.paths()
            if not songs:
                return {"info": "There are no songs in the Review & player list."}
            n = self.queue_alignment(songs, force=True, words=words)
            self.log(f"Align: {n} song(s) queued for the GPU (songs already aligned are skipped quickly).")
            return {"queued": n}
        if which in ("selected", "songs"):
            songs = list(songs or [])
            if not songs:
                return {"info": "Select songs in the Review & player list first."}
            n = self.queue_alignment(songs, force=True, words=words)
            if words:
                self.log(f"Align: {n} song(s) queued for word-synced lyrics — review each before saving.")
            if n < len(songs):
                self.log(f"Align: {len(songs) - n} of the {len(songs)} song(s) were already waiting.")
        else:
            paths = ai_align.pick_low_confidence(self.settings, self.rows.paths())
            if not paths:
                return {"info": "No songs in the list are below High confidence "
                                f"({self.settings.confident_threshold}%) or Good fit "
                                f"({self.settings.confident_fit}%) with lyrics still to save."}
            n = self.queue_alignment(paths)
        self.log(f"Align: {n} song(s) queued for the GPU.")
        return {"queued": n}

    def rpc_align_stop(self) -> None:
        svc = self._align_service(create=False)
        if svc is not None:
            svc.stop()
        self.align_service = None
        self.activity.done("align")
        self.loop.after(200, lambda: self.rows.clear_busy("align"))
        self.emit("align_error", None)
        self.log("Align: stopped. Songs not aligned yet keep their found lyrics.")

    def rpc_align_error_choice(self, what: str) -> None:
        """The aligner failed and the user chose: ignore (keep aligning), disable, or cancel the run."""
        svc = self._align_service(create=False)
        if self._align_paused_job is not None:
            self.loop.cancel(self._align_paused_job)
            self._align_paused_job = None
        if what == "ignore":
            self.log("Align: error ignored — aligning the waiting songs.")
            if svc is not None:
                svc.resume()
        elif what == "disable":
            if svc is not None:
                svc.stop()
            self.align_service = None
            self._ai_set(False)
            self.log("Align: switched off. Songs keep their found lyrics; switch it on again under Scores and "
                     "alignment.")
        else:
            if svc is not None:
                svc.stop()
            self.align_service = None
            if self.worker and self.worker.is_alive():
                self.rpc_find_stop()
            self.log("Align: run cancelled.")
        self._align_activity()
        self._find()

    def _align_paused_tick(self) -> None:
        self._align_paused_job = None
        svc = self._align_service(create=False)
        if svc is None or not svc.paused:
            return
        self.emit("align_waiting", {"waiting": svc.pending()})
        self._align_paused_job = self.loop.after(1000, self._align_paused_tick)

    def _align_event(self, e: dict) -> None:
        t = e["type"]
        svc = self._align_service(create=False)
        waiting = svc.pending() if svc is not None else 0
        if t == "align_progress":
            name = display_path(e["path"])
            self.rows.set_op(e["path"], "align", "partial" if e.get("partial") else {
                "aligned": "done", "rejected": "skip", "error": "fail"}.get(e["status"], "skip"))
            self._find(status=f"Aligning on the GPU · {svc.done if svc else 0} done"
                              + (f" · {waiting} waiting" if waiting else ""))
            if e["status"] == "aligned" and str(e.get("reason", "")).startswith("already aligned"):
                self.log(f"Skipped (already aligned on the GPU): {name}")
            elif e["status"] == "aligned":
                kind = e.get("kind") or "online"
                kw = {f"conf_{kind}": e.get("confidence"), f"fit_{kind}": e.get("fit")}
                key = state_key(e["path"])
                if key in self.rows.rows:
                    row = self.rows.rows[key]
                    row["check"] = bool(e.get("partial") or e.get("check"))
                    if not row.get("level") or row.get("level") == "found":
                        row["level"] = "aligned"
                    self.rows.add_row(e["path"], "aligned", **kw)
                if e.get("check"):
                    self.log(f"Check: {name} — the aligned timing is {e.get('shift', 0):.1f} s from the found "
                             "timing (typical line): the found lyrics may be for another version, or the GPU "
                             "misheard. Compare Found and Aligned in the player.")
                if e.get("partial"):
                    self.log(f"Partly aligned ({e['verify']:.0f}% of words heard, below "
                             f"{self.settings.ai_align_min}%): {name} — {e['reason']}. Only the lines it was sure "
                             "of were placed; its Match isn't raised, so check it in the player.")
                else:
                    self.log(f"Aligned ({e['verify']:.0f}% of words heard): {name} — {e['reason']}")
                if self.review.song is not None and self.review.song.path == key:
                    self.review.reload()
                    self.loop.after(4000, self.review.check_vocals)
            elif e["status"] == "rejected":
                v = e.get("verify")
                self.log(f"Not aligned{f' ({v:.0f}% heard)' if v is not None else ''}: {name} — {e['reason']}"
                         " — keeps its found lyrics")
            elif e["status"] == "error":
                self.log(f"Align error: {name} — {e['reason']} — keeps its found lyrics")
            elif e.get("reason"):
                self.log(f"Not aligned: {name} — {e['reason']}")
            if time.monotonic() - self._apply_bar_refreshed > 1.5:
                self.refresh_apply_bar(background=True)
            return
        if t == "align_error":
            self.log(f"Align: paused — {e['error']}")
            self._find(status=f"Aligning paused · {waiting} waiting")
            self.emit("align_error", {"error": e["error"], "waiting": waiting})
            if self._align_paused_job is None:
                self._align_paused_job = self.loop.after(1000, self._align_paused_tick)
            return
        if svc is not None and svc.done and not (self.worker and self.worker.is_alive()):
            c = e.get("counts", {})
            self._find(status=f"Aligned {c.get('aligned', 0)} of {svc.done} song(s) on the GPU")
        self._align_finish_if_idle()
        self.refresh_apply_bar()
        self.session_changed()

    def _align_activity(self) -> None:
        svc = self._align_service(create=False)
        if svc is None:
            self.activity.done("align")
            return
        finding = bool(self.worker and self.worker.is_alive())
        waiting = svc.pending()
        busy, paused = bool(getattr(svc, "busy", False)), bool(getattr(svc, "paused", False))
        cur = getattr(svc, "current", None)
        song = cur is not None and cur.kind == "song"
        total = svc.done + waiting + (1 if song else 0)
        if not total and not paused:
            if finding:
                self.activity.set("align", GPU + " Aligning on the GPU", None, detail="waiting for songs…",
                                  stop="align")
            elif not busy:
                self.activity.done("align")
            return
        if not paused and not waiting and not song:
            if not finding and not busy:
                self._align_finish_if_idle()
                return
            if not busy:
                self.activity.set("align", GPU + " Aligning on the GPU", svc.done, max(1, total),
                                  detail=f"{svc.done} done · waiting for songs from Find…", stop="align")
                return
        value, detail = float(svc.done), f"{svc.done} of {total}"
        if song:
            snap = cur.snapshot()
            value += snap["frac"]
            left = snap["left"] + waiting * cur.song_estimate()
            detail = (f"Song {svc.done + 1} of {total} · {snap['label']} {snap['stage_frac'] * 100:.0f}% · "
                      f"~{fmt_left(left)} left")
            self.rows.set_align_pct(cur.path, snap["frac"] * 100)
        if paused:
            detail = f"{svc.done} of {total} · paused"
        self.activity.set("align", GPU + " Aligning on the GPU", value, max(1, total), detail=detail, stop="align")

    def align_tick(self) -> None:
        """Refresh the GPU progress a few times a second while something is being aligned."""
        if self._align_tick_job is not None:
            return
        self._align_tick_job = self.loop.after(250, self._align_tick_run)

    def _align_tick_run(self) -> None:
        self._align_tick_job = None
        svc = self._align_service(create=False)
        req = self.review._align_req
        if svc is None:
            if req is not None:
                self.review.cancel_auto_align()
            return
        cur = getattr(svc, "current", None)
        if cur is not None and cur.kind == "song" or svc.pending():
            self._align_activity()
        if req is not None:
            if cur is not None and cur.kind == "raw" and svc.current_token == req:
                snap = cur.snapshot()
                self.activity.set("autoalign", value=snap["frac"] * 100, maximum=100,
                                  detail=f"{snap['label']} {snap['stage_frac'] * 100:.0f}% · "
                                         f"~{fmt_left(snap['left'])} left")
                self.review.auto_align_progress(snap, False)
            else:
                self.activity.set("autoalign", detail="waiting for the GPU…" if svc.busy else "starting…")
                self.review.auto_align_progress(None, svc.busy)
        if req is not None or cur is not None or svc.pending():
            self.align_tick()

    def _align_finish_if_idle(self) -> None:
        svc = self._align_service(create=False)
        if svc is None:
            self.activity.done("align")
            return
        if svc.paused or not svc.idle() or (self.worker and self.worker.is_alive()):
            return
        if svc.done:
            c = svc.counts
            self.log(f"Align: {c.get('aligned', 0)} aligned, {c.get('rejected', 0)} kept as found (below "
                     f"{self.settings.ai_align_min}% or not placeable), {c.get('error', 0)} errors.")
            svc.reset_counts()
        self.activity.done("align")
        self.rows.clear_busy("align")

    def rpc_activity_stop(self, key: str) -> None:
        row = self.activity.rows.get(key)
        stop = (row or {}).get("stop") or key
        if stop == "find":
            self.rpc_find_stop()
        elif stop == "apply":
            self.rpc_apply_stop()
        elif stop == "install":
            self._ai_install_stop()
        elif stop == "align":
            self.rpc_align_stop()
        elif stop == "autoalign":
            self.review.cancel_auto_align()

    # ================================================================ events from the workers
    def _track_activity(self, e: dict) -> None:
        a, t = self.activity, e["type"]
        if t == "scanning":
            a.set("find", "Finding lyrics", None, detail=f"scanning… {e['found']} files", stop="find")
        elif t == "scan":
            n = e["total"]
            a.set("find", "Finding lyrics", 0, max(1, n), detail=f"0 of {n}", stop="find")
        elif t == "result":
            a.set("find", "Finding lyrics", e["done"], max(1, e["total"]), detail=f"{e['done']} of {e['total']}",
                  stop="find")
        elif t in ("done", "fatal"):
            a.done("find")
            self.rows.clear_busy("find")
        elif t in ("apply_start", "restore_start"):
            title = "Saving lyrics" if t == "apply_start" else "Restoring originals"
            a.set("apply", title, 0, max(1, e["total"]), detail=f"0 of {e['total']}", stop="apply")
        elif t in ("apply_progress", "restore_progress"):
            a.set("apply", value=e["done"], maximum=max(1, e["total"]), detail=f"{e['done']} of {e['total']}")
        elif t in ("apply_done", "restore_done", "apply_fatal"):
            a.done("apply")
            self.rows.clear_busy("save")
        elif t == "ai_install_progress":
            a.set("install", "Installing GPU add-on", round(e["frac"] * 100, 1), 100,
                  detail=e.get("detail") or e.get("title") or "", stop="install")
        elif t == "ai_installed":
            a.done("install")
        elif t in ("align_progress", "align_error", "align_idle"):
            self._align_activity()
        elif t == "align_raw":
            a.done("autoalign")

    def _handle(self, e: dict) -> None:
        t = e["type"]
        self._track_activity(e)
        if t == "log":
            self.log(e["text"])
        elif t == "scanning":
            self._find(status=f"Scanning… {e['found']} audio files so far", busy=True)
        elif t == "scan":
            self._find(busy=False, value=0, max=max(1, e["total"]))
        elif t == "file_start":
            self.rows.set_op(e["path"], "find", "busy",
                             None if state_key(e["path"]) in self.rows.rows else "searching")
        elif t == "align_slow":
            self.log(e["message"])
        elif t == "align_song_start":
            self.rows.set_op(e["path"], "align", "busy")
            self.align_tick()
        elif t in ("apply_file_start", "restore_file_start"):
            self.rows.set_op(e["path"], "save", "busy")
        elif t == "align_raw":
            self.review.align_result(e)
        elif t in ("align_progress", "align_error", "align_idle"):
            self._align_event(e)
        elif t == "ai_installed":
            if e["ok"]:
                self._ai_set(True)
                self.log("Align: the add-on is installed and switched on.")
            else:
                self._ai_set(False)
                self.log("Align: the add-on could not be installed; the option stays off.")
            self._find()
        elif t == "apply_counts":
            self._set_apply_counts(*e["counts"])
        elif t == "apply_start":
            self.log("")
            self.log(f"Applying {e['total']} file(s) — "
                     + ("embedding into the audio files" if e["mode"] == "embed" else "saving .lrc files"))
        elif t == "apply_progress":
            self._apply_state.update(value=e["done"], max=max(1, e["total"]), text=f"Applying {e['done']}/{e['total']}…")
            if state_key(e["path"]) in self.rows.rows:
                self.rows.add_row(e["path"], e["status"] if e["status"] != "unchanged" else "already_embedded",
                                  e["source"], ops={"save": {"error": "fail", "unchanged": "skip"}.get(
                                      e["status"], "done")})
            label = STATUS_LABELS.get(e["status"], e["status"])
            self.log(f"{label}: {display_path(e['path'])}" + (f" — {e['detail']}" if e["detail"] else ""))
            self._emit_applybar()
        elif t == "apply_done":
            c = e["counts"]
            written = c.get("embedded", 0) + c.get("lrc_written", 0)
            self.log(f"Applied: {written} written, {c.get('unchanged', 0)} unchanged, {c.get('error', 0)} errors"
                     + (" (stopped)" if e["cancelled"] else "") + f". Log: {e['csv']}")
            self._apply_finished(f"Done: {written} written, {c.get('error', 0)} errors"
                                 + (" (stopped)" if e["cancelled"] else ""))
            self.save_session_now()
            self.review.reload()
        elif t == "restore_start":
            self.log("")
            self.log(f"Restoring original lyrics for {e['total']} song(s)…")
        elif t == "restore_progress":
            self._apply_state.update(value=e["done"], max=max(1, e["total"]),
                                     text=f"Restoring {e['done']}/{e['total']}…")
            key = state_key(e["path"])
            if e["status"] in ("restored", "unchanged") and key in self.rows.rows:
                self.rows.add_row(e["path"], "restored", ops={"save": ""})
            elif key in self.rows.rows:
                self.rows.set_op(e["path"], "save", "fail" if e["status"] == "error" else "")
            if e["status"] in ("restored", "error"):
                self.log(f"{'Restored' if e['status'] == 'restored' else 'Error'}: {display_path(e['path'])}"
                         + (f" — {e['detail']}" if e["detail"] else ""))
            self._emit_applybar()
        elif t == "restore_done":
            c = e["counts"]
            self.log(f"Restore finished: {c.get('restored', 0)} restored, {c.get('error', 0)} errors"
                     + (" (stopped)" if e["cancelled"] else "") + ".")
            self._apply_finished(f"Restored {c.get('restored', 0)} song(s)")
            self.save_session_now()
            self.review.reload()
        elif t == "apply_fatal":
            self._apply_finished("")
            self.dialog("error", e["text"])
        elif t == "result":
            self._on_result(e)
        elif t == "done":
            self._on_done(e)
        elif t == "fatal":
            self.log("ERROR: " + e["text"])
            self._find(running=False, busy=False, status="Failed.")
            self.refresh_apply_bar()
            self.dialog("error", e["text"])

    def _on_result(self, e: dict) -> None:
        r = e["result"]
        m = r.meta
        self.rows.add_row(r.path, r.detail if r.status == "cached" else r.status, r.source,
                          m.artist if m else "", m.title if m else "", r.local, r.online, r.choice,
                          m.album if m else "", r.conf_local, r.conf_online, r.fit_local, r.fit_online,
                          ops={"find": "fail" if r.status == "error" else "done"})
        self.show_apply_bar()
        if r.status == "proposed" and not self._switched_to_review:
            self._first_reviewable(r.path)
        if r.status == "proposed" and self.settings.ai_align:
            from .ai_align import is_low_confidence
            if getattr(self.settings, "ai_align_all", False) or getattr(self.settings, "sync_words", False):
                self.queue_alignment([r.path], force=True)
            elif is_low_confidence(self.settings, r.score, r.fit):
                self.queue_alignment([r.path])
        if time.monotonic() - self._apply_bar_refreshed > max(1.5, 10 * self._apply_bar_cost):
            self.refresh_apply_bar(background=True)
        if r.status != "cached":
            label = STATUS_LABELS.get(r.status, r.status)
            src = f" [{r.source}]" if r.source and r.status == "embedded" else ""
            det = f" — {r.detail}" if r.detail else ""
            self.log(f"{label}{src}: {display_path(r.path)}{det}")
        c = e["counts"]
        done, total = e["done"], e["total"]
        eta = ""
        if done and e["elapsed"] > 5 and done < total:
            eta = f" · about {fmt_duration(e['elapsed'] / done * (total - done))} left"
        self._find(value=done, max=max(1, total), busy=False,
                   status=f"{done}/{total} · found {c.get('proposed', 0)} · not found "
                          f"{c.get('not_found', 0) + c.get('no_synced', 0)} · ambiguous {c.get('ambiguous', 0)}"
                          f" · errors {c.get('error', 0)}{eta}")

    def _on_done(self, e: dict) -> None:
        self.last_csv = e["csv"]
        c = e["counts"]
        self.log("")
        self.log("Summary:")
        for k, v in sorted(c.items(), key=lambda kv: -kv[1]):
            self.log(f"  {STATUS_LABELS.get(k, k)}: {v}")
        self.log(f"CSV log: {e['csv']}")
        self.log("Nothing has been written yet. Check the Review & player tab if you like, then use the Apply "
                 "button at the bottom.")
        word = "Stopped" if e["cancelled"] else "Finished"
        self._find(running=False, busy=False, stopping=False, csv=True,
                   status=f"{word} in {fmt_duration(e['elapsed'])} · lyrics found for {c.get('proposed', 0)} "
                          "file(s)" + (" · run again to resume" if e["cancelled"] else ""))
        self.show_apply_bar()
        self.refresh_apply_bar()
        self.rows.flush()
        self.save_session_now()
        self.emit("select", {"tab": "review", "first": True})
        if self.settings.ai_align and not e["cancelled"]:
            if getattr(self.settings, "ai_align_all", False) or getattr(self.settings, "sync_words", False):
                self.align_paths(self.rows.paths(), everything=True)
            else:
                self.align_paths(None, everything=False)
        self._align_finish_if_idle()

    # ================================================================ review & player
    def rpc_rv_load(self, path: str) -> None:
        self.review.load_song(path)

    def rpc_rv_state(self) -> dict:
        return self.review.view()

    def rpc_rv_audio_wav(self) -> str:
        return self.review.audio_wav()

    def rpc_rv_call(self, name: str, args: list | None = None) -> object:
        """Player actions (see ReviewSession): rv.call {"name": "toggle_pin", "args": [3]}."""
        if name not in RV_METHODS:
            raise ValueError(f"unknown player action {name}")
        return getattr(self.review, name)(*(args or []))

    # ================================================================ closing
    def rpc_quit(self) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=10)
        if self.ai_install_cancel is not None:
            self.ai_install_cancel.set()
        svc = self._align_service(create=False)
        if svc is not None:
            svc.stop()
            svc.join(timeout=10)
        self._release_held_aligner()
        self.review.shutdown()
        self.save_session_now()
        self._save_settings()
        self._flush_log()
        self.loop.stop()


RV_METHODS = {
    "select_candidate", "set_timing", "toggle_interpolate", "clear_pins", "clear_edits", "toggle_pin",
    "set_line_time", "set_line_time_text", "select_line", "delete_line", "insert_line", "align_line",
    "stretch_to_length", "transfer", "transfer_all", "move_begin", "move", "move_end", "move_selected",
    "resize_begin", "resize", "resize_end", "next_line_to_needle", "note", "auto_align", "cancel_auto_align",
    "add_checkpoint", "delete_checkpoint", "default_checkpoints", "use_level", "apply", "lock", "clear",
    "search_lrclib", "set_ripple", "set_word_time", "time_words", "clear_word_times", "next_word_to_needle",
    "check_vocals",
}

RPC = {
    "app.init": "rpc_init", "app.paths": "rpc_paths", "app.busy": "rpc_busy", "app.quit": "rpc_quit",
    "settings.set": "rpc_settings_set",
    "find.start": "rpc_find_start", "find.stop": "rpc_find_stop",
    "apply.plan": "rpc_apply_plan", "apply.confident_plan": "rpc_apply_confident_plan",
    "apply.start": "rpc_apply_start", "apply.stop": "rpc_apply_stop", "restore.start": "rpc_restore_start",
    "session.load": "rpc_session_load", "session.ui": "rpc_session_ui",
    "rows.get": "rpc_rows_get", "rows.add": "rpc_rows_add", "rows.remove": "rpc_rows_remove",
    "rows.load_earlier": "rpc_rows_load_earlier", "rows.cycle": "rpc_rows_cycle",
    "rows.cycle_many": "rpc_rows_cycle_many", "rows.set_choice": "rpc_rows_set_choice",
    "rows.reset": "rpc_rows_reset", "prioritize": "rpc_prioritize", "cache.clear": "rpc_cache_clear",
    "ai.toggle": "rpc_ai_toggle", "ai.install": "rpc_ai_install", "ai.state": "rpc_ai_state",
    "align.command": "rpc_align_command", "align.stop": "rpc_align_stop",
    "align.error_choice": "rpc_align_error_choice", "activity.stop": "rpc_activity_stop",
    "rv.load": "rpc_rv_load", "rv.state": "rpc_rv_state", "rv.audio_wav": "rpc_rv_audio_wav",
    "rv.call": "rpc_rv_call",
}
