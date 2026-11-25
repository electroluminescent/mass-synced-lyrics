"""Desktop interface (Tkinter)."""
from __future__ import annotations

import dataclasses
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import APP_TITLE, DEFAULT_ACOUSTID_KEY, __version__
from .config import Settings, load_settings, save_settings
from .engine import STATUS_LABELS, Engine, display_path
from .paths import state_key

SESSION_MODES = [("ask", "Ask whether to load the last session"), ("load", "Load the last session"),
                 ("fresh", "Start with an empty list")]
LRC_ACTIONS = [("recycle", "Move to Recycle Bin"), ("delete", "Delete permanently"), ("keep", "Keep")]
MAX_LOG_LINES = 3000


def _fmt_duration(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def open_path(path: str) -> None:
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        messagebox.showerror(APP_TITLE, f"Could not open {path}:\n{e}")


class App(tk.Tk):
    def __init__(self, settings: Settings | None = None):
        super().__init__()
        self.title(f"{APP_TITLE} {__version__}")
        self.minsize(980, 700)
        self.geometry("1440x920")
        self._set_icon()
        self.settings = settings or load_settings()
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel = threading.Event()
        self.last_csv = ""
        self._build()
        self._load_into_widgets()
        self._session_dirty = False
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(400, self._offer_session)
        self.after(30000, self._autosave_session)
        self.after(100, self._poll)

    def _set_icon(self) -> None:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
        try:
            if os.name == "nt":
                self.iconbitmap(default=os.path.join(base, "icon.ico"))
            self._icon_images = [tk.PhotoImage(file=os.path.join(base, n))
                                 for n in ("icon-256.png", "icon-32.png")]
            self.iconphoto(True, *self._icon_images)
        except (tk.TclError, OSError):
            pass

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        self._build_apply_bar()
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=6)
        root = ttk.Frame(self.nb, padding=10)
        self.nb.add(root, text="  Run  ")
        from .review_ui import ReviewTab
        self.review = ReviewTab(self.nb, self)
        self.nb.add(self.review, text="  Review & player  ")
        self.bind_all("<Key>", self._on_key)
        root.columnconfigure(0, weight=1)

        lib = ttk.LabelFrame(root, text="Music folder", padding=8)
        lib.grid(row=0, column=0, sticky="ew", **pad)
        lib.columnconfigure(0, weight=1)
        self.v_folder = tk.StringVar()
        ttk.Entry(lib, textvariable=self.v_folder).grid(row=0, column=0, sticky="ew")
        ttk.Button(lib, text="Browse…", command=self._browse_music).grid(row=0, column=1, padx=(6, 0))

        col = ttk.LabelFrame(root, text="Personal LRC collection (used before LRCLIB)", padding=8)
        col.grid(row=1, column=0, sticky="ew", **pad)
        col.columnconfigure(0, weight=1)
        self.v_use_collection = tk.BooleanVar()
        ttk.Checkbutton(col, text="Use these .lrc folders/files", variable=self.v_use_collection
                        ).grid(row=0, column=0, sticky="w")
        self.lst_collection = tk.Listbox(col, height=4, selectmode="extended")
        self.lst_collection.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        btns = ttk.Frame(col)
        btns.grid(row=1, column=1, sticky="n", padx=(6, 0), pady=(4, 0))
        ttk.Button(btns, text="Add folder…", command=self._add_collection_folder).pack(fill="x")
        ttk.Button(btns, text="Add files…", command=self._add_collection_files).pack(fill="x", pady=2)
        ttk.Button(btns, text="Remove", command=self._remove_collection).pack(fill="x")

        opt = ttk.LabelFrame(root, text="Options", padding=8)
        opt.grid(row=2, column=0, sticky="ew", **pad)
        opt.columnconfigure(1, weight=1)
        self.v_online = tk.BooleanVar()
        self.v_acoustid = tk.BooleanVar()
        self.v_key = tk.StringVar()
        self.v_lrc_action = tk.StringVar()
        self.v_mtime = tk.BooleanVar()
        self.v_threshold = tk.IntVar()
        self.v_retry = tk.BooleanVar()
        r = 0
        ttk.Checkbutton(opt, text="Look up missing lyrics online (LRCLIB)", variable=self.v_online
                        ).grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
        ttk.Checkbutton(opt, text="Identify untagged files with AcoustID", variable=self.v_acoustid
                        ).grid(row=r, column=0, sticky="w")
        kf = ttk.Frame(opt)
        kf.grid(row=r, column=1, columnspan=2, sticky="ew"); r += 1
        ttk.Label(kf, text="API key:").pack(side="left", padx=(12, 4))
        self.ent_key = ttk.Entry(kf, textvariable=self.v_key, show="•", width=24)
        self.ent_key.pack(side="left")
        self.v_showkey = tk.BooleanVar()
        ttk.Checkbutton(kf, text="Show", variable=self.v_showkey,
                        command=lambda: self.ent_key.config(show="" if self.v_showkey.get() else "•")
                        ).pack(side="left", padx=4)
        ttk.Label(kf, text=("(blank = built-in key) " if DEFAULT_ACOUSTID_KEY else "")
                  + "application key from acoustid.org/new-application", foreground="gray").pack(side="left")
        ttk.Label(opt, text="After using a sidecar .lrc:").grid(row=r, column=0, sticky="w")
        self.cmb_action = ttk.Combobox(opt, state="readonly", width=24,
                                       values=[lbl for _, lbl in LRC_ACTIONS])
        self.cmb_action.grid(row=r, column=1, sticky="w"); r += 1
        ttk.Label(opt, text="Match confidence (70–100):").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(opt, from_=70, to=100, textvariable=self.v_threshold, width=6
                    ).grid(row=r, column=1, sticky="w"); r += 1
        ttk.Checkbutton(opt, text="Keep original file modified dates", variable=self.v_mtime
                        ).grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
        ttk.Checkbutton(opt, text="Retry tracks not found in earlier runs", variable=self.v_retry
                        ).grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
        ttk.Label(opt, text="When the app starts:").grid(row=r, column=0, sticky="w")
        self.cmb_session = ttk.Combobox(opt, state="readonly", width=34, values=[v for _, v in SESSION_MODES])
        self.cmb_session.grid(row=r, column=1, sticky="w"); r += 1

        act = ttk.Frame(root)
        act.grid(row=3, column=0, sticky="ew", **pad)
        self.btn_start = ttk.Button(act, text="Find lyrics", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(act, text="Stop", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(act, text="Open logs folder", command=self._open_logs).pack(side="right")
        self.btn_csv = ttk.Button(act, text="Open last CSV", command=self._open_csv, state="disabled")
        self.btn_csv.pack(side="right", padx=6)

        prog = ttk.Frame(root)
        prog.grid(row=4, column=0, sticky="ew", **pad)
        prog.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(prog, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        self.v_status = tk.StringVar(value="Ready.")
        ttk.Label(prog, textvariable=self.v_status).grid(row=1, column=0, sticky="w", pady=(4, 0))

        root.rowconfigure(5, weight=1)
        self.txt = ScrolledText(root, height=14, wrap="none", state="disabled",
                                font=("Consolas", 9) if os.name == "nt" else ("Monospace", 9))
        self.txt.grid(row=5, column=0, sticky="nsew", **pad)

    # ------------------------------------------------------------ settings
    def _load_into_widgets(self) -> None:
        s = self.settings
        self.v_folder.set(s.music_folder)
        self.v_use_collection.set(s.use_collection)
        self.lst_collection.delete(0, "end")
        for p in s.collection:
            self.lst_collection.insert("end", p)
        self.v_online.set(s.use_online)
        self.v_acoustid.set(s.use_acoustid)
        self.v_key.set(s.acoustid_key)
        self.cmb_action.set(dict(LRC_ACTIONS).get(s.lrc_action, LRC_ACTIONS[0][1]))
        self.v_mtime.set(s.restore_mtime)
        self.v_threshold.set(s.match_threshold)
        self.v_retry.set(s.retry_misses)
        self.v_output.set(s.output_mode if s.output_mode in ("embed", "lrc") else "embed")
        self.cmb_session.set(dict(SESSION_MODES).get(s.session_restore, SESSION_MODES[0][1]))

    def _collect_settings(self) -> Settings:
        s = self.settings
        s.music_folder = self.v_folder.get().strip()
        s.use_collection = self.v_use_collection.get()
        s.collection = list(self.lst_collection.get(0, "end"))
        s.use_online = self.v_online.get()
        s.use_acoustid = self.v_acoustid.get()
        s.acoustid_key = self.v_key.get().strip()
        label = self.cmb_action.get()
        s.lrc_action = next((k for k, v in LRC_ACTIONS if v == label), "recycle")
        s.restore_mtime = self.v_mtime.get()
        try:
            s.match_threshold = max(70, min(100, int(self.v_threshold.get())))
        except (tk.TclError, ValueError):
            s.match_threshold = 90
        s.retry_misses = self.v_retry.get()
        s.output_mode = self.v_output.get()
        s.session_restore = next((k for k, v in SESSION_MODES if v == self.cmb_session.get()), "ask")
        return s

    # ------------------------------------------------------------- actions
    def _browse_music(self) -> None:
        d = filedialog.askdirectory(title="Choose music folder", initialdir=self.v_folder.get() or None)
        if d:
            self.v_folder.set(os.path.normpath(d))

    def _add_collection_folder(self) -> None:
        d = filedialog.askdirectory(title="Add folder of .lrc files")
        if d and os.path.normpath(d) not in self.lst_collection.get(0, "end"):
            self.lst_collection.insert("end", os.path.normpath(d))
            self.v_use_collection.set(True)

    def _add_collection_files(self) -> None:
        files = filedialog.askopenfilenames(title="Add .lrc files", filetypes=[("LRC lyrics", "*.lrc")])
        existing = set(self.lst_collection.get(0, "end"))
        for f in files:
            f = os.path.normpath(f)
            if f not in existing:
                self.lst_collection.insert("end", f)
        if files:
            self.v_use_collection.set(True)

    def _remove_collection(self) -> None:
        for i in reversed(self.lst_collection.curselection()):
            self.lst_collection.delete(i)

    def _open_logs(self) -> None:
        d = self._collect_settings().resolved_log_dir()
        os.makedirs(d, exist_ok=True)
        open_path(d)

    def _open_csv(self) -> None:
        if self.last_csv and os.path.exists(self.last_csv):
            open_path(self.last_csv)

    def _start(self) -> None:
        s = self._collect_settings()
        if not s.music_folder or not os.path.isdir(s.music_folder):
            messagebox.showerror(APP_TITLE, "Choose an existing music folder first.")
            return
        save_settings(s)
        s = dataclasses.replace(s, dry_run=True)   # find only; the bottom bar writes
        self.cancel = threading.Event()
        self._set_running(True)
        self._clear_log()
        self.progress.config(value=0, maximum=1, mode="indeterminate")
        self.progress.start(15)
        self.v_status.set("Scanning…")
        self.worker = threading.Thread(target=self._run_engine, args=(s,), daemon=True)
        self.worker.start()

    def _run_engine(self, s: Settings) -> None:
        try:
            Engine(s, self.events.put, self.cancel).run()
        except Exception as e:
            self.events.put({"type": "fatal", "text": f"{type(e).__name__}: {e}"})

    def _stop(self) -> None:
        self.cancel.set()
        self.v_status.set("Stopping after the current file…")
        self.btn_stop.config(state="disabled")

    def _set_running(self, running: bool) -> None:
        self.btn_start.config(state="disabled" if running else "normal")
        self.btn_stop.config(state="normal" if running else "disabled")
        self.refresh_apply_bar()

    # -------------------------------------------------- final step (bottom bar)
    def _build_apply_bar(self) -> None:
        bar = ttk.Frame(self, padding=(12, 6, 12, 8))
        self.apply_bar = bar
        ttk.Separator(bar, orient="horizontal").pack(fill="x", pady=(0, 6))
        row = ttk.Frame(bar)
        row.pack(fill="x")
        ttk.Label(row, text="Final step:", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self.v_output = tk.StringVar(value="embed")
        ttk.Radiobutton(row, text="Overwrite files (embed lyrics)", value="embed", variable=self.v_output,
                        command=self._output_changed).pack(side="left", padx=(10, 4))
        ttk.Radiobutton(row, text="Save .lrc beside each file", value="lrc", variable=self.v_output,
                        command=self._output_changed).pack(side="left", padx=4)
        self.btn_apply_all = ttk.Button(row, text="Apply", command=self._apply_all)
        self.btn_apply_all.pack(side="right")
        self.btn_apply_stop = ttk.Button(row, text="Stop", command=self._apply_stop)
        self.apply_progress = ttk.Progressbar(row, mode="determinate", length=160)
        self.v_apply = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.v_apply).pack(side="right", padx=10)
        self.apply_worker: threading.Thread | None = None
        self.apply_cancel = threading.Event()
        self._apply_bar_shown = False

    def show_apply_bar(self) -> None:
        if not self._apply_bar_shown:
            self.apply_bar.pack(side="bottom", fill="x", before=self.nb)
            self._apply_bar_shown = True
        self.refresh_apply_bar()

    def _session_paths(self) -> list[str]:
        return list(self.review.rows.keys())

    def refresh_apply_bar(self) -> None:
        if not getattr(self, "_apply_bar_shown", False):
            return
        if self.apply_worker and self.apply_worker.is_alive():
            return
        try:
            from .applier import plan

            n = len(plan(self.settings, self._session_paths()))
        except Exception:
            n = 0
        running = bool(self.worker and self.worker.is_alive())
        target = "embed into" if self.v_output.get() == "embed" else "save .lrc for"
        self.v_apply.set(f"{n} file(s) ready" + (" — still finding…" if running else ""))
        self.btn_apply_all.config(text=f"Apply: {target} {n} file(s)",
                                  state="normal" if n and not running else "disabled")

    def _output_changed(self) -> None:
        self.settings.output_mode = self.v_output.get()
        try:
            save_settings(self.settings)
        except OSError:
            pass
        self.refresh_apply_bar()
        if self.review.song is not None:
            self.review.load_song(self.review.song.path)

    def _apply_all(self) -> None:
        self.apply_paths(None)

    def apply_paths(self, paths: list[str] | None) -> None:
        """Write lyrics for these songs now (None = every song in the list)."""
        if self.apply_worker and self.apply_worker.is_alive():
            return
        s = self._collect_settings()
        paths = self._session_paths() if paths is None else list(paths)
        from .applier import plan

        n = len(plan(s, paths))
        if not n:
            messagebox.showinfo(APP_TITLE, "Nothing to write for the selected song(s): their choice is "
                                           "Don't change, or the file already has those lyrics.")
            return
        if s.output_mode == "embed":
            msg = (f"Write lyrics into {n} audio file(s)? Existing lyrics in those files are replaced "
                   "(originals are kept in the app's backup).")
        else:
            msg = f"Save a .lrc file beside {n} audio file(s)? Existing .lrc files with the same name are replaced."
        if s.lrc_action == "delete":
            msg += "\n\nUsed sidecar .lrc files will be permanently deleted."
        elif s.lrc_action == "recycle":
            msg += "\n\nUsed sidecar .lrc files will be moved to the Recycle Bin."
        if not messagebox.askyesno(APP_TITLE, msg):
            return
        save_settings(s)
        self.review.player.stop()
        self.apply_cancel = threading.Event()
        self.btn_apply_all.config(state="disabled")
        self.btn_start.config(state="disabled")
        self.apply_progress.config(value=0, maximum=max(1, n))
        self.apply_progress.pack(side="right", padx=6)
        self.btn_apply_stop.pack(side="right")

        def work():
            from .applier import apply_all
            try:
                apply_all(s, paths, self.events.put, self.apply_cancel)
            except Exception as e:
                self.events.put({"type": "apply_fatal", "text": f"{type(e).__name__}: {e}"})
        self.apply_worker = threading.Thread(target=work, daemon=True)
        self.apply_worker.start()

    def restore_paths(self, paths: list[str], confirm: bool = True) -> None:
        """Put back the lyrics / .lrc these songs had before this app wrote to them."""
        if self.apply_worker and self.apply_worker.is_alive():
            return
        paths = list(paths)
        if not paths:
            return
        if confirm and not messagebox.askyesno(
                APP_TITLE, f"Restore the original lyrics of {len(paths)} song(s)?\n\nFiles this app wrote to get "
                           "back the lyrics (or .lrc file) they had before. Songs it never wrote are skipped."):
            return
        s = self._collect_settings()
        self.review.player.stop()
        self.apply_cancel = threading.Event()
        self.btn_apply_all.config(state="disabled")
        self.btn_start.config(state="disabled")
        self.apply_progress.config(value=0, maximum=max(1, len(paths)))
        self.apply_progress.pack(side="right", padx=6)
        self.btn_apply_stop.pack(side="right")

        def work():
            from .applier import restore_originals
            try:
                restore_originals(s, paths, self.events.put, self.apply_cancel)
            except Exception as e:
                self.events.put({"type": "apply_fatal", "text": f"{type(e).__name__}: {e}"})
        self.apply_worker = threading.Thread(target=work, daemon=True)
        self.apply_worker.start()

    # ------------------------------------------------------------ session
    def session_changed(self) -> None:
        self._session_dirty = True

    def save_session_now(self) -> None:
        if not self.review.rows:
            return
        from .session import save_session
        d = self.review.session_data()
        try:
            save_session(d["rows"], d["selected"], d["filter"], d["search"], self.settings.music_folder)
            self._session_dirty = False
        except OSError:
            pass

    def _autosave_session(self) -> None:
        if self._session_dirty:
            self.save_session_now()
        self.after(30000, self._autosave_session)

    def _offer_session(self) -> None:
        from .session import load_session
        data = load_session()
        if not data or self.review.rows:
            return
        mode = self.settings.session_restore
        if mode == "fresh":
            return
        if mode == "load":
            self.review.load_session_rows(data)
            self._log(f"Loaded your last session ({len(data['rows'])} songs).")
            return
        self._session_dialog(data)

    def _session_dialog(self, data: dict) -> None:
        import datetime as _dt
        n = len(data["rows"])
        when = _dt.datetime.fromtimestamp(data.get("saved", 0)).strftime("%Y-%m-%d %H:%M")
        dlg = tk.Toplevel(self)
        dlg.title(APP_TITLE)
        dlg.transient(self)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Load your last session?", font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        folder = data.get("music_folder") or "your music folder"
        ttk.Label(frm, text=f"{n} song{'s' if n != 1 else ''} from {folder}, saved {when}.",
                  wraplength=460).pack(anchor="w", pady=(4, 10))
        v_remember = tk.BooleanVar(value=False)
        v_restore = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Remember my choice (change it later on the Run tab)",
                        variable=v_remember).pack(anchor="w")
        ttk.Checkbutton(frm, text="Also restore files that were overwritten (put back the lyrics / .lrc files "
                                  "they had before this app wrote to them)", variable=v_restore).pack(anchor="w")
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(14, 0))
        self._session_dialog_win = dlg

        def finish(load: bool):
            remember, restore = v_remember.get(), v_restore.get()
            dlg.destroy()
            self._session_dialog_win = None
            if remember:
                self.settings.session_restore = "load" if load else "fresh"
                self.cmb_session.set(dict(SESSION_MODES)[self.settings.session_restore])
                try:
                    save_settings(self.settings)
                except OSError:
                    pass
            if load:
                self.review.load_session_rows(data)
                self.nb.select(self.review)
            if restore:
                self.restore_paths(list(data["rows"].keys()), confirm=False)
        ttk.Button(btns, text="Start fresh", command=lambda: finish(False)).pack(side="right")
        ttk.Button(btns, text="Load session", command=lambda: finish(True)).pack(side="right", padx=6)
        dlg.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        self._session_finish = finish
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + 120
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

    def _apply_stop(self) -> None:
        self.apply_cancel.set()

    def _apply_finished(self) -> None:
        self.apply_progress.pack_forget()
        self.btn_apply_stop.pack_forget()
        self.btn_start.config(state="normal")
        self.apply_worker = None
        self.refresh_apply_bar()

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_TITLE, "A run is in progress. Stop it and quit?"):
                return
            self.cancel.set()
            self.worker.join(timeout=10)
        self.review.shutdown()
        self.save_session_now()
        try:
            save_settings(self._collect_settings())
        except OSError:
            pass
        self.destroy()

    # ---------------------------------------------------------- event loop
    def _on_key(self, e):
        if str(self.nb.select()) != str(self.review):   # select() may return a Tcl object
            return None
        try:
            focus = self.focus_get()
        except (KeyError, tk.TclError):
            focus = None
        if self.review.key(e.keysym, focus):
            return "break"
        return None

    def log_line(self, text: str) -> None:
        self._log(text)

    def _log(self, text: str) -> None:
        self.txt.config(state="normal")
        self.txt.insert("end", text + "\n")
        lines = int(self.txt.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.txt.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.txt.see("end")
        self.txt.config(state="disabled")

    def _clear_log(self) -> None:
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.config(state="disabled")

    def _poll(self) -> None:
        try:
            for _ in range(500):
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _handle(self, e: dict) -> None:
        t = e["type"]
        if t == "log":
            self._log(e["text"])
        elif t == "scanning":
            self.v_status.set(f"Scanning… {e['found']} audio files so far")
        elif t == "scan":
            self.progress.stop()
            self.progress.config(mode="determinate", maximum=max(1, e["total"]), value=0)
        elif t.startswith("review_"):
            self.review.handle(e)
        elif t == "apply_start":
            self._log("")
            self._log(f"Applying {e['total']} file(s) — "
                      + ("embedding into the audio files" if e["mode"] == "embed" else "saving .lrc files"))
        elif t == "apply_progress":
            self.apply_progress.config(value=e["done"])
            self.v_apply.set(f"Applying {e['done']}/{e['total']}…")
            if state_key(e["path"]) in self.review.rows:
                self.review.add_row(e["path"], e["status"] if e["status"] != "unchanged" else "already_embedded",
                                    e["source"])
            label = STATUS_LABELS.get(e["status"], e["status"])
            self._log(f"{label}: {display_path(e['path'])}" + (f" — {e['detail']}" if e["detail"] else ""))
        elif t == "apply_done":
            c = e["counts"]
            written = c.get("embedded", 0) + c.get("lrc_written", 0)
            self._log(f"Applied: {written} written, {c.get('unchanged', 0)} unchanged, {c.get('error', 0)} errors"
                      + (" (stopped)" if e["cancelled"] else "") + f". Log: {e['csv']}")
            self._apply_finished()
            self.v_apply.set(f"Done: {written} written, {c.get('error', 0)} errors"
                             + (" (stopped)" if e["cancelled"] else ""))
            self.save_session_now()
            if self.review.song is not None:
                self.review.load_song(self.review.song.path)
        elif t == "restore_start":
            self._log("")
            self._log(f"Restoring original lyrics for {e['total']} song(s)…")
        elif t == "restore_progress":
            self.apply_progress.config(value=e["done"])
            self.v_apply.set(f"Restoring {e['done']}/{e['total']}…")
            if e["status"] in ("restored", "unchanged") and state_key(e["path"]) in self.review.rows:
                self.review.add_row(e["path"], "restored")
            if e["status"] in ("restored", "error"):
                self._log(f"{'Restored' if e['status'] == 'restored' else 'Error'}: {display_path(e['path'])}"
                          + (f" — {e['detail']}" if e["detail"] else ""))
        elif t == "restore_done":
            c = e["counts"]
            self._log(f"Restore finished: {c.get('restored', 0)} restored, {c.get('error', 0)} errors"
                      + (" (stopped)" if e["cancelled"] else "") + ".")
            self._apply_finished()
            self.v_apply.set(f"Restored {c.get('restored', 0)} song(s)")
            self.save_session_now()
            if self.review.song is not None:
                self.review.load_song(self.review.song.path)
        elif t == "apply_fatal":
            self._apply_finished()
            messagebox.showerror(APP_TITLE, e["text"])
        elif t == "result":
            r = e["result"]
            self.progress.config(value=e["done"])
            m = r.meta
            if r.status == "cached":    # finished in an earlier run: still list it for review
                self.review.add_row(r.path, r.detail, r.source, m.artist if m else "", m.title if m else "",
                                    r.local, r.online, r.choice, m.album if m else "")
                if not self._apply_bar_shown:
                    self.show_apply_bar()
            if r.status != "cached":
                self.review.add_row(r.path, r.status, r.source, m.artist if m else "", m.title if m else "",
                                    r.local, r.online, r.choice, m.album if m else "")
                if not self._apply_bar_shown:
                    self.show_apply_bar()
                label = STATUS_LABELS.get(r.status, r.status)
                src = f" [{r.source}]" if r.source and r.status == "embedded" else ""
                det = f" — {r.detail}" if r.detail else ""
                self._log(f"{label}{src}: {display_path(r.path)}{det}")
            c = e["counts"]
            done, total = e["done"], e["total"]
            eta = ""
            if done and e["elapsed"] > 5 and done < total:
                eta = f" · about {_fmt_duration(e['elapsed'] / done * (total - done))} left"
            self.v_status.set(
                f"{done}/{total} · found {c.get('proposed', 0)} · not found "
                f"{c.get('not_found', 0) + c.get('no_synced', 0)} · ambiguous {c.get('ambiguous', 0)}"
                f" · errors {c.get('error', 0)}{eta}")
        elif t == "done":
            self.progress.stop()
            self.last_csv = e["csv"]
            self.btn_csv.config(state="normal")
            self._set_running(False)
            c = e["counts"]
            self._log("")
            self._log("Summary:")
            for k, v in sorted(c.items(), key=lambda kv: -kv[1]):
                self._log(f"  {STATUS_LABELS.get(k, k)}: {v}")
            self._log(f"CSV log: {e['csv']}")
            self._log("Nothing has been written yet. Check the Review & player tab if you like, "
                      "then use the Apply button at the bottom.")
            word = "Stopped" if e["cancelled"] else "Finished"
            self.v_status.set(f"{word} in {_fmt_duration(e['elapsed'])} · lyrics found for "
                              f"{c.get('proposed', 0)} file(s)" + (" · run again to resume" if e["cancelled"] else ""))
            self.show_apply_bar()
            self.save_session_now()
            self.nb.select(self.review)          # straight on to reviewing what was found
            first = next((p for p in self.review.rows if self.review.tree.exists(p)), None)
            if first and not self.review.tree.selection():
                self.review.tree.selection_set(first)
                self.review.tree.see(first)
        elif t == "fatal":
            self.progress.stop()
            self._set_running(False)
            self._log("ERROR: " + e["text"])
            self.v_status.set("Failed.")
            messagebox.showerror(APP_TITLE, e["text"])


def main() -> None:
    if os.name == "nt":
        try:  # crisp text on high-DPI displays
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        try:  # own taskbar identity, so Windows shows this app's icon (not Python's)
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MassSyncedLyrics.App")
        except Exception:
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
