"""Desktop interface (Tkinter)."""
from __future__ import annotations

import dataclasses
import os
import time
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import APP_TITLE, DEFAULT_ACOUSTID_KEY, __version__, theme
from .config import Settings, load_settings, save_settings, snap5
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
        self.minsize(1100, 760)
        self.geometry("1440x920")
        self._set_icon()
        self.settings = settings or load_settings()
        self.option_add("*tearOff", False)
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
        theme.apply(self, self.settings.theme)
        self._build_header()
        self._build_apply_bar()
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=14, pady=(4, 8))
        root = ttk.Frame(self.nb, padding=(16, 14))
        self.nb.add(root, text="   Find lyrics   ")
        from .review_ui import ReviewTab
        self.review = ReviewTab(self.nb, self)
        self.nb.add(self.review, text="   Review & player   ")
        self.bind_all("<Key>", self._on_key)
        root.columnconfigure(0, weight=3, uniform="cols")
        root.columnconfigure(1, weight=2, uniform="cols")
        root.rowconfigure(1, weight=1)

        # ---- left column: library + collection
        lib = theme.card(root)
        lib.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))
        lib.columnconfigure(0, weight=1)
        ttk.Label(lib, text="Music library", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.v_folder = tk.StringVar()
        theme.hint(ttk.Entry(lib, textvariable=self.v_folder), 'The music folder to scan. Sub-folders are included.').grid(row=2, column=0, sticky="ew")
        theme.hint(ttk.Button(lib, text="Browse…", command=self._browse_music), 'Choose the music folder to scan.').grid(row=2, column=1, padx=(8, 0))

        ttk.Separator(lib).grid(row=3, column=0, columnspan=2, sticky="ew", pady=14)
        ttk.Label(lib, text="Your own LRC collection", style="Section.TLabel").grid(row=4, column=0, sticky="w",
                                                                                pady=(0, 6))
        self.v_use_collection = tk.BooleanVar()
        theme.hint(ttk.Checkbutton(lib, text="Use these folders / files", variable=self.v_use_collection,
                        style="Switch.TCheckbutton" if theme._state["sv"] else "TCheckbutton"
                        ), 'Look in your own .lrc files (listed below) before searching online. Collection files are never moved or deleted.').grid(row=6, column=0, sticky="w", pady=(0, 6))
        self.lst_collection = theme.register(theme.hint(tk.Listbox(lib, height=5, selectmode="extended"), "Folders and .lrc files from your own collection. They're matched by their [ar:]/[ti:] tags or an 'Artist - Title.lrc' name, plus song length."), "listbox")
        self.lst_collection.grid(row=7, column=0, sticky="nsew")
        btns = ttk.Frame(lib, style="Card.TFrame" if theme._state["sv"] else "TFrame")
        btns.grid(row=7, column=1, sticky="n", padx=(8, 0))
        theme.hint(ttk.Button(btns, text="Add folder…", command=self._add_collection_folder), 'Add a folder of .lrc files (searched recursively) to your collection.').pack(fill="x")
        theme.hint(ttk.Button(btns, text="Add files…", command=self._add_collection_files), 'Add individual .lrc files to your collection.').pack(fill="x", pady=6)
        theme.hint(ttk.Button(btns, text="Remove", command=self._remove_collection), 'Remove the selected folders/files from the collection list (nothing is deleted).').pack(fill="x")

        # ---- right column: options
        opt = theme.card(root)
        opt.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 8))
        opt.columnconfigure(1, weight=1)
        ttk.Label(opt, text="Options", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w",
                                                                    pady=(0, 8))
        self.v_online = tk.BooleanVar()
        self.v_acoustid = tk.BooleanVar()
        self.v_key = tk.StringVar()
        self.v_lrc_action = tk.StringVar()
        self.v_mtime = tk.BooleanVar()
        self.v_threshold = tk.IntVar()
        self.v_confident = tk.IntVar()
        self.v_fit = tk.IntVar()
        self.v_fit_audio = tk.BooleanVar()
        self.v_only_found = tk.BooleanVar()
        self.v_retry = tk.BooleanVar()
        sw = "Switch.TCheckbutton" if theme._state["sv"] else "TCheckbutton"
        r = 1
        for text, var, tip in (("Look up missing lyrics online (LRCLIB)", self.v_online,
                                 "Search LRCLIB, a free community lyrics database, for songs without local lyrics."),
                                ("Identify untagged files with AcoustID", self.v_acoustid,
                                 "Fingerprint files that have no title/artist tags to find out what they are.")):
            theme.hint(ttk.Checkbutton(opt, text=text, variable=var, style=sw), tip).grid(
                row=r, column=0, columnspan=2, sticky="w", pady=2); r += 1
        kf = ttk.Frame(opt, style="Card.TFrame" if theme._state["sv"] else "TFrame")
        kf.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(2, 8)); r += 1
        kf.columnconfigure(1, weight=1)
        ttk.Label(kf, text="AcoustID key").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.ent_key = theme.hint(ttk.Entry(kf, textvariable=self.v_key, show="•"),
                                  ("Blank = built-in key. " if DEFAULT_ACOUSTID_KEY else "")
                                  + "AcoustID needs an application key from acoustid.org/new-application "
                                  "(a personal user key is rejected). Only used for files with no title/artist tags.")
        self.ent_key.grid(row=0, column=1, sticky="ew")
        self.v_showkey = tk.BooleanVar()
        theme.hint(ttk.Checkbutton(kf, text="Show", variable=self.v_showkey,
                        command=lambda: self.ent_key.config(show="" if self.v_showkey.get() else "•")
                        ), 'Show or hide the key.').grid(row=0, column=2, padx=(8, 0))
        for label, widget in (("After using a sidecar .lrc", "action"), ("Match confidence (70–100)", "thr"),
                              ("High confidence (50–100)", "conf"), ("Good fit (0–100)", "fit"),
                              ("When the app starts", "session")):
            ttk.Label(opt, text=label).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 10))
            if widget == "action":
                self.cmb_action = theme.hint(ttk.Combobox(opt, state="readonly", values=[lbl for _, lbl in LRC_ACTIONS]), 'What happens to a sidecar .lrc after its lyrics are applied: Recycle Bin, delete, or keep. A <song>.lrc that is itself the output is never removed.')
                self.cmb_action.grid(row=r, column=1, sticky="ew")
            elif widget == "thr":
                sp = theme.hint(ttk.Spinbox(opt, from_=70, to=100, increment=5, textvariable=self.v_threshold,
                                            width=8), 'How closely names must match for a sidecar or collection .lrc to be used automatically. Lower finds more, higher is stricter. Steps of 5%.')
                sp.grid(row=r, column=1, sticky="w")
                sp.bind("<FocusOut>", lambda e: self.v_threshold.set(self._threshold_value()), add="+")
                sp.bind("<Return>", lambda e: self.v_threshold.set(self._threshold_value()), add="+")
            elif widget == "conf":
                sp = theme.hint(ttk.Spinbox(opt, from_=50, to=100, increment=5, textvariable=self.v_confident, width=8,
                                            command=self._confident_changed),
                                "Songs whose found lyrics match at this % or better count as high confidence: "
                                "Apply confident writes them without asking, and Hide low confidence hides the "
                                "rest. 100% = exact title, artist, album and length.")
                sp.grid(row=r, column=1, sticky="w")
                sp.bind("<FocusOut>", lambda e: self._confident_changed(), add="+")
                sp.bind("<Return>", lambda e: self._confident_changed(), add="+")
            elif widget == "fit":
                sp = theme.hint(ttk.Spinbox(opt, from_=0, to=100, increment=5, textvariable=self.v_fit, width=8,
                                            command=self._confident_changed),
                                "Found lyrics fit the recording well at this % or better (length, lines past the end, "
                                "spread over the song, lines on silence). Apply confident needs both High confidence "
                                "and Good fit; Hide low confidence hides songs below either.")
                sp.grid(row=r, column=1, sticky="w")
                sp.bind("<FocusOut>", lambda e: self._confident_changed(), add="+")
                sp.bind("<Return>", lambda e: self._confident_changed(), add="+")
            else:
                self.cmb_session = theme.hint(ttk.Combobox(opt, state="readonly", values=[v for _, v in SESSION_MODES]), 'What to do with the last review session when the app opens.')
                self.cmb_session.grid(row=r, column=1, sticky="ew")
            r += 1
        self.v_trim = tk.BooleanVar()
        self.v_quotes = tk.BooleanVar()
        for text, var, tip in (("Trim spaces from each lyric line", self.v_trim,
                                 "Remove spaces at the start and end of every lyric line (after its timestamp) "
                                 "in the lyrics that get written."),
                                ("Straighten smart quotes and punctuation", self.v_quotes,
                                 "Typographic characters become plain ones in the lyrics that get written: "
                                 "‘ ’ → '   “ ” → \"   … → ...   – — → -   and unusual or invisible spaces → "
                                 "normal spaces.")):
            theme.hint(ttk.Checkbutton(opt, text=text, variable=var, style=sw,
                                       command=self._tidy_changed), tip).grid(
                row=r, column=0, columnspan=2, sticky="w", pady=2); r += 1
        for text, var, tip in (("Keep original file modified dates", self.v_mtime,
                                 "After writing, put each file's modified date back so cloud sync and music "
                                 "libraries don't treat it as changed."),
                                ("Retry songs not found in earlier runs", self.v_retry,
                                 "Search online again for songs that had no lyrics last time."),
                                ("Check lyrics against the audio (slower)", self.v_fit_audio,
                                 "While finding, decode each song with found lyrics to see whether lines start on "
                                 "silence or run into the silence at the end; this sharpens the Fit score. Songs "
                                 "opened in the player are always checked."),
                                ("List only songs with lyrics found", self.v_only_found,
                                 "Review & player lists only the songs that have lyrics to write. Songs with "
                                 "nothing found (or already synced) are left out. Turn the list's Lyrics found "
                                 "box off to see them.")):
            theme.hint(ttk.Checkbutton(opt, text=text, variable=var, style=sw), tip).grid(
                row=r, column=0, columnspan=2, sticky="w", pady=2); r += 1

        # ---- bottom: actions + activity
        act = theme.card(root)
        act.grid(row=1, column=0, columnspan=2, sticky="nsew")
        act.columnconfigure(0, weight=1)
        act.rowconfigure(3, weight=1)
        top = ttk.Frame(act, style="Card.TFrame" if theme._state["sv"] else "TFrame")
        top.grid(row=0, column=0, sticky="ew")
        self.btn_start = theme.hint(ttk.Button(top, text="  Find lyrics  ", command=self._start,
                                    style="Accent.TButton" if theme._state["sv"] else "TButton"), 'Scan the music folder and find lyrics: sidecar .lrc files, your collection, then LRCLIB. Nothing is written until you press Apply at the bottom.')
        self.btn_start.pack(side="left")
        self.btn_stop = theme.hint(ttk.Button(top, text="Stop", command=self._stop, state="disabled"), 'Stop after the current song. Run Find lyrics again to continue where it stopped.')
        self.btn_stop.pack(side="left", padx=8)
        theme.hint(ttk.Button(top, text="Open logs folder", command=self._open_logs), 'Open the folder with the CSV logs of every run and apply.').pack(side="right")
        self.btn_csv = theme.hint(ttk.Button(top, text="Open last CSV", command=self._open_csv, state="disabled"), 'Open the CSV log of the last run (status, source and details for each song).')
        self.btn_csv.pack(side="right", padx=8)
        self.progress = ttk.Progressbar(act, mode="determinate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=(12, 4))
        self.v_status = tk.StringVar(value="Ready.")
        ttk.Label(act, textvariable=self.v_status, style="Muted.TLabel").grid(row=2, column=0, sticky="w",
                                                                            pady=(0, 8))
        logf = ttk.Frame(act, style="Card.TFrame" if theme._state["sv"] else "TFrame")
        logf.grid(row=3, column=0, sticky="nsew")
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.txt = tk.Text(logf, height=10, wrap="none", state="disabled",
                           font=("Cascadia Mono", 9) if os.name == "nt" else ("Monospace", 9))
        theme.register(self.txt, "text", mono=True)
        sb = ttk.Scrollbar(logf, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

    def _build_header(self) -> None:
        head = ttk.Frame(self, padding=(18, 12, 18, 4))
        head.pack(side="top", fill="x")
        self.header = head
        try:
            base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
            self._logo = tk.PhotoImage(file=os.path.join(base, "icon-256.png")).subsample(6, 6)
            ttk.Label(head, image=self._logo).pack(side="left", padx=(0, 12))
        except (tk.TclError, OSError):
            pass
        titles = ttk.Frame(head)
        titles.pack(side="left")
        ttk.Label(titles, text=APP_TITLE, style="App.Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text=f"Find, review and apply time-synced lyrics · v{__version__}",
                  style="Muted.TLabel").pack(anchor="w")
        self.v_dark = tk.BooleanVar(value=self.settings.theme == "dark")
        theme.hint(ttk.Checkbutton(head, text="Dark mode", variable=self.v_dark, command=self._toggle_theme,
                        style="Switch.TCheckbutton" if theme._state["sv"] else "TCheckbutton"), 'Switch between light and dark appearance.').pack(side="right")

    def _tidy_changed(self) -> None:
        self.settings.tidy_trim = bool(self.v_trim.get())
        self.settings.tidy_quotes = bool(self.v_quotes.get())
        try:
            save_settings(self.settings)
        except OSError:
            pass
        if self.review.song is not None:
            self.review._recompute()            # preview the cleaned-up lyrics right away

    def _toggle_theme(self) -> None:
        self.settings.theme = "dark" if self.v_dark.get() else "light"
        theme.apply(self, self.settings.theme)
        try:
            save_settings(self.settings)
        except OSError:
            pass

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
        self.v_threshold.set(snap5(s.match_threshold, 70, 100))
        self.v_confident.set(snap5(s.confident_threshold, 50, 100))
        self.v_fit.set(snap5(s.confident_fit, 0, 100))
        self.v_fit_audio.set(bool(s.fit_audio))
        self.v_only_found.set(bool(s.only_found))
        self.v_retry.set(s.retry_misses)
        self.v_output.set(s.output_mode if s.output_mode in ("embed", "lrc") else "embed")
        self.cmb_session.set(dict(SESSION_MODES).get(s.session_restore, SESSION_MODES[0][1]))
        self.v_trim.set(s.tidy_trim)
        self.v_quotes.set(s.tidy_quotes)

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
        s.match_threshold = self._threshold_value()
        self.v_threshold.set(s.match_threshold)
        s.retry_misses = self.v_retry.get()
        s.confident_threshold = self._confident_value()
        s.confident_fit = self._fit_value()
        s.fit_audio = bool(self.v_fit_audio.get())
        s.only_found = bool(self.v_only_found.get())
        s.output_mode = self.v_output.get()
        s.session_restore = next((k for k, v in SESSION_MODES if v == self.cmb_session.get()), "ask")
        s.tidy_trim = bool(self.v_trim.get())
        s.tidy_quotes = bool(self.v_quotes.get())
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
        self._switched_to_review = False
        if bool(self.review.v_only_found.get()) != s.only_found:
            self.review.v_only_found.set(s.only_found)
            self.review._refill()
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
        applying = bool(getattr(self, "apply_worker", None) and self.apply_worker.is_alive())
        self.btn_start.config(state="disabled" if running or applying else "normal")
        self.btn_stop.config(state="normal" if running else "disabled")
        self.refresh_apply_bar()

    # -------------------------------------------------- final step (bottom bar)
    def _build_apply_bar(self) -> None:
        outer = ttk.Frame(self, padding=(14, 0, 14, 12))
        self.apply_bar = outer
        bar = theme.card(outer, padding=(16, 10))
        bar.pack(fill="x")
        row = ttk.Frame(bar, style="Card.TFrame" if theme._state["sv"] else "TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Final step", style="Section.TLabel").pack(side="left", padx=(0, 14))
        self.v_output = tk.StringVar(value="embed")
        theme.hint(ttk.Radiobutton(row, text="Overwrite files (embed lyrics)", value="embed", variable=self.v_output,
                        command=self._output_changed), "Apply writes the lyrics into each audio file's tags. Originals are backed up and can be restored.").pack(side="left", padx=(0, 10))
        theme.hint(ttk.Radiobutton(row, text="Save .lrc beside each file", value="lrc", variable=self.v_output,
                        command=self._output_changed), "Apply writes '<song name>.lrc' next to each song and leaves the audio files untouched.").pack(side="left")
        self.btn_apply_all = theme.hint(ttk.Button(row, text="Apply", command=self._apply_all,
                                        style="Accent.TButton" if theme._state["sv"] else "TButton"), 'Write the chosen lyrics for every song in the list that has something to write. To write just some songs, select them and right-click → Save now.')
        self.btn_apply_all.pack(side="right")
        self.btn_apply_conf = theme.hint(ttk.Button(row, text="Apply confident", command=self._apply_confident),
                                         "Write, without asking, the found lyrics at or above both the High "
                                         "confidence and Good fit levels (Find lyrics tab). Songs with your own "
                                         "choices or edits are left for Apply.")
        self.btn_apply_conf.pack(side="right", padx=(0, 8))
        self.btn_apply_stop = theme.hint(ttk.Button(row, text="Stop", command=self._apply_stop), 'Stop applying after the current song.')
        self.apply_progress = ttk.Progressbar(row, mode="determinate", length=160)
        self.v_apply = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.v_apply, style="Muted.TLabel").pack(side="right", padx=12)
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

    def refresh_apply_bar(self, background: bool = False) -> None:
        """Update the Apply / Apply confident counts. With background=True (used while finding, for big
        lists) the database is read on a helper thread and the bar updates when it's done."""
        if not getattr(self, "_apply_bar_shown", False):
            return
        if self.apply_worker and self.apply_worker.is_alive():
            return
        self._apply_bar_refreshed = time.monotonic()
        paths = self._session_paths()
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
            prev = getattr(self, "_count_worker", None)
            if prev is not None and prev.is_alive():
                return
            self._count_worker = threading.Thread(
                target=lambda: self.events.put({"type": "apply_counts", "counts": count()}), daemon=True)
            self._count_worker.start()
            return
        self._set_apply_counts(*count())

    def _set_apply_counts(self, n: int, nc: int, cost: float = 0.0) -> None:
        # big lists make counting slower: during a run, refresh at most every 1.5 s or 10x its own cost
        self._apply_bar_cost = cost
        if self.apply_worker and self.apply_worker.is_alive():
            return
        running = bool(self.worker and self.worker.is_alive())
        target = "embed into" if self.v_output.get() == "embed" else "save .lrc for"
        self.v_apply.set(f"{n} file(s) ready" + (" — still finding…" if running else ""))
        self.btn_apply_all.config(text=f"Apply: {target} {n} file(s)", state="normal" if n else "disabled")
        self.btn_apply_conf.config(text=f"Apply confident ({nc})", state="normal" if nc else "disabled")

    def _first_reviewable(self, path: str) -> None:
        """The first song with lyrics to look at: open it in Review & player while the rest load."""
        self._switched_to_review = True
        if str(self.nb.select()) != str(self.review) and not self.review.tree.selection():
            self.nb.select(self.review)
            key = state_key(path)
            if self.review.tree.exists(key):
                self.review.tree.selection_set(key)
                self.review.tree.focus(key)
                self.review.tree.see(key)
            self._log("Lyrics found — opened Review & player. You can review, edit and save songs "
                      "while the rest are still loading.")

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

    def _confident_value(self) -> int:
        try:
            return snap5(self.v_confident.get(), 50, 100)
        except tk.TclError:
            return 95

    def _fit_value(self) -> int:
        try:
            return snap5(self.v_fit.get(), 0, 100)
        except tk.TclError:
            return 80

    def _threshold_value(self) -> int:
        try:
            return snap5(self.v_threshold.get(), 70, 100)
        except tk.TclError:
            return 90

    def set_score_levels(self, confident: int | None = None, fit: int | None = None) -> None:
        """Change the High confidence / Good fit levels (from the Find tab or the list's header filters)."""
        if confident is not None:
            self.v_confident.set(snap5(confident, 50, 100))
        if fit is not None:
            self.v_fit.set(snap5(fit, 0, 100))
        self._confident_changed()

    def _confident_changed(self) -> None:
        v, f = self._confident_value(), self._fit_value()
        for var, val in ((self.v_confident, v), (self.v_fit, f)):
            try:
                same = var.get() == val
            except tk.TclError:
                same = False
            if not same:
                var.set(val)
        if v == self.settings.confident_threshold and f == self.settings.confident_fit:
            return
        self.settings.confident_threshold = v
        self.settings.confident_fit = f
        try:
            save_settings(self.settings)
        except OSError:
            pass
        self.review._refill()
        self.refresh_apply_bar()

    def _apply_confident(self) -> None:
        """Write the high-confidence proposals straight away (no confirmation)."""
        if self.apply_worker and self.apply_worker.is_alive():
            return
        from .applier import plan_confident

        s = self._collect_settings()
        paths = plan_confident(s, self._session_paths())
        if paths:
            self.apply_paths(paths, confirm=False)

    def apply_paths(self, paths: list[str] | None, confirm: bool = True) -> None:
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
        if confirm and not messagebox.askyesno(APP_TITLE, msg):
            return
        save_settings(s)
        self.review.player.stop()
        self.apply_cancel = threading.Event()
        self.btn_apply_all.config(state="disabled")
        self.btn_apply_conf.config(state="disabled")
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
        self.btn_apply_conf.config(state="disabled")
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

    def save_session_now(self, background: bool = False) -> None:
        if not self.review.rows:
            return
        from .session import save_session
        d = self.review.session_data()
        rows = {p: dict(r) for p, r in d["rows"].items()}       # a snapshot, so writing can run alongside
        extra = {k: v for k, v in d.items() if k not in ("rows", "selected", "filter", "search")}
        args = (rows, d["selected"], d["filter"], d["search"], self.settings.music_folder)
        self._session_dirty = False

        def write():
            try:
                save_session(*args, extra=extra)
            except OSError:
                self._session_dirty = True
        if background:
            prev = getattr(self, "_session_writer", None)
            if prev is not None and prev.is_alive():
                self._session_dirty = True                        # still writing the last one: next time
                return
            self._session_writer = threading.Thread(target=write, daemon=True)
            self._session_writer.start()
        else:
            prev = getattr(self, "_session_writer", None)
            if prev is not None and prev.is_alive():
                prev.join(10)
            write()

    def _autosave_session(self) -> None:
        if self._session_dirty:
            self.save_session_now(background=True)
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
            n = self.review.load_session_rows(data, self.settings.session_include_saved)
            self._log(f"Loaded your last session ({n} songs).")
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
        ttk.Label(frm, text="Load your last session?", style="Section.TLabel").pack(anchor="w")
        folder = data.get("music_folder") or "your music folder"
        ttk.Label(frm, text=f"{n} song{'s' if n != 1 else ''} from {folder}, saved {when}.",
                  wraplength=460).pack(anchor="w", pady=(4, 10))
        v_remember = tk.BooleanVar(value=False)
        v_saved = tk.BooleanVar(value=False)
        theme.hint(ttk.Checkbutton(frm, text="Remember my choice", variable=v_remember),
                   "Don't ask next time. Change it later under 'When the app starts' on the Find lyrics tab.").pack(
            anchor="w")
        theme.hint(ttk.Checkbutton(frm, text="Also include songs that were already saved", variable=v_saved),
                   "Bring back songs whose lyrics were already written, shown as Saved. Their files are not "
                   "changed.").pack(anchor="w")
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(14, 0))
        self._session_dialog_win = dlg

        def finish(load: bool):
            remember, include_saved = v_remember.get(), v_saved.get()
            dlg.destroy()
            self._session_dialog_win = None
            if remember:
                self.settings.session_restore = "load" if load else "fresh"
                self.settings.session_include_saved = include_saved
                self.cmb_session.set(dict(SESSION_MODES)[self.settings.session_restore])
                try:
                    save_settings(self.settings)
                except OSError:
                    pass
            if load:
                n = self.review.load_session_rows(data, include_saved)
                self._log(f"Loaded your last session ({n} songs).")
                self.nb.select(self.review)
        theme.hint(ttk.Button(btns, text="Start fresh", command=lambda: finish(False)), 'Start with an empty song list.').pack(side="right")
        theme.hint(ttk.Button(btns, text="Load session", command=lambda: finish(True),
                   style="Accent.TButton" if theme._state["sv"] else "TButton"), 'Reload the song list, choices and selection from last time.').pack(side="right", padx=6)
        dlg.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        self._session_finish = finish
        self._session_saved_var = v_saved
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
        finding = bool(self.worker and self.worker.is_alive())
        self.btn_start.config(state="disabled" if finding else "normal")
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
        """Queue a line for the activity log; lines are written in one go per batch of events."""
        buf = getattr(self, "_log_buf", None)
        if buf is None:
            buf = self._log_buf = []
        buf.append(text)
        if len(buf) == 1:
            self.after_idle(self._flush_log)

    def _flush_log(self) -> None:
        buf = getattr(self, "_log_buf", None)
        if not buf:
            return
        lines_in = buf[-MAX_LOG_LINES:]
        self._log_buf = []
        self.txt.config(state="normal")
        self.txt.insert("end", "\n".join(lines_in) + "\n")
        lines = int(self.txt.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.txt.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.txt.see("end")
        self.txt.config(state="disabled")

    def _clear_log(self) -> None:
        self._log_buf = []
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
        elif t == "apply_counts":
            self._set_apply_counts(*e["counts"])
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
            # cached = finished in an earlier run: still listed for review (its detail holds the status)
            self.review.add_row(r.path, r.detail if r.status == "cached" else r.status, r.source,
                                m.artist if m else "", m.title if m else "", r.local, r.online, r.choice,
                                m.album if m else "", r.conf_local, r.conf_online, r.fit_local, r.fit_online)
            if not self._apply_bar_shown:
                self.show_apply_bar()
            if r.status == "proposed" and not getattr(self, "_switched_to_review", True):
                self._first_reviewable(r.path)
            if time.monotonic() - getattr(self, "_apply_bar_refreshed", 0.0) > max(
                    1.5, 10 * getattr(self, "_apply_bar_cost", 0.0)):
                self.refresh_apply_bar(background=True)
            if r.status != "cached":
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
