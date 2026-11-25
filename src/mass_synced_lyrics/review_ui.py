"""Review tab: results list, in-app player with waveforms, checkpoints, lyric
comparison, timing adjustment and per-file choices."""
from __future__ import annotations

import os
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import APP_TITLE, theme
from . import lrc as lrcmod
from . import review
from .config import save_settings
from .engine import STATUS_LABELS, display_path
from .paths import state_key
from .player import Player, decode, envelope, volume_gain
from .state import KEEP, State

FILTERS = {
    "All": None,
    "Needs attention": {"ambiguous", "not_found", "no_synced", "no_metadata", "error", "rejected", "unsupported"},
    "Found (to apply)": {"proposed", "chosen", "locked"},
    "Saved": {"embedded", "lrc_written", "already_embedded", "unchanged"},
    "Changed": {"embedded", "lrc_written", "locked", "chosen"},
    "Per-file choices": {"locked", "chosen"},
    "Already had lyrics": {"has_synced", "already_embedded"},
}

BG = "#1d1f24"
WAVE = "#5b8def"
WAVE_DIM = "#34507f"
PLAYHEAD = "#ff4d4f"
CUR_A, CUR_B, CUR_ON = "#a7782a", "#c9973f", "#ffd27a"
NEW_A, NEW_B, NEW_ON = "#2f8a5f", "#46b07f", "#8ff0c0"
CP_COLOR = "#f5c542"
WINDOW = 8.0           # seconds shown in the scrolling waveform
ANIM = 0.2             # seconds for eased scrolling / scrubbing
TICK_MS = 20

SHORT_STATUS = {
    "proposed": "Found", "embedded": "Saved", "lrc_written": ".lrc saved", "already_embedded": "Up to date",
    "has_synced": "Has lyrics", "not_found": "Not found", "no_synced": "Plain only online",
    "instrumental": "Instrumental", "no_metadata": "No tags", "ambiguous": "Ambiguous", "error": "Error",
    "unsupported": "Unsupported", "locked": "Your choice", "chosen": "Your choice", "unchanged": "Unchanged",
    "rejected": "Rejected", "restored": "Restored", "": "",
}
CHOICE_TEXT = {"local": "Local", "online": "Online", "keep": "Don't change", "custom": "Custom",
               "remove": "Remove lyrics", "": ""}
DONE_STATUSES = {"embedded", "lrc_written", "already_embedded", "unchanged"}
SAVED_STATUSES = {"embedded", "lrc_written"}          # this app wrote the lyrics
PROBLEM_STATUSES = {"error", "ambiguous", "unsupported", "rejected"}


LEGEND_HINTS = {
    "c_local": "Apply will use local lyrics: a sidecar .lrc, your collection, or embedded lyrics (.lrc mode).",
    "c_online": "Apply will use lyrics found online (LRCLIB).",
    "c_custom": "Apply will use lyrics you chose or re-timed in the player.",
    "c_keep": "Nothing will be written for this song.",
    "c_done": "Saved: the lyrics are written (or the song already had them).",
    "c_problem": "Needs a look: error, ambiguous .lrc match, or unsupported file.",
}
COLUMN_HINTS = {
    "#0": "Artist ▸ Album ▸ song. Ctrl/Shift+click selects several songs; an artist or album row counts as all "
          "its songs. Right-click for Save now, sources, Reset and Restore original.",
    "#1": "What happened to the song so far.",
    "#2": "Lyrics found locally: a sidecar .lrc beside the song, your collection, or embedded lyrics.",
    "#3": "Result of the LRCLIB lookup: found, plain lyrics only, instrumental, or not found.",
    "#4": "What Apply will do. Click a Choice cell to cycle Local → Online → Don't change. On an artist/album "
          "row or a multi-selection it changes all of them.",
}


def _hex_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def ease_out(p: float) -> float:
    p = max(0.0, min(1.0, p))
    return 1 - (1 - p) ** 3
TEXT_INPUTS = {"Entry", "TEntry", "Spinbox", "TSpinbox", "TCombobox", "Text"}


def fmt_time(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60)}:{t % 60:04.1f}"


class Tooltip:
    def __init__(self, widget: tk.Widget):
        self.widget = widget
        self.tip: tk.Toplevel | None = None
        self.label: tk.Label | None = None

    def show(self, text: str, x_root: int, y_root: int) -> None:
        if not text:
            self.hide()
            return
        if self.tip is None:
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            try:
                self.tip.wm_attributes("-topmost", True)
            except tk.TclError:
                pass
            p = theme.palette()
            self.label = tk.Label(self.tip, justify="left", background=p["surface"], foreground=p["fg"],
                                  relief="solid", borderwidth=1, padx=10, pady=6, wraplength=520,
                                  font=theme.font("body"), highlightthickness=0)
            self.label.pack()
        self.label.config(text=text)
        self.tip.update_idletasks()
        h = self.tip.winfo_reqheight()
        self.tip.wm_geometry(f"+{x_root + 14}+{max(0, y_root - h - 14)}")

    def hide(self) -> None:
        if self.tip is not None:
            self.tip.destroy()
            self.tip = self.label = None


class ReviewTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=6, takefocus=0)
        self.app = app
        self.player = Player()
        self.rows: dict[str, dict] = {}
        self.row_status_before_choice: dict[str, str] = {}
        self._group_target: dict[str, str] = {}
        self.song: review.SongInfo | None = None
        self.audio = None
        self.cands: list[review.Candidate] = []
        self.cand: review.Candidate | None = None
        self.offset, self.factor = 0.0, 1.0
        self.anchors: list[tuple[float, float]] = []
        self.pins: dict[str, float] = {}          # original line time (pin_key) -> fixed time
        self.edited: set[str] = set()             # pins whose time was typed / set by the user
        self.interpolate = False                  # unpinned lines follow a warp through the pins
        self._editor = None
        self.orig_lines: list = []
        self.new_text: str | None = None
        self.cur_lines: list = []
        self.new_lines: list = []
        self.cur_segs: list = []
        self.new_segs: list = []
        self.checkpoints: list[float] = []
        self.cp_custom = False
        self.cp_range: tuple[float, float] | None = None
        self.cp_cycle = -1
        self.sel_line: int | None = None
        self._now = {"cur": -1, "new": -1}
        self._ov_active = {"cur": -1, "new": -1}
        self._ov_lane_items: dict[str, list[int]] = {"cur": [], "new": []}
        self._cp_buttons: list[tk.Widget] = []
        self._collection = None
        self._collection_key = None
        self._token = 0
        self._recompute_job = None
        self._suspend_trace = False
        # animation state
        self._disp_pos = 0.0
        self._last_actual: float | None = None
        self._last_t = 0.0
        self._jump: tuple[float, float] | None = None       # (offset at start, start time)
        self._scroll_anim: dict[str, tuple[float, float, float] | None] = {"cur": None, "new": None}
        self._prog_scroll = {"cur": False, "new": False}
        self._ov_drag: dict | None = None
        self._sc_drag: dict | None = None
        self._build()
        self.after(TICK_MS, self._tick)

    # ================================================================ layout
    def _build(self) -> None:
        paned = theme.paned(self)
        paned.pack(fill="both", expand=True)
        self._paned = paned
        self.bind("<Map>", self._first_map, add="+")

        # ---- left: results list
        left = ttk.Frame(paned, padding=(0, 0, 4, 0))
        paned.add(left, minsize=300, stretch="always")
        top = ttk.Frame(left)
        top.pack(fill="x")
        self.v_filter = tk.StringVar(value="All")
        cmb = theme.hint(ttk.Combobox(top, textvariable=self.v_filter, values=list(FILTERS), state="readonly", width=16), 'Show only some songs: needing a look, found, changed, your choices…')
        cmb.pack(side="left")
        cmb.bind("<<ComboboxSelected>>", lambda e: self._refill())
        self.v_search = tk.StringVar()
        ent = theme.hint(ttk.Entry(top, textvariable=self.v_search), 'Search by artist, album, title or path.')
        ent.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.v_search.trace_add("write", lambda *a: self._refill())
        legend = ttk.Frame(left)
        legend.pack(fill="x", pady=(4, 0))
        for tag, text in (("c_local", "Local"), ("c_online", "Online"), ("c_custom", "Custom"),
                          ("c_keep", "Don't change"), ("c_done", "Saved"), ("c_problem", "Check")):
            pill = theme.register(tk.Label(legend, text=text), "pill", tag=tag)
            theme.hint(pill, LEGEND_HINTS[tag]).pack(side="left", padx=(0, 4))
        tf = ttk.Frame(left)
        tf.pack(fill="both", expand=True, pady=4)
        cols = ("status", "local", "online", "choice")
        self.tree = ttk.Treeview(tf, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Artist ▸ Album ▸ Song")
        self.tree.column("#0", width=190, minwidth=110, stretch=True)
        for col, w, label in (("status", 76, "Status"), ("local", 64, "Local"),
                              ("online", 70, "Online"), ("choice", 108, "Choice ⟳")):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=w, minwidth=40, stretch=False)
        theme.register(self, "callback", fn=self._restyle)
        self.tree.bind("<Button-1>", self._tree_click, add="+")
        self.tree.bind("<Button-3>", self._tree_menu)
        self._head_tip = Tooltip(self.tree)
        self.tree.bind("<Motion>", self._tree_motion, add="+")
        self.tree.bind("<Leave>", lambda e: self._head_tip.hide(), add="+")
        if sys.platform == "darwin":
            self.tree.bind("<Button-2>", self._tree_menu)
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        bf = ttk.Frame(left)
        bf.pack(fill="x")
        theme.hint(ttk.Button(bf, text="Load earlier results", command=self.load_earlier), 'Add every song from earlier runs to the list.').pack(side="left")
        theme.hint(ttk.Button(bf, text="Open file…", command=self._open_file), 'Open any song in the player.').pack(side="left", padx=6)
        self.v_count = tk.StringVar(value="0 files")
        ttk.Label(bf, textvariable=self.v_count).pack(side="right")

        # ---- right: player
        right = ttk.Frame(paned, padding=(6, 0, 0, 0))
        paned.add(right, minsize=520, stretch="always")
        right.columnconfigure(0, weight=1)
        r = 0
        head = ttk.Frame(right)
        head.grid(row=r, column=0, sticky="ew"); r += 1
        self.v_title = tk.StringVar(value="Select a song on the left (or Open file…).")
        ttk.Label(head, textvariable=self.v_title, style="Section.TLabel").pack(side="left")
        self.v_override = tk.StringVar()
        ttk.Label(head, textvariable=self.v_override, style="Muted.TLabel").pack(side="right")

        tr = ttk.Frame(right)
        tr.grid(row=r, column=0, sticky="ew", pady=(4, 2)); r += 1
        self.btn_play = theme.hint(ttk.Button(tr, text="▶ Play", width=9, command=self._toggle_play,
                                   style="Accent.TButton" if theme._state["sv"] else "TButton"), 'Play / pause (Space).\nSpace: play / pause · keys 1–9: play checkpoint N · 0: next checkpoint\nClick a waveform to jump, drag it to scrub · Ctrl+click the overview to add or remove a checkpoint')
        self.btn_play.pack(side="left")
        theme.hint(ttk.Button(tr, text="−5s", width=4, command=lambda: self._nudge(-5)), 'Back 5 seconds.').pack(side="left", padx=(4, 0))
        theme.hint(ttk.Button(tr, text="+5s", width=4, command=lambda: self._nudge(5)), 'Forward 5 seconds.').pack(side="left", padx=(2, 8))
        self.v_time = tk.StringVar(value="0:00.0 / 0:00.0")
        ttk.Label(tr, textvariable=self.v_time, width=17).pack(side="left")
        ttk.Label(tr, text="🔊" if os.name == "nt" else "Vol").pack(side="left", padx=(6, 2))
        self.v_volume = tk.DoubleVar(value=float(self.app.settings.playback_volume))
        self.sld_volume = theme.hint(ttk.Scale(tr, from_=0, to=100, orient="horizontal", length=110,
                                    variable=self.v_volume, command=self._volume_changed), 'Playback volume (logarithmic). Mouse wheel: ±2 %.')
        self.sld_volume.pack(side="left")
        self.v_volume_txt = tk.StringVar(value=f"{int(self.v_volume.get())}%")
        self.lbl_volume = ttk.Label(tr, textvariable=self.v_volume_txt, width=5, cursor="xterm")
        self.lbl_volume.pack(side="left", padx=(4, 0))
        self.lbl_volume.bind("<Button-1>", lambda e: self._edit_volume())
        self.sld_volume.bind("<ButtonRelease-1>", lambda e: self._save_globals())
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.sld_volume.bind(seq, self._volume_wheel)
        self.player.volume = volume_gain(self.v_volume.get())
        self.v_audio_note = tk.StringVar()
        ttk.Label(right, textvariable=self.v_audio_note, style="Caption.TLabel").grid(row=r, column=0, sticky="w"); r += 1

        self.ov = tk.Canvas(right, height=78, background=BG, highlightthickness=0, cursor="hand2")
        self.ov.grid(row=r, column=0, sticky="ew"); r += 1
        self.cpbar = theme.register(tk.Canvas(right, height=28, highlightthickness=0), "bg")
        self.cpbar.grid(row=r, column=0, sticky="ew"); r += 1
        cpr = ttk.Frame(right)
        cpr.grid(row=r, column=0, sticky="ew", pady=(0, 3)); r += 1
        ttk.Label(cpr, text="Checkpoints: play ±").pack(side="left")
        self.v_cp_len = tk.DoubleVar(value=self.app.settings.checkpoint_seconds)
        theme.hint(ttk.Spinbox(cpr, from_=0.2, to=15, increment=0.1, width=5, textvariable=self.v_cp_len,
                    command=self._save_globals), 'How many seconds before and after a checkpoint to play.').pack(side="left", padx=(3, 0))
        ttk.Label(cpr, text="s").pack(side="left")
        self.v_loop = tk.BooleanVar(value=self.app.settings.loop_checkpoints)
        theme.hint(ttk.Checkbutton(cpr, text="Loop", variable=self.v_loop,
                        command=self._save_globals), 'Repeat a checkpoint until you stop it.').pack(side="left", padx=8)
        theme.hint(ttk.Button(cpr, text="Reset points", command=self._default_checkpoints), 'Back to the default checkpoints: the first and last lyric line.').pack(side="left")
        self.sc = tk.Canvas(right, height=128, background=BG, highlightthickness=0, cursor="sb_h_double_arrow")
        self.sc.grid(row=r, column=0, sticky="ew"); r += 1

        tcard = theme.card(right, padding=(12, 8))
        tcard.grid(row=r, column=0, sticky="ew", pady=(8, 4)); r += 1
        ttk.Label(tcard, text="Timing of the new lyrics", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        cs = "Card.TFrame" if theme._state["sv"] else "TFrame"
        tm = ttk.Frame(tcard, style=cs)
        tm.pack(fill="x")
        ttk.Label(tm, text="Offset (ms)").pack(side="left")
        self.v_offset = tk.DoubleVar(value=0.0)
        theme.hint(ttk.Spinbox(tm, from_=-600000, to=600000, increment=50, width=7, textvariable=self.v_offset
                    ), 'Shift all new lyric times (milliseconds; + is later). Pinned and edited lines stay put.').pack(side="left", padx=(2, 10))
        ttk.Label(tm, text="Stretch (%)").pack(side="left")
        self.v_stretch = tk.DoubleVar(value=100.0)
        theme.hint(ttk.Spinbox(tm, from_=50, to=200, increment=0.05, width=7, textvariable=self.v_stretch
                    ), 'Stretch the new lyric timing (100 % = unchanged).').pack(side="left", padx=(2, 10))
        theme.hint(ttk.Button(tm, text="Stretch to song length", command=self._stretch_to_length), "Scale the timing by this song's length ÷ the length of the version the lyrics were made for (from LRCLIB or the [length:] tag; asks if unknown).").pack(side="left")
        tm2 = ttk.Frame(tcard, style=cs)
        tm2.pack(fill="x", pady=(8, 0))
        theme.hint(ttk.Button(tm2, text="Align selected line to playhead", command=self._align), "Click a line in New, play to where it's actually sung, then press: one aligned line sets the offset, two set offset and stretch.").pack(side="left", padx=(0, 8))
        ttk.Label(tm2, text="Pins & edits:").pack(side="left", padx=(0, 4))
        self.btn_interp = theme.flat_button(tm2, "Interpolate from pins", self._toggle_interpolate, padx=10)
        theme.hint(self.btn_interp, "Re-time every line you haven't pinned or edited by stretching the original "
                                    "timing between your pins. Updates live as you add pins; click again to turn off.")
        self.btn_interp.pack(side="left")
        theme.hint(ttk.Button(tm2, text="Clear pins", command=self.clear_pins), 'Remove all pins (edited times stay).').pack(side="left", padx=(6, 0))
        theme.hint(ttk.Button(tm2, text="Clear edited times", command=self.clear_edits), 'Reset every time you edited back to the computed timing.').pack(side="left", padx=(4, 0))
        self.v_pins = tk.StringVar()
        ttk.Label(tm2, textvariable=self.v_pins, style="Muted.TLabel").pack(side="left", padx=8)
        self.spn_offset = None
        self.v_anchors = tk.StringVar(value="")
        ttk.Label(tm, textvariable=self.v_anchors, style="Muted.TLabel").pack(side="left", padx=6)
        theme.hint(ttk.Button(tm, text="Reset timing", command=self._reset_timing), 'Clear offset, stretch, aligned lines, pins and edits.').pack(side="right")
        self.v_offset.trace_add("write", lambda *a: self._timing_changed())
        self.v_stretch.trace_add("write", lambda *a: self._timing_changed())

        right.rowconfigure(r, weight=1)
        lp = theme.paned(right)
        lp.grid(row=r, column=0, sticky="nsew"); r += 1
        lf = theme.card(lp, padding=(10, 8))
        self.lbl_cur = ttk.Label(lf, text="Current (embedded)", style="Section.TLabel")
        self.lbl_cur.pack(anchor="w", pady=(0, 6))
        nf = theme.card(lp, padding=(10, 8))
        ttk.Label(nf, text="New", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        lp.add(lf, minsize=180, stretch="always")
        lp.add(nf, minsize=180, stretch="always")
        self._lyrics_paned = lp
        lp.bind("<Map>", lambda e: self.after(30, self._even_lyrics_split), add="+")
        self.cmb_cand = theme.hint(ttk.Combobox(nf, state="readonly"), 'Lyrics to compare and choose from: what Find found, sidecars, collection, LRCLIB, the current lyrics (to re-time), the originals, or none.')
        self.cmb_cand.pack(fill="x", pady=(0, 4))
        self.cmb_cand.bind("<<ComboboxSelected>>", lambda e: (self._select_candidate(self.cmb_cand.current()),
                                                              self._grab_keys()))
        self.btn_keep = theme.flat_button(lf, "Keep current", self._lock, pady=8)
        theme.hint(self.btn_keep, "Leave this song's lyrics as they are (Apply skips it, later runs too).")
        self.btn_keep.pack(side="bottom", fill="x", pady=(8, 0))
        self.btn_apply = theme.flat_button(nf, "Use new", self._apply, pady=8)
        theme.hint(self.btn_apply, "Use the lyrics shown on the right, with any timing changes. Written when you "
                                   "press Apply (or right-click the song → Save now).")
        self.btn_apply.pack(side="bottom", fill="x", pady=(8, 0))
        self.txt_cur = self._lyrics_text(lf, "cur")
        self.txt_new = self._lyrics_text(nf, "new")

        act = ttk.Frame(right)
        act.grid(row=r, column=0, sticky="ew", pady=(6, 0)); r += 1
        theme.hint(ttk.Button(act, text="Reset to default (clear my choice)", command=self._clear), 'Forget your choice for this song; the default (what Find picked) applies again.').pack(side="left")
        self.v_choice_note = tk.StringVar()
        ttk.Label(act, textvariable=self.v_choice_note, style="Muted.TLabel").pack(side="left", padx=8)
        theme.hint(ttk.Button(act, text="Search LRCLIB", command=self._search_lrclib), "Search LRCLIB for more versions of this song's lyrics.").pack(side="right")

        self.tooltip = Tooltip(self)
        for c in (self.ov, self.sc):
            c.bind("<Motion>", self._hover)
            c.bind("<Leave>", lambda e: self.tooltip.hide())
        self.ov.bind("<Configure>", lambda e: self._draw_overview())
        self.cpbar.bind("<Configure>", lambda e: self._draw_cp_buttons())
        self.ov.bind("<ButtonPress-1>", self._ov_press)
        self.ov.bind("<B1-Motion>", self._ov_motion)
        self.ov.bind("<ButtonRelease-1>", self._ov_release)
        self.ov.bind("<Control-Button-1>", self._ov_ctrl_click)
        self.ov.bind("<Button-3>", self._ov_right_click)
        self.sc.bind("<ButtonPress-1>", self._sc_press)
        self.sc.bind("<B1-Motion>", self._sc_motion)
        self.sc.bind("<ButtonRelease-1>", self._sc_release)
        self._scene = None

    def _first_map(self, _e=None) -> None:
        if getattr(self, "_sash_done", False):
            return
        self._sash_done = True
        x = int(getattr(self.app.settings, "review_split", 0) or 540)
        self.after(50, lambda: self._paned.sash_place(0, x, 1))
        self._paned.bind("<ButtonRelease-1>", self._save_split, add="+")

    def _tag_lyrics(self, t: tk.Text) -> None:
        p = theme.palette()
        t.tag_configure("ts", foreground=p["muted"])
        t.tag_configure("now", background=p["now"])
        t.tag_configure("pick", background=p["pick"])
        t.tag_configure("unpinned", foreground=p["border"] if theme.mode() == "dark" else "#b5b5b5")
        t.tag_configure("pinned", foreground=p["pin"])
        t.tag_configure("ts_pinned", background=p["ts_pinned"], foreground=p["ts_pinned_fg"])
        t.tag_configure("edited", background=p["edited"], foreground=p["edited_fg"], font=theme.font("strong"))
        t.tag_raise("now")
        t.tag_raise("edited")

    def _restyle(self) -> None:
        """Re-apply palette colours (light/dark switch)."""
        p = theme.palette()
        for tag, (bg, fg) in p["rows"].items():
            if tag in ("group", "subgroup"):
                self.tree.tag_configure(tag, background=bg, foreground=fg,
                                        font=theme.font("strong") if tag == "group" else theme.font("body"))
            else:
                self.tree.tag_configure(tag, background=bg, foreground=fg)
        for t in (getattr(self, "txt_cur", None), getattr(self, "txt_new", None)):
            if t is not None:
                self._tag_lyrics(t)
        if hasattr(self, "btn_keep"):
            self._update_choice_buttons()
            self._style_interp()

    def _even_lyrics_split(self) -> None:
        if getattr(self, "_lyrics_split_done", False):
            return
        w = self._lyrics_paned.winfo_width()
        if w > 50:
            self._lyrics_paned.sash_place(0, w // 2, 1)
            self._lyrics_split_done = True

    def _save_split(self, _e=None) -> None:
        try:
            x = self._paned.sash_coord(0)[0]
        except tk.TclError:
            return
        if x > 0:
            self.app.settings.review_split = int(x)
            try:
                save_settings(self.app.settings)
            except OSError:
                pass

    def _lyrics_text(self, parent, which: str) -> tk.Text:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        t = tk.Text(frame, wrap="word", height=12, cursor="arrow", state="disabled", spacing1=2, spacing3=2)
        theme.register(t, "text")
        sb = ttk.Scrollbar(frame, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=lambda a, b, w=which, bar=sb: (bar.set(a, b), self._on_lyrics_scroll(w)))
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")


        self._tag_lyrics(t)
        t.tag_bind("pin", "<Enter>", lambda e, w=t: w.config(cursor="hand2"))
        t.tag_bind("pin", "<Leave>", lambda e, w=t: w.config(cursor="arrow"))
        t.bind("<Button-1>", lambda e, w=which: self._line_click(w, e))
        if which == "new":
            t.bind("<Button-3>", self._line_menu)
            t.tag_bind("ts", "<Enter>", lambda e, w=t: w.config(cursor="xterm"))
            t.tag_bind("ts", "<Leave>", lambda e, w=t: w.config(cursor="arrow"))
        return t

    # ============================================================ results
    def add_row(self, path: str, status: str | None = None, source: str | None = None, artist: str | None = None,
                title: str | None = None, local: str | None = None, online: str | None = None,
                choice: str | None = None, album: str | None = None) -> None:
        """Add or update a song row; None keeps the current value."""
        path = state_key(path)
        row = self.rows.get(path) or {"status": "", "source": "", "artist": "", "title": "", "local": "",
                                      "online": "", "choice": "", "album": ""}
        for k, v in (("status", status), ("source", source), ("artist", artist), ("title", title),
                     ("local", local), ("online", online), ("choice", choice), ("album", album)):
            if v is not None:
                row[k] = v
        self.rows[path] = row
        if hasattr(self.app, "session_changed"):
            self.app.session_changed()
        if not getattr(self.app, "_apply_bar_shown", True):
            self.app.show_apply_bar()
        if self._matches(path):
            self._upsert(path)
        elif self.tree.exists(path):
            parent = self.tree.parent(path)
            self.tree.delete(path)
            self._refresh_groups(parent)
        self._update_count()

    # -- grouping
    def _group_keys(self, path: str) -> tuple[str, str]:
        r = self.rows[path]
        folder = os.path.dirname(display_path(path))
        artist = r["artist"] or os.path.basename(os.path.dirname(folder)) or "(no artist)"
        album = r.get("album") or os.path.basename(folder) or "(no album)"
        return artist, album

    def _ensure_groups(self, path: str) -> str:
        artist, album = self._group_keys(path)
        a_id = "grp:" + artist.casefold()
        b_id = a_id + "\x1f" + album.casefold()
        many = len(self.rows) > 600
        if not self.tree.exists(a_id):
            self.tree.insert("", "end", iid=a_id, text=artist, open=not many, tags=("group",))
        if not self.tree.exists(b_id):
            self.tree.insert(a_id, "end", iid=b_id, text=album, open=not many, tags=("subgroup",))
        return b_id

    def _songs_under(self, node: str) -> list[str]:
        out = []
        for c in self.tree.get_children(node):
            if c.startswith("grp:"):
                out += self._songs_under(c)
            else:
                out.append(c)
        return out

    def _refresh_groups(self, node: str) -> None:
        """Update counts on a group row and its parent; drop empty groups."""
        while node and node.startswith("grp:"):
            parent = self.tree.parent(node)
            songs = self._songs_under(node)
            if not songs:
                self.tree.delete(node)
            else:
                rows = [self.rows[p] for p in songs if p in self.rows]
                n_local = sum(1 for r in rows if r["local"])
                n_online = sum(1 for r in rows if r["online"] == "found")
                choices = {r["choice"] for r in rows}
                choice = CHOICE_TEXT.get(next(iter(choices)), "") if len(choices) == 1 else "mixed"
                self.tree.item(node, values=(f"{len(rows)} song{'s' if len(rows) != 1 else ''}",
                                             f"{n_local} local" if n_local else "—",
                                             f"{n_online} found" if n_online else "—",
                                             f"⟳ {choice}" if choice else ""))
            node = parent

    def _update_count(self) -> None:
        shown = sum(1 for p in self.rows if self.tree.exists(p))
        self.v_count.set(f"{shown} shown / {len(self.rows)} songs")

    def _row_tag(self, row: dict) -> str:
        if row["status"] in PROBLEM_STATUSES:
            return "c_problem"
        if row["status"] in DONE_STATUSES:
            return "c_done"
        return {"local": "c_local", "online": "c_online", "custom": "c_custom", "remove": "c_custom",
                "keep": "c_keep"}.get(row["choice"], "c_keep")

    def _row_values(self, path):
        r = self.rows[path]
        status = SHORT_STATUS.get(r["status"], STATUS_LABELS.get(r["status"], r["status"]))
        choice = CHOICE_TEXT.get(r["choice"], r["choice"])
        return (status, r["local"] or "—", r["online"] or "—", f"⟳ {choice}" if choice else "")

    def _row_text(self, path) -> str:
        r = self.rows[path]
        return r["title"] or os.path.basename(display_path(path))

    def _upsert(self, path):
        vals, tags = self._row_values(path), (self._row_tag(self.rows[path]),)
        parent = self._ensure_groups(path)
        if self.tree.exists(path):
            old_parent = self.tree.parent(path)
            if old_parent != parent:
                self.tree.move(path, parent, "end")
                self._refresh_groups(old_parent)
            self.tree.item(path, text=self._row_text(path), values=vals, tags=tags)
        else:
            self.tree.insert(parent, "end", iid=path, text=self._row_text(path), values=vals, tags=tags)
        self._refresh_groups(parent)

    def _matches(self, path) -> bool:
        statuses = FILTERS.get(self.v_filter.get())
        r = self.rows[path]
        if statuses is not None and r["status"] not in statuses:
            return False
        q = self.v_search.get().strip().casefold()
        return not q or q in f"{r['artist']} {r.get('album', '')} {r['title']} {path}".casefold()

    def _refill(self) -> None:
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        touched = set()
        for p in self.rows:
            if self._matches(p):
                parent = self._ensure_groups(p)
                self.tree.insert(parent, "end", iid=p, text=self._row_text(p), values=self._row_values(p),
                                 tags=(self._row_tag(self.rows[p]),))
                touched.add(parent)
        for g in touched:
            self._refresh_groups(g)
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
            self.tree.see(sel[0])
        self._update_count()

    def load_earlier(self) -> None:
        try:
            st = State(self.app.settings.resolved_state_db())
            rows = st.iter_files()
            choices = {r[0]: st.current_choice(r[0]) for r in rows}
            st.close()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not read earlier results:\n{e}")
            return
        for path, status, source, artist, title, _, local, online, album in rows:
            if path not in self.rows:
                self.rows[path] = {"status": status, "source": source or "", "artist": artist or "",
                                   "title": title or "", "local": local or "", "online": online or "",
                                   "choice": choices.get(path, ""), "album": album or ""}
        self._refill()
        self.app.show_apply_bar()

    def _open_file(self) -> None:
        p = filedialog.askopenfilename(title="Open a song")
        if not p:
            return
        p = state_key(os.path.normpath(p))
        if p not in self.rows:
            self.add_row(p, "")
        self.v_filter.set("All")
        self.v_search.set("")
        self._refill()
        self.tree.selection_set(p)
        self.tree.see(p)
        self.app.show_apply_bar()

    def _tree_click(self, e):
        """Clicking a Choice cell cycles: found sources → Don't change."""
        if self.tree.identify_region(e.x, e.y) != "cell" or self.tree.identify_column(e.x) != "#4":
            return None
        node = self.tree.identify_row(e.y)
        sel = self.tree.selection()
        if node in sel and len(self.selected_songs()) > 1:
            self._cycle_many(self.selected_songs(), "sel:" + ",".join(sorted(sel)))
            return "break"      # keep the multi-selection
        if node.startswith("grp:"):
            self.cycle_group_choice(node)
            return "break"      # don't select / collapse the group
        if node:
            self.cycle_choice(node)
        return None

    def _tree_motion(self, e) -> None:
        if self.tree.identify_region(e.x, e.y) == "heading":
            text = COLUMN_HINTS.get(self.tree.identify_column(e.x), "")
            if text:
                self._head_tip.show(text, e.x_root, e.y_root + 90)
                return
        self._head_tip.hide()

    def selected_songs(self) -> list[str]:
        out, seen = [], set()
        for node in self.tree.selection():
            for p in (self._songs_under(node) if node.startswith("grp:") else [node]):
                if p not in seen and p in self.rows:
                    seen.add(p)
                    out.append(p)
        return out

    def _tree_menu(self, e) -> str:
        node = self.tree.identify_row(e.y)
        if not node:
            return "break"
        if node not in self.tree.selection():
            self.tree.selection_set(node)
            self.tree.focus(node)
        songs = self.selected_songs()
        if not songs:
            return "break"
        n = len(songs)
        what = f"{n} song{'s' if n != 1 else ''}"
        m = theme.register(tk.Menu(self, tearoff=0), "menu")
        target = "embed into" if self.app.settings.output_mode == "embed" else "save .lrc for"
        m.add_command(label=f"Save now — {target} {what}", command=lambda: self.app.apply_paths(songs))
        m.add_separator()
        m.add_command(label="Use Local lyrics", command=lambda: self.set_many(songs, "local"))
        m.add_command(label="Use Online lyrics", command=lambda: self.set_many(songs, "online"))
        m.add_command(label="Don't change", command=lambda: self.set_many(songs, "keep"))
        m.add_command(label="Reset to default", command=lambda: self.reset_many(songs))
        m.add_separator()
        m.add_command(label=f"Restore original lyrics ({what})…", command=lambda: self.app.restore_paths(songs))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()
        return "break"

    def set_many(self, songs: list[str], kind: str) -> int:
        """Set the same source on several songs (songs without it are left as they are)."""
        s, changed = self.app.settings, 0
        for p in songs:
            opts = {o[0]: o for o in review.choice_options(p, s)}
            if kind in opts:
                self._set_choice(p, opts[kind])
                changed += 1
        self.app.refresh_apply_bar()
        self.app.session_changed()
        if self.song is not None and self.song.path in songs:
            self._refresh_override(self.song.path)
        return changed

    def reset_many(self, songs: list[str]) -> None:
        s = self.app.settings
        st = State(s.resolved_state_db())
        try:
            for p in songs:
                review.clear_choice(p, s)
                choice = st.current_choice(p)
                prev = self.row_status_before_choice.pop(p, None)
                cur = self.rows.get(p, {}).get("status", "")
                self.add_row(p, prev if prev is not None else ("proposed" if cur in ("chosen", "locked") and
                                                               choice != "keep" else cur), choice=choice)
        finally:
            st.close()
        self.app.refresh_apply_bar()
        self.app.session_changed()
        if self.song is not None and self.song.path in songs:
            self._refresh_override(self.song.path)

    # ------------------------------------------------------------ session
    def session_data(self) -> dict:
        return {"rows": self.rows, "selected": self.tree.focus() or "", "filter": self.v_filter.get(),
                "search": self.v_search.get()}

    def load_session_rows(self, data: dict, include_saved: bool = False) -> int:
        """Bring back last session's songs. Songs whose lyrics were already saved (written to the file
        or a .lrc) are left out unless include_saved; then they're listed as Saved. Files are never
        changed by this. Returns how many songs were loaded."""
        rows = dict(data.get("rows") or {})
        try:
            st = State(self.app.settings.resolved_state_db())
            try:
                for p, r in list(rows.items()):
                    info = st.get_file_info(p)
                    if info.get("status") in ("restored", "embedded", "lrc_written"):
                        r["status"] = info["status"]          # the database knows what happened since
                    r["choice"] = st.current_choice(p)
            finally:
                st.close()
        except Exception:
            pass
        if not include_saved:
            rows = {p: r for p, r in rows.items() if r.get("status") not in SAVED_STATUSES}
        for p, r in rows.items():
            base = {"status": "", "source": "", "artist": "", "title": "", "local": "", "online": "",
                    "choice": "", "album": ""}
            base.update({k: v for k, v in r.items() if k in base})
            self.rows[p] = base
        if data.get("filter") in FILTERS:
            self.v_filter.set(data["filter"])
        self.v_search.set(data.get("search", ""))
        self._refill()
        sel = data.get("selected", "")
        if sel and self.tree.exists(sel):
            self.tree.selection_set(sel)
            self.tree.focus(sel)
            self.tree.see(sel)
        self.app.show_apply_bar()
        return len(rows)

    def cycle_group_choice(self, node: str) -> str:
        """Set every song in a group to the next of: Local → Online → Don't change (where available)."""
        return self._cycle_many(self._songs_under(node), node)

    def _cycle_many(self, songs: list[str], node: str) -> str:
        if not songs:
            return ""
        order = ["local", "online", "keep"]
        current = {self.rows[p]["choice"] for p in songs if p in self.rows}
        cur = next(iter(current)) if len(current) == 1 else self._group_target.get(node, "")
        target = order[(order.index(cur) + 1) % 3] if cur in order else "local"
        self._group_target[node] = target
        s = self.app.settings
        for p in songs:
            opts = {o[0]: o for o in review.choice_options(p, s)}
            kind = target if target in opts else ("keep" if target == "keep" else None)
            if kind is None:
                continue          # this song has no lyrics of that kind; leave it as it is
            self._set_choice(p, opts[kind])
        self.app.refresh_apply_bar()
        self.app.session_changed()
        if self.song is not None and self.song.path in songs:
            self._refresh_override(self.song.path)
        return target

    def _set_choice(self, path: str, opt) -> None:
        kind, label, text, lrc_path = opt
        s = self.app.settings
        if kind == "keep":
            review.lock_current(path, s)
            status = "locked"
        else:
            review.choose(path, text, label, s, lrc_path)
            status = "chosen"
        row = self.rows.get(path, {})
        if row.get("status") not in ("chosen", "locked"):
            self.row_status_before_choice[path] = row.get("status", "")
        self.add_row(path, status, choice=kind)

    def cycle_choice(self, path: str) -> str:
        s = self.app.settings
        opts = review.choice_options(path, s)
        kinds = [o[0] for o in opts]
        cur = self.rows.get(path, {}).get("choice", "")
        nxt = kinds[(kinds.index(cur) + 1) % len(kinds)] if cur in kinds else kinds[0]
        opt = next(o for o in opts if o[0] == nxt)
        kind = opt[0]
        self._set_choice(path, opt)
        self.app.refresh_apply_bar()
        self.app.session_changed()
        if self.song is not None and self.song.path == path:
            self._refresh_override(path)
        return kind

    def _on_select(self, _e=None) -> None:
        sel = self.tree.selection()
        focus = self.tree.focus()
        if focus in sel and not focus.startswith("grp:"):
            if self.song is None or self.song.path != focus:
                self.load_song(focus)
            return
        if len(sel) == 1 and not sel[0].startswith("grp:"):
            self.load_song(sel[0])

    # ========================================================= song loading
    def load_song(self, path: str) -> None:
        self.player.stop()
        self.cp_range = None
        self._token += 1
        token = self._token
        self.v_title.set(f"Loading {os.path.basename(display_path(path))}…")
        s = self.app.settings
        threading.Thread(target=self._load_worker, args=(token, path, s), daemon=True).start()

    def _collection_for(self, s):
        if not (s.use_collection and s.collection):
            return None
        key = tuple(s.collection)
        if self._collection_key != key:
            from .collection import LrcCollection
            self._collection = LrcCollection(s.collection)
            self._collection_key = key
        return self._collection

    def _load_worker(self, token, path, s) -> None:
        info = audio = None
        err = audio_err = ""
        try:
            info = review.gather(path, s, self._collection_for(s))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        if not err:
            try:
                audio = decode(display_path(path))
            except Exception as e:
                audio_err = f"Could not decode audio: {type(e).__name__}: {e}"
        self.app.events.put({"type": "review_loaded", "token": token, "path": path, "info": info,
                             "audio": audio, "err": err, "audio_err": audio_err})

    def handle(self, e: dict) -> None:
        t = e["type"]
        if t == "review_loaded":
            if e["token"] != self._token:
                return
            if e["err"]:
                self.v_title.set(os.path.basename(display_path(e["path"])))
                messagebox.showerror(APP_TITLE, e["err"])
                return
            self._show_song(e["info"], e["audio"], e["audio_err"])
        elif t == "review_lrclib":
            if e["token"] != self._token or self.song is None:
                return
            if e.get("err"):
                messagebox.showerror(APP_TITLE, f"LRCLIB search failed:\n{e['err']}")
                return
            known = {c.key for c in self.cands}
            new = [c for c in e["cands"] if c.key not in known]
            if not new:
                messagebox.showinfo(APP_TITLE, "LRCLIB returned no other synced lyrics for this song.")
                return
            self.cands[0:0] = new
            self._fill_candidates()
            self._select_candidate(0)
        elif t == "review_done":
            if e.get("err"):
                messagebox.showerror(APP_TITLE, e["err"])
            else:
                if e.get("status"):
                    st = self.rows.get(e["path"], {}).get("status", "")
                    choice = e.get("choice")
                    if e["status"] == "cleared":
                        self.add_row(e["path"], self.row_status_before_choice.pop(e["path"], st), choice=choice)
                    else:
                        if st not in ("chosen", "locked"):
                            self.row_status_before_choice[e["path"]] = st
                        self.add_row(e["path"], e["status"], choice=choice)
                self.app.refresh_apply_bar()
                if e.get("msg"):
                    self.app.log_line(e["msg"])
            if self.song is not None and e["path"] == self.song.path:
                self._refresh_override(e["path"])

    def _show_song(self, info: review.SongInfo, audio, audio_err: str) -> None:
        self.song, self.audio = info, audio
        m = info.meta
        name = os.path.basename(display_path(info.path))
        self.v_title.set(f"{m.artist or '?'} — {m.title or name}" + (f"   ({name})" if m.title else ""))
        self._show_override()
        if audio is not None:
            self.player.load(audio)
        self.v_audio_note.set(audio_err)
        lrc_mode = self.app.settings.output_mode == "lrc"
        self.lbl_cur.config(text="Current (.lrc file beside the song)" if lrc_mode else "Current (embedded)")
        self.cur_lines = lrcmod.display_lines(info.current)
        self.cur_segs = lrcmod.segments(info.current, self._duration())
        self._render(self.txt_cur, self.cur_lines,
                     empty="(no .lrc file yet)" if lrc_mode else "(no embedded lyrics)")
        self.cands = list(info.candidates)
        self._fill_candidates()
        prefs = info.prefs or {}
        idx = 0
        if prefs.get("cand"):
            idx = next((i for i, c in enumerate(self.cands) if c.key == prefs["cand"]), 0)
        self.cp_custom = "checkpoints" in prefs
        self.checkpoints = sorted(prefs.get("checkpoints", []))
        self._select_candidate(idx, restore=prefs)
        self._invalidate_scene()

    def _refresh_override(self, path: str) -> None:
        try:
            st = State(self.app.settings.resolved_state_db())
            self.song.override = st.get_override(path)
            st.close()
        except Exception:
            pass
        self._show_override()
        self._update_choice_buttons()

    def _show_override(self) -> None:
        ov = self.song.override if self.song else None
        if not ov:
            self.v_override.set("No per-file choice — follows settings")
        elif ov[0] == KEEP:
            self.v_override.set("Per-file choice: keep current lyrics")
        else:
            self.v_override.set(f"Per-file choice: {ov[2] or 'chosen lyrics'} — written when you apply")

    def _style_btn(self, btn: tk.Button, selected: bool, text: str) -> None:
        btn.config(text=("✓  " + text) if selected else text)
        theme.set_flat_kind(btn, "success" if selected else "neutral")

    def _update_choice_buttons(self) -> None:
        """Green ✓ on whichever of Keep current / Use new Apply will do for this file."""
        keep_sel = new_sel = False
        note = ""
        if self.song is not None:
            ov = self.song.override
            try:
                st = State(self.app.settings.resolved_state_db())
                pend = st.get_pending(self.song.path)
                choice = st.current_choice(self.song.path)
                st.close()
            except Exception:
                pend, choice = None, ""
            shown = self.new_text
            if choice == "keep":
                keep_sel = True
            elif ov is not None:
                new_sel = bool(shown is not None and lrcmod.same_lyrics(ov[1], shown)) or (
                    not ov[1] and self.cand is not None and self.cand.text is None)
                if not new_sel:
                    note = f"Will use: {ov[2] or CHOICE_TEXT.get(choice, choice)}"
            elif pend is not None:
                new_sel = bool(shown is not None and lrcmod.same_lyrics(pend[2], shown))
                if not new_sel:
                    note = f"Default: {pend[1]}"
        default = self.song is not None and self.song.override is None
        self._style_btn(self.btn_keep, keep_sel, "Keeping current" + (" (default)" if default else "")
                        if keep_sel else "Keep current")
        self._style_btn(self.btn_apply, new_sel, "Using new" + (" (default)" if default else "")
                        if new_sel else "Use new")
        self.v_choice_note.set(note)

    def _duration(self) -> float:
        if self.audio is not None:
            return self.audio.duration
        return self.song.duration if self.song else 0.0

    # ========================================================== candidates
    def _fill_candidates(self) -> None:
        labels = [c.label for c in self.cands] or ["(no candidates — try Search LRCLIB)"]
        self.cmb_cand.config(values=labels)

    def _select_candidate(self, idx: int, restore: dict | None = None) -> None:
        if not self.cands:
            self.cand = None
            self.cmb_cand.set("(no candidates — try Search LRCLIB)")
            self._set_timing(0.0, 1.0)
            self._recompute()
            return
        idx = max(0, min(idx, len(self.cands) - 1))
        self.cand = self.cands[idx]
        self.cmb_cand.current(idx)
        self.anchors = []
        self.sel_line = None
        if restore and restore.get("cand") == self.cand.key:
            self.pins = {k: float(v) for k, v in (restore.get("pins") or {}).items()}
            self.edited = set(restore.get("edited") or [])
            self.interpolate = bool(restore.get("interpolate")) and bool(self.pins)
            self._set_timing(restore.get("offset", 0.0), restore.get("factor", 1.0))
        else:
            self.pins, self.edited, self.interpolate = {}, set(), False
            self._set_timing(0.0, 1.0)
        self._recompute()

    def _set_timing(self, offset: float, factor: float) -> None:
        self._suspend_trace = True
        self.offset, self.factor = offset, factor
        self.v_offset.set(round(offset * 1000, 1))
        self.v_stretch.set(round(factor * 100, 4))
        self._suspend_trace = False
        self.v_anchors.set(f"{len(self.anchors)} aligned line(s)" if self.anchors else "")

    def _timing_changed(self) -> None:
        if self._suspend_trace:
            return
        try:
            off = float(self.v_offset.get()) / 1000.0
            fac = float(self.v_stretch.get()) / 100.0
        except (tk.TclError, ValueError):
            return
        if fac <= 0:
            return
        self.offset, self.factor = off, fac
        self.interpolate = False
        if self._recompute_job:
            self.after_cancel(self._recompute_job)
        self._recompute_job = self.after(150, self._recompute)

    def _recompute(self) -> None:
        self._recompute_job = None
        c = self.cand
        text = c.text if c else None
        self.orig_lines = lrcmod.display_lines(text) if text and c.synced else []
        if text and c.synced:
            text = lrcmod.retime(text, self.offset, self.factor, self.pins, self.interpolate)
        self._style_interp()
        self.new_text = text
        self.new_lines = lrcmod.display_lines(text)
        self.new_segs = lrcmod.segments(text, self._duration())
        if c is None:
            empty = "(nothing to compare)"
        elif c.text is None:
            empty = "(lyrics will be removed)"
        else:
            empty = "(empty)"
        self._render(self.txt_new, self.new_lines, empty=empty, pins=bool(self.orig_lines))
        if self.sel_line is not None and self.sel_line < len(self.new_lines):
            self.txt_new.tag_add("pick", f"{self.sel_line + 1}.0", f"{self.sel_line + 1}.end")
        self.btn_apply.config(state="normal" if c is not None else "disabled")
        self._update_choice_buttons()
        if not self.cp_custom:
            src = text if lrcmod.is_synced(text) else self.song.current if self.song else ""
            self.checkpoints = review.default_checkpoints(src, self._duration())
        self._now = {"cur": -1, "new": -1}
        self._draw_overview()
        self._draw_cp_buttons()
        self._save_prefs_later()

    def _render(self, widget: tk.Text, lines, empty: str, pins: bool = False) -> None:
        y = widget.yview()[0]
        widget.config(state="normal")
        widget.delete("1.0", "end")
        if not lines:
            widget.insert("end", empty, "ts")
        for i, (t, txt) in enumerate(lines):
            if pins and t is not None:
                pinned = i < len(self.orig_lines) and lrcmod.pin_key(self.orig_lines[i][0]) in self.pins
                widget.insert("end", "📌" if pinned else " ○ ", ("pin", "pinned" if pinned else "unpinned"))
                widget.insert("end", " ")
            if t is not None:
                key = lrcmod.pin_key(self.orig_lines[i][0]) if pins and i < len(self.orig_lines) else None
                tags = ("ts",)
                if key in self.edited:
                    tags = ("ts", "edited")
                elif key in self.pins:
                    tags = ("ts", "ts_pinned")
                widget.insert("end", lrcmod.format_ts(t), tags)
                widget.insert("end", "  ")
            widget.insert("end", txt + ("\n" if i < len(lines) - 1 else ""))
        widget.config(state="disabled")
        widget.yview_moveto(y if pins else 0)

    # ============================================================== timing
    def _line_click(self, which: str, e) -> None:
        w = self.txt_cur if which == "cur" else self.txt_new
        lines = self.cur_lines if which == "cur" else self.new_lines
        self.after_idle(self._grab_keys)
        at = w.index(f"@{e.x},{e.y}")
        idx = int(at.split(".")[0]) - 1
        if not (0 <= idx < len(lines)):
            return
        if which == "new" and "pin" in w.tag_names(at):
            self.toggle_pin(idx)
            return
        if which == "new" and "ts" in w.tag_names(at) and self.orig_lines:
            self.sel_line = idx
            self._edit_time(idx)
            return
        if which == "new":
            self.sel_line = idx
            w.tag_remove("pick", "1.0", "end")
            w.tag_add("pick", f"{idx + 1}.0", f"{idx + 1}.end")
        t = lines[idx][0]
        if t is not None and self.audio is not None:
            self.cp_range = None
            self.player.seek(t)

    def _toggle_interpolate(self) -> None:
        if not self.interpolate and not self.pins:
            messagebox.showinfo(APP_TITLE, "Pin or edit at least one line first (two or more for a stretch "
                                           "between them). Lines you haven't touched are then interpolated.")
            return
        self.interpolate = not self.interpolate
        self._recompute()

    def clear_pins(self) -> None:
        """Remove pins that are not edited times."""
        self.pins = {k: v for k, v in self.pins.items() if k in self.edited}
        if not self.pins:
            self.interpolate = False
        self._recompute()

    def clear_edits(self) -> None:
        """Reset every edited time (their lines go back to the computed timing)."""
        for k in list(self.edited):
            self.pins.pop(k, None)
        self.edited = set()
        if not self.pins:
            self.interpolate = False
        self._recompute()

    def _style_interp(self) -> None:
        if not hasattr(self, "btn_interp"):
            return
        n_edit = len(self.edited)
        n_pin = len(self.pins) - n_edit
        self.v_pins.set(f"{n_pin} pinned · {n_edit} edited" if (n_pin or n_edit) else "")
        if self.interpolate:
            n = len(self.pins)
            self.btn_interp.config(text=f"✓  Interpolated from {n} pin{'s' if n != 1 else ''}")
            theme.set_flat_kind(self.btn_interp, "success")
        else:
            self.btn_interp.config(text="Interpolate from pins")
            theme.set_flat_kind(self.btn_interp, "neutral")

    def toggle_pin(self, idx: int) -> None:
        """Pin / unpin a New-pane line at its current time."""
        if not (0 <= idx < len(self.orig_lines)) or idx >= len(self.new_lines):
            return
        key = lrcmod.pin_key(self.orig_lines[idx][0])
        if key in self.pins:
            del self.pins[key]
            if not self.pins:
                self.interpolate = False
        else:
            self.pins[key] = float(self.new_lines[idx][0])
        self.edited.discard(key)
        self._recompute()

    # ---------------------------------------------------- editing timestamps
    def _line_key(self, idx: int) -> str | None:
        if 0 <= idx < len(self.orig_lines):
            return lrcmod.pin_key(self.orig_lines[idx][0])
        return None

    def set_line_time(self, idx: int, seconds: float | None) -> None:
        """Give line `idx` of the New pane a fixed time; None resets it to the computed time."""
        key = self._line_key(idx)
        if key is None:
            return
        if seconds is None:
            self.pins.pop(key, None)
            self.edited.discard(key)
            if not self.pins:
                self.interpolate = False
        else:
            self.pins[key] = max(0.0, min(float(seconds), self._duration() or float(seconds)))
            self.edited.add(key)
        self._recompute()

    def _edit_time(self, idx: int) -> None:
        """Inline editor over the timestamp: Enter saves, Esc cancels, empty + Enter resets."""
        self._close_editor()
        if not (0 <= idx < len(self.new_lines)) or self.new_lines[idx][0] is None:
            return
        w = self.txt_new
        rng = w.tag_nextrange("ts", f"{idx + 1}.0", f"{idx + 1}.end")
        if not rng:
            return
        w.see(rng[0])
        w.update_idletasks()
        box = w.bbox(rng[0])
        if not box:
            return
        x, y, _, h = box
        ent = ttk.Entry(w, width=10)
        ent.insert(0, lrcmod.format_ts(self.new_lines[idx][0]))
        ent.select_range(0, "end")
        ent.place(x=x, y=y - 1, height=h + 4)
        ent.focus_set()
        self._editor = ent

        def commit(_e=None):
            if self._editor is not ent:      # already closed (Esc, or committed once)
                return
            raw = ent.get().strip()
            self._close_editor()
            if not raw:
                self.set_line_time(idx, None)
                return
            secs = lrcmod.parse_length(raw)
            if secs is None:
                messagebox.showinfo(APP_TITLE, f"'{raw}' isn't a time. Use m:ss.xx, e.g. 1:23.45")
                return
            self.set_line_time(idx, secs)
        ent.bind("<Return>", commit)
        ent.bind("<KP_Enter>", commit)
        ent.bind("<Escape>", lambda e: self._close_editor())
        ent.bind("<FocusOut>", commit)          # clicking elsewhere keeps what was typed

    def _close_editor(self) -> None:
        ed = getattr(self, "_editor", None)
        self._editor = None
        if ed is not None:
            try:
                ed.destroy()
            except tk.TclError:
                pass
            self._grab_keys()

    def _line_menu(self, e) -> str:
        w = self.txt_new
        at = w.index(f"@{e.x},{e.y}")
        idx = int(at.split(".")[0]) - 1
        if not (0 <= idx < len(self.new_lines)) or self.new_lines[idx][0] is None or not self.orig_lines:
            return "break"
        key = self._line_key(idx)
        m = theme.register(tk.Menu(self, tearoff=0), "menu")
        m.add_command(label="Edit time…", command=lambda: self._edit_time(idx))
        m.add_command(label=f"Set to playhead ({fmt_time(self.player.position())})",
                      command=lambda: self.set_line_time(idx, self.player.position()))
        m.add_command(label="Unpin" if key in self.pins else "Pin at this time", command=lambda: self.toggle_pin(idx))
        m.add_separator()
        m.add_command(label="Reset this timestamp", command=lambda: self.set_line_time(idx, None),
                      state="normal" if key in self.pins else "disabled")
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()
        return "break"

    def _align(self) -> None:
        if self.cand is None or not self.cand.synced:
            messagebox.showinfo(APP_TITLE, "Choose synced lyrics in the New pane first.")
            return
        if self.sel_line is None or self.sel_line >= len(self.new_lines):
            messagebox.showinfo(APP_TITLE, "Click a line in the New pane, play to where that line is "
                                           "actually sung, then press Align.")
            return
        t_new = self.new_lines[self.sel_line][0]
        orig = (t_new - self.offset) / self.factor
        target = self.player.position()
        self.anchors = [a for a in self.anchors if abs(a[0] - orig) > 0.01] + [(orig, target)]
        off, fac = lrcmod.solve_alignment(self.anchors, self.factor)
        self._set_timing(off, fac)
        self._recompute()

    def _stretch_to_length(self) -> None:
        """Scale the new lyrics by (this song's length / length of the version they were timed for)."""
        if self.cand is None or not self.cand.synced:
            messagebox.showinfo(APP_TITLE, "Choose synced lyrics in the New pane first.")
            return
        dur = self._duration()
        src = self.cand.src_length
        if not src:
            from tkinter import simpledialog
            ans = simpledialog.askstring(
                APP_TITLE, "How long is the version these lyrics were timed for? (m:ss)\n"
                           f"This song is {fmt_time(dur)}.", parent=self)
            src = lrcmod.parse_length(ans or "")
            if not src:
                return
            self.cand.src_length = src
        if src <= 0 or dur <= 0:
            return
        self.anchors = []
        self._set_timing(0.0, dur / src)
        self._recompute()
        self.v_anchors.set(f"stretched {fmt_time(src)} → {fmt_time(dur)}")

    def _reset_timing(self) -> None:
        self.anchors = []
        self.pins, self.edited, self.interpolate = {}, set(), False
        self._set_timing(0.0, 1.0)
        self._recompute()

    # ========================================================= checkpoints
    def _ov_time(self, x: float) -> float:
        w = max(1, self.ov.winfo_width())
        return max(0.0, min(self._duration(), x / w * self._duration()))

    def _ov_press(self, e) -> None:
        self._grab_keys()
        self._ov_drag = {"x": e.x, "moved": False}

    def _ov_motion(self, e) -> None:
        d = self._ov_drag
        if d is None or self.audio is None:
            return
        if d["moved"] or abs(e.x - d["x"]) > 4:
            d["moved"] = True
            self.cp_range = None
            self.player.seek(self._ov_time(e.x))

    def _ov_release(self, e) -> None:
        """A plain click (no drag) on the overview jumps there."""
        d, self._ov_drag = self._ov_drag, None
        if d is None or d["moved"] or self.audio is None:
            return
        self.cp_range = None
        self.player.seek(self._ov_time(e.x))

    def _ov_click(self, e) -> None:
        """Add a checkpoint at x."""
        if not self._duration():
            return
        self.checkpoints = sorted(self.checkpoints + [round(self._ov_time(e.x), 2)])
        self.cp_custom = True
        self._checkpoints_changed()

    def _nearest_checkpoint(self, x: float) -> int | None:
        if not self.checkpoints or not self._duration():
            return None
        w = max(1, self.ov.winfo_width())
        xs = [t / self._duration() * w for t in self.checkpoints]
        i = min(range(len(xs)), key=lambda k: abs(xs[k] - x))
        return i if abs(xs[i] - x) <= max(12, w * 0.01) else None

    def _ov_ctrl_click(self, e) -> str:
        """Ctrl+click on the overview: remove the checkpoint under the pointer, or add one there."""
        self._grab_keys()
        self._ov_drag = None
        if not self._duration():
            return "break"
        i = self._nearest_checkpoint(e.x)
        if i is not None:
            self.delete_checkpoint(i)
        else:
            self._ov_click(e)
        return "break"

    def _ov_right_click(self, e) -> str:
        """Right-click on the overview removes the nearest checkpoint (never adds)."""
        self._grab_keys()
        i = self._nearest_checkpoint(e.x)
        if i is not None:
            self.delete_checkpoint(i)
        return "break"

    def delete_checkpoint(self, i: int) -> str:
        if 0 <= i < len(self.checkpoints):
            del self.checkpoints[i]
            self.cp_custom = True
            if self.cp_range is not None:
                self.player.pause()
                self.cp_range = None
            self.cp_cycle = -1
            self._checkpoints_changed()
        return "break"

    def _sc_press(self, e) -> None:
        self._grab_keys()
        if self.audio is not None:
            self._sc_drag = {"x": e.x, "pos": self.player.position(), "moved": False}

    def _sc_motion(self, e) -> None:
        d = self._sc_drag
        if d is None or self.audio is None:
            return
        if d["moved"] or abs(e.x - d["x"]) > 3:
            d["moved"] = True
            w = max(1, self.sc.winfo_width())
            self.cp_range = None
            self.player.seek(d["pos"] - (e.x - d["x"]) / w * WINDOW)   # drag the waveform like tape

    def _sc_release(self, e) -> None:
        d, self._sc_drag = self._sc_drag, None
        if d is not None and not d["moved"]:
            self._sc_click(e)

    def _sc_click(self, e) -> None:
        if self.audio is None:
            return
        w = max(1, self.sc.winfo_width())
        self.cp_range = None
        self.player.seek(self.player.position() + (e.x - w / 2) / w * WINDOW)

    def _default_checkpoints(self) -> None:
        self.cp_custom = False
        self._recompute()

    def _checkpoints_changed(self) -> None:
        self._draw_overview()
        self._draw_cp_buttons()
        self._save_prefs_later()

    def play_checkpoint(self, i: int) -> None:
        self._grab_keys()
        if self.audio is None or not (0 <= i < len(self.checkpoints)):
            return
        self.cp_cycle = i
        t = self.checkpoints[i]
        try:
            span = max(0.1, float(self.v_cp_len.get()))
        except (tk.TclError, ValueError):
            span = 1.0
        start, end = max(0.0, t - span), min(self._duration(), t + span)
        self.player.seek(start)
        self.cp_range = (start, end)
        self.player.play()

    def next_checkpoint(self) -> None:
        if self.checkpoints:
            self.play_checkpoint((self.cp_cycle + 1) % len(self.checkpoints))

    def _draw_cp_buttons(self) -> None:
        for b in self._cp_buttons:
            b.destroy()
        self._cp_buttons = []
        self.cpbar.delete("all")
        dur, w = self._duration(), max(1, self.cpbar.winfo_width())
        if not dur:
            return
        for i, t in enumerate(self.checkpoints):
            label = f"▶{i + 1}" if i < 9 else "▶"
            b = theme.flat_button(self.cpbar, label, lambda k=i: self.play_checkpoint(k), padx=6, pady=1,
                                  takefocus=0)
            theme.set_flat_kind(b, "accent")
            theme.hint(b, f"Play checkpoint {i + 1} at {fmt_time(t)}" + (f" (key {i + 1})" if i < 9 else "")
                       + ". Right-click to delete it.")
            for seq in ("<Button-3>", "<Button-2>") if sys.platform == "darwin" else ("<Button-3>",):
                b.bind(seq, lambda e, k=i: self.delete_checkpoint(k))
            x = min(max(t / dur * w, 16), w - 16)
            self.cpbar.create_window(x, 13, window=b)
            self._cp_buttons.append(b)

    def _save_globals(self) -> None:
        s = self.app.settings
        try:
            s.checkpoint_seconds = max(0.1, float(self.v_cp_len.get()))
        except (tk.TclError, ValueError):
            pass
        s.loop_checkpoints = bool(self.v_loop.get())
        s.playback_volume = int(round(self.v_volume.get()))
        try:
            save_settings(s)
        except OSError:
            pass

    def set_volume(self, level: float, save: bool = True) -> None:
        self.v_volume.set(max(0.0, min(100.0, float(level))))
        self._volume_changed()
        if save:
            self._save_globals()

    def _volume_wheel(self, e) -> str:
        up = getattr(e, "delta", 0) > 0 or getattr(e, "num", 0) == 4
        self.set_volume(round(self.v_volume.get()) + (2 if up else -2))
        return "break"

    def _edit_volume(self) -> None:
        """Click the % to type a level: Enter/clicking away saves, Esc cancels."""
        if getattr(self, "_vol_editor", None) is not None:
            return
        lbl = self.lbl_volume
        ent = ttk.Entry(lbl.master, width=5, justify="right")
        ent.insert(0, str(int(round(self.v_volume.get()))))
        ent.select_range(0, "end")
        ent.place(in_=lbl, x=0, y=0, relwidth=1.0, relheight=1.0)
        ent.focus_set()
        self._vol_editor = ent

        def close(save: bool):
            if self._vol_editor is not ent:
                return
            raw = ent.get().strip().rstrip("%").strip()
            self._vol_editor = None
            ent.destroy()
            self._grab_keys()
            if save and raw:
                try:
                    self.set_volume(float(raw))
                except ValueError:
                    pass
        ent.bind("<Return>", lambda e: close(True))
        ent.bind("<KP_Enter>", lambda e: close(True))
        ent.bind("<Escape>", lambda e: close(False))
        ent.bind("<FocusOut>", lambda e: close(True))

    def _volume_changed(self, _value=None) -> None:
        v = max(0.0, min(100.0, float(self.v_volume.get())))
        self.player.volume = volume_gain(v)
        self.v_volume_txt.set(f"{int(round(v))}%")

    def _save_prefs_later(self) -> None:
        if getattr(self, "_prefs_job", None):
            self.after_cancel(self._prefs_job)
        self._prefs_job = self.after(600, self._save_prefs)

    def _save_prefs(self) -> None:
        self._prefs_job = None
        if self.song is None:
            return
        prefs = {"cand": self.cand.key if self.cand else "", "offset": self.offset, "factor": self.factor,
                 "pins": dict(self.pins), "edited": sorted(self.edited), "interpolate": self.interpolate}
        if self.cp_custom:
            prefs["checkpoints"] = self.checkpoints
        if prefs != (self.song.prefs or {}):
            try:
                review.save_prefs(self.song.path, prefs, self.app.settings)
                self.song.prefs = prefs
            except Exception:
                pass

    # ============================================================= drawing
    def _draw_overview(self) -> None:
        c = self.ov
        c.delete("all")
        self._ov_lane_items = {"cur": [], "new": []}
        self._ov_active = {"cur": -1, "new": -1}
        w, h = max(2, c.winfo_width()), int(c.cget("height"))
        dur = self._duration()
        if not dur:
            return
        mid, amp = 24, 22
        if self.audio is not None:
            import numpy as np

            env = envelope(self.audio.peaks, 0, dur, w)
            hh = mid * 2
            ys = np.arange(hh, dtype=np.float32)[:, None]
            img = np.empty((hh, w, 3), dtype=np.uint8)
            img[:] = _hex_rgb(BG)
            img[np.abs(ys - mid) <= env[None, :] * amp] = _hex_rgb(WAVE)
            self._ov_photo = tk.PhotoImage(master=c, width=w, height=hh, format="PPM",
                                           data=f"P6 {w} {hh} 255 ".encode() + img.tobytes())
            c.create_image(0, 0, image=self._ov_photo, anchor="nw")
        for which, segs, y0, cols in (("cur", self.cur_segs, 52, (CUR_A, CUR_B)),
                                      ("new", self.new_segs, 64, (NEW_A, NEW_B))):
            for i, sg in enumerate(segs):
                x0, x1 = sg.start / dur * w, max(sg.start / dur * w + 1, sg.end / dur * w - 1)
                item = c.create_rectangle(x0, y0, x1, y0 + 10, fill=cols[i % 2], outline="")
                self._ov_lane_items[which].append(item)
                for ws, _, _ in sg.words[1:]:
                    xw = ws / dur * w
                    c.create_line(xw, y0, xw, y0 + 10, fill=BG)
        for i, t in enumerate(self.checkpoints):
            x = t / dur * w
            c.create_line(x, 0, x, h, fill=CP_COLOR, dash=(3, 2))
            c.create_text(x + 3, 2, text=str(i + 1) if i < 9 else "", anchor="nw", fill=CP_COLOR,
                          font=("TkDefaultFont", 8, "bold"))
        self._ov_pos = c.create_line(0, 0, 0, h, fill=PLAYHEAD, width=2)

    # ------------------------------------------- scrolling waveform (pre-rendered, scrolled view)
    # The waveform is rendered once into image tiles and the lyric lanes are created once, in
    # "world" coordinates (x = pad + seconds * px_per_second). Each frame only scrolls the canvas
    # view and moves a few items (playhead, progress fill, current word) instead of redrawing.
    TILE = 2048
    WAVE_H = 86

    def _invalidate_scene(self) -> None:
        self._scene = None
        self.sc.delete("all")

    def _ensure_scene(self) -> dict | None:
        if self.audio is None:
            return None
        c = self.sc
        w = max(2, c.winfo_width())
        sc = getattr(self, "_scene", None)
        if sc is None or sc["w"] != w or sc["audio"] is not self.audio:
            c.delete("all")
            dur = self.audio.duration
            pps = w / WINDOW
            n = max(1, int(round(dur * pps)))
            sc = {"w": w, "audio": self.audio, "pps": pps, "pad": w / 2.0, "total": n + w,
                  "env": envelope(self.audio.peaks, 0.0, dur, n), "tiles": {}, "lanes": None}
            c.configure(scrollregion=(0, 0, sc["total"], int(c.cget("height"))), xscrollincrement=1)
            h = int(c.cget("height"))
            sc["center"] = c.create_line(0, 0, 0, h, fill=PLAYHEAD, width=2, tags="center")
            self._scene = sc
        lanes = (id(self.cur_segs), id(self.new_segs))
        if sc["lanes"] != lanes:
            self._build_lanes(sc)
            sc["lanes"] = lanes
        return sc

    def _build_lanes(self, sc: dict) -> None:
        c = self.sc
        c.delete("lane")
        pad, pps = sc["pad"], sc["pps"]
        sc["lane_items"] = {}
        for which, segs, y0, cols, on in (("cur", self.cur_segs, 90, (CUR_A, CUR_B), CUR_ON),
                                          ("new", self.new_segs, 109, (NEW_A, NEW_B), NEW_ON)):
            for i, sg in enumerate(segs):
                x0, x1 = pad + sg.start * pps, pad + sg.end * pps - 1
                c.create_rectangle(x0, y0, x1, y0 + 17, fill=cols[i % 2], outline="", tags=("lane", "lanebg"))
            prog = c.create_rectangle(0, y0, 0, y0 + 17, fill=on, outline="", state="hidden",
                                      tags=("lane", "laneprog"))
            for sg in segs:
                for ws, _, _ in sg.words:
                    if ws > sg.start:
                        x = pad + ws * pps
                        c.create_line(x, y0, x, y0 + 17, fill=BG, tags=("lane", "lanediv"))
            word = c.create_rectangle(0, y0, 0, y0 + 17, outline="#ffffff", width=1, state="hidden",
                                      tags=("lane", "laneword"))
            texts = []
            for sg in segs:
                x0, x1 = pad + sg.start * pps, pad + sg.end * pps - 1
                room = int((x1 - x0) / 7)
                if room > 2:
                    txt = sg.text if len(sg.text) <= room else sg.text[: max(1, room - 1)] + "…"
                    texts.append(c.create_text(x0 + 3, y0 + 9, text=txt, anchor="w", fill="#111",
                                               font=("TkDefaultFont", 8), tags=("lane", "lanetext")))
                else:
                    texts.append(None)
            # label that sticks to the left edge for a line that started before the view
            sticky = c.create_text(0, y0 + 9, text="", anchor="w", fill="#111", font=("TkDefaultFont", 8),
                                   state="hidden", tags=("lane", "lanetext"))
            sc["lane_items"][which] = (segs, prog, word, texts, sticky)
        sc["sticky_hidden"] = {}
        c.tag_raise("laneprog", "lanebg")
        c.tag_raise("lanediv")
        c.tag_raise("laneword")
        c.tag_raise("lanetext")
        c.tag_raise("center")

    def _tile(self, sc: dict, k: int) -> None:
        env = sc["env"]
        a, b = k * self.TILE, min(len(env), (k + 1) * self.TILE)
        if b <= a or k in sc["tiles"]:
            return
        import numpy as np

        h, mid, amp = self.WAVE_H, 44, 40
        ys = np.arange(h, dtype=np.float32)[:, None]
        mask = np.abs(ys - mid) <= env[None, a:b] * amp
        img = np.empty((h, b - a, 3), dtype=np.uint8)
        img[:] = _hex_rgb(BG)
        img[mask] = _hex_rgb(WAVE)
        photo = tk.PhotoImage(master=self.sc, width=b - a, height=h, format="PPM",
                              data=f"P6 {b - a} {h} 255 ".encode() + img.tobytes())
        item = self.sc.create_image(sc["pad"] + a, 0, image=photo, anchor="nw", tags="tile")
        self.sc.tag_lower(item)
        sc["tiles"][k] = (photo, item)

    def _draw_scroll(self, pos: float) -> None:
        sc = self._ensure_scene()
        if sc is None:
            return
        c, w, pps, pad = self.sc, sc["w"], sc["pps"], sc["pad"]
        left = pos * pps                                   # world x of the view's left edge
        c.xview_moveto(max(0.0, left) / sc["total"])
        first, last = int((left - pad) // self.TILE), int((left + w - pad) // self.TILE)
        for k in range(max(0, first), last + 1):
            self._tile(sc, k)
        for k in [k for k in sc["tiles"] if k < first - 2 or k > last + 2]:    # bound memory
            photo, item = sc["tiles"].pop(k)
            c.delete(item)
        cx = left + w / 2
        h = int(c.cget("height"))
        c.coords(sc["center"], cx, 0, cx, h)
        view_t = (left - pad) / pps                        # time at the view's left edge
        for which, (segs, prog, word, texts, sticky) in sc.get("lane_items", {}).items():
            j = lrcmod.segment_at(segs, view_t)
            prev = sc["sticky_hidden"].get(which)
            if prev is not None and prev != j and texts[prev] is not None:
                c.itemconfigure(texts[prev], state="normal")
            if j >= 0 and texts[j] is not None and segs[j].start < view_t:
                sg = segs[j]
                room = int((pad + sg.end * pps - 1 - left) / 7)
                if room > 2:
                    t = sg.text if len(sg.text) <= room else sg.text[: max(1, room - 1)] + "…"
                    c.itemconfigure(sticky, text=t, state="normal")
                    c.coords(sticky, left + 3, 90 + 9 if which == "cur" else 109 + 9)
                else:
                    c.itemconfigure(sticky, state="hidden")
                c.itemconfigure(texts[j], state="hidden")
                sc["sticky_hidden"][which] = j
            else:
                c.itemconfigure(sticky, state="hidden")
                sc["sticky_hidden"][which] = None
            i = lrcmod.segment_at(segs, pos)
            if i < 0:
                c.itemconfigure(prog, state="hidden")
                c.itemconfigure(word, state="hidden")
                continue
            sg = segs[i]
            y0 = 90 if which == "cur" else 109
            c.coords(prog, pad + sg.start * pps, y0, pad + pos * pps, y0 + 17)
            c.itemconfigure(prog, state="normal")
            wd = next(((ws, we) for ws, we, _ in sg.words if ws <= pos < we), None)
            if wd:
                c.coords(word, pad + wd[0] * pps, y0, pad + wd[1] * pps - 1, y0 + 17)
                c.itemconfigure(word, state="normal")
            else:
                c.itemconfigure(word, state="hidden")

    # =============================================================== hover
    def _hover(self, e) -> None:
        dur = self._duration()
        if not dur or self.song is None:
            return
        if e.widget is self.ov:
            t = self._ov_time(e.x)
        else:
            w = max(1, self.sc.winfo_width())
            t = self.player.position() + (e.x - w / 2) / w * WINDOW
        if not 0 <= t <= dur:
            self.tooltip.hide()
            return
        parts = [fmt_time(t)]
        for name, segs in (("Current", self.cur_segs), ("New", self.new_segs)):
            i = lrcmod.segment_at(segs, t)
            if i >= 0:
                sg = segs[i]
                word = next((wd for ws, we, wd in sg.words if ws <= t < we), "")
                parts.append(f"{name}: {sg.text}" + (f"   [{word}]" if word else ""))
            elif segs:
                parts.append(f"{name}: —")
        parts.append("Click: jump · drag: scrub" + (" · Ctrl+click: add/remove checkpoint" if e.widget is self.ov else ""))
        self.tooltip.show("\n".join(parts), e.x_root, e.y_root)

    # ============================================================ playback
    def _toggle_play(self) -> None:
        if self.audio is None:
            return
        self.cp_range = None
        self.player.toggle()

    def _nudge(self, sec: float) -> None:
        if self.audio is not None:
            self.cp_range = None
            self.player.seek(self.player.position() + sec)

    def key(self, ch: str, focus) -> bool:
        """Global key handling while this tab is shown; returns True if used."""
        cls = focus.winfo_class() if focus is not None else ""
        if self._is_typing(focus, cls):
            return False
        if ch == "space":
            if cls in ("Button", "TButton", "TCheckbutton", "TRadiobutton"):
                return False   # Space already presses the focused button
            self._toggle_play()
            return True
        if ch.startswith("KP_") and ch[3:].isdigit():
            ch = ch[3:]
        if len(ch) == 1 and ch.isdigit():
            n = int(ch)
            self.next_checkpoint() if n == 0 else self.play_checkpoint(n - 1)
            return True
        return False

    @staticmethod
    def _is_typing(focus, cls: str) -> bool:
        """True only when keystrokes are going into an editable field."""
        if cls not in TEXT_INPUTS:
            return False
        try:
            state = str(focus.cget("state"))
        except tk.TclError:
            state = "normal"
        return state not in ("disabled", "readonly")

    def _grab_keys(self) -> None:
        """Give keyboard focus back to the player so number keys / Space reach it."""
        try:
            self.focus_set()
        except tk.TclError:
            pass

    def _display_pos(self, actual: float) -> float:
        """Playback position for drawing: jumps (seek, scrub, checkpoint) glide over ANIM seconds."""
        now = time.monotonic()
        if self._last_actual is not None:
            expected = self._last_actual + ((now - self._last_t) if self.player.playing else 0.0)
            if abs(actual - expected) > 0.15:
                self._jump = (self._disp_pos - actual, now)
        self._last_actual, self._last_t = actual, now
        disp = actual
        if self._jump is not None:
            off0, t0 = self._jump
            p = (now - t0) / ANIM
            if p >= 1:
                self._jump = None
            else:
                disp = actual + off0 * (1 - ease_out(p))
        self._disp_pos = disp
        return disp

    def _tick(self) -> None:
        try:
            if self.audio is not None and self.cp_range is not None:
                pos = self.player.position()
                start, end = self.cp_range
                if pos >= end - 0.005 or not self.player.playing:
                    if self.v_loop.get() and self.player.playing:
                        self.player.seek(start)
                    else:
                        self.player.pause()
                        self.cp_range = None
            self._step_scroll_anims()
            if self.audio is not None and self.winfo_ismapped():
                pos = self.player.position()
                disp = self._display_pos(pos)
                dur = self._duration()
                self.v_time.set(f"{fmt_time(pos)} / {fmt_time(dur)}")
                self.btn_play.config(text="⏸ Pause" if self.player.playing else "▶ Play")
                if self.player.silent_reason:
                    self.v_audio_note.set(f"Silent playback: {self.player.silent_reason}")
                w = max(1, self.ov.winfo_width())
                if dur and getattr(self, "_ov_pos", None):
                    x = disp / dur * w
                    self.ov.coords(self._ov_pos, x, 0, x, int(self.ov.cget("height")))
                self._draw_scroll(disp)
                self._highlight("cur", self.txt_cur, self.cur_lines, pos)
                self._highlight("new", self.txt_new, self.new_lines, pos)
                self._ov_highlight("cur", self.cur_segs, pos, (CUR_A, CUR_B), CUR_ON)
                self._ov_highlight("new", self.new_segs, pos, (NEW_A, NEW_B), NEW_ON)
        finally:
            self.after(TICK_MS, self._tick)

    # ------------------------------------------------- lyrics scrolling (eased, kept in sync)
    def _pane(self, which):
        return (self.txt_cur, self.cur_lines) if which == "cur" else (self.txt_new, self.new_lines)

    def _scroll_to(self, which: str, top: float) -> None:
        w, _ = self._pane(which)
        span = w.yview()[1] - w.yview()[0]
        top = max(0.0, min(top, 1.0 - span)) if span < 1 else 0.0
        cur = w.yview()[0]
        if abs(cur - top) < 1e-4:
            self._scroll_anim[which] = None
            return
        self._scroll_anim[which] = (cur, top, time.monotonic())

    def _step_scroll_anims(self) -> None:
        now = time.monotonic()
        for which, anim in self._scroll_anim.items():
            if anim is None:
                continue
            a, b, t0 = anim
            p = (now - t0) / ANIM
            w, _ = self._pane(which)
            self._prog_scroll[which] = True
            try:
                w.yview_moveto(b if p >= 1 else a + (b - a) * ease_out(p))
            finally:
                self._prog_scroll[which] = False
            if p >= 1:
                self._scroll_anim[which] = None

    def _line_top_fraction(self, which: str, idx: int, center: bool) -> float:
        w, _ = self._pane(which)
        n = max(1, int(w.index("end-1c").split(".")[0]))
        span = w.yview()[1] - w.yview()[0]
        frac = idx / n
        return frac - (span / 2 - 0.5 / n) if center else frac

    def _on_lyrics_scroll(self, which: str) -> None:
        """User scrolled one pane: bring the other to the same point in the song."""
        if self._prog_scroll[which] or self._scroll_anim[which] is not None:
            return
        other = "new" if which == "cur" else "cur"
        w, lines = self._pane(which)
        ow, olines = self._pane(other)
        if not lines or not olines:
            return
        top = int(w.index("@0,0").split(".")[0]) - 1
        t = lines[min(top, len(lines) - 1)][0] if lines else None
        if t is None or olines[0][0] is None:
            self._scroll_to(other, w.yview()[0])
            return
        j = 0
        for k, (ot, _) in enumerate(olines):
            if ot is not None and ot <= t + 1e-6:
                j = k
        self._scroll_to(other, self._line_top_fraction(other, j, center=False))

    def _highlight(self, which, widget, lines, pos) -> None:
        i = lrcmod.current_index(lines, pos)
        if i == self._now[which]:
            return
        self._now[which] = i
        widget.tag_remove("now", "1.0", "end")
        if i >= 0:
            widget.tag_add("now", f"{i + 1}.0", f"{i + 1}.end")
            self._scroll_to(which, self._line_top_fraction(which, i, center=True))

    def _ov_highlight(self, which, segs, pos, cols, on) -> None:
        i = lrcmod.segment_at(segs, pos)
        prev = self._ov_active[which]
        if i == prev:
            return
        items = self._ov_lane_items[which]
        if 0 <= prev < len(items):
            self.ov.itemconfig(items[prev], fill=cols[prev % 2])
        if 0 <= i < len(items):
            self.ov.itemconfig(items[i], fill=on)
        self._ov_active[which] = i

    # ============================================================= actions
    def _apply(self) -> None:
        """Record the chosen lyrics for this file; the bottom bar writes them."""
        if self.song is None or self.cand is None:
            return
        c = self.cand
        text = self.new_text if c.text is not None else None
        label = c.label
        if c.synced and (abs(self.offset) > 1e-6 or abs(self.factor - 1) > 1e-9 or self.pins):
            label += f" (offset {self.offset * 1000:+.0f} ms, stretch {self.factor * 100:.2f}%"
            label += (f", interpolated from {len(self.pins)} pins)" if self.interpolate
                      else f", {len(self.pins)} pinned)" if self.pins else ")")
        self._run_action(lambda s: review.choose(self.song.path, text, label, s, c.path), "chosen")

    def _lock(self) -> None:
        if self.song is not None:
            self._run_action(lambda s: review.lock_current(self.song.path, s), "locked")

    def _clear(self) -> None:
        if self.song is not None:
            self._run_action(lambda s: review.clear_choice(self.song.path, s), "cleared")

    def _run_action(self, fn, status) -> None:
        path, s = self.song.path, self.app.settings

        def work():
            try:
                msg = fn(s)
                st = State(s.resolved_state_db())
                try:
                    choice = st.current_choice(path)
                finally:
                    st.close()
                self.app.events.put({"type": "review_done", "path": path, "status": status,
                                     "msg": msg or "", "choice": choice})
            except Exception as e:
                self.app.events.put({"type": "review_done", "path": path,
                                     "err": f"{type(e).__name__}: {e}"})
        threading.Thread(target=work, daemon=True).start()

    def _search_lrclib(self) -> None:
        if self.song is None:
            return
        if not self.song.meta.title:
            messagebox.showinfo(APP_TITLE, "This file has no title tag to search for.")
            return
        token, meta, dur, s = self._token, self.song.meta, self._duration(), self.app.settings

        def work():
            try:
                cands = review.search_lrclib(meta, dur, s)
                self.app.events.put({"type": "review_lrclib", "token": token, "cands": cands})
            except Exception as e:
                self.app.events.put({"type": "review_lrclib", "token": token, "cands": [],
                                     "err": f"{type(e).__name__}: {e}"})
        threading.Thread(target=work, daemon=True).start()

    def shutdown(self) -> None:
        self.player.stop()
        self.tooltip.hide()
