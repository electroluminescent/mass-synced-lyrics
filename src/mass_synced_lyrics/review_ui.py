"""Review tab: results list, in-app player with waveforms, checkpoints, lyric
comparison, timing adjustment and per-file choices."""
from __future__ import annotations

import os
import re
import sys
import threading
from dataclasses import dataclass, field
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import APP_TITLE, theme
from . import fit as fitmod
from . import lrc as lrcmod
from . import review
from .config import save_settings, snap5
from .engine import STATUS_LABELS, display_path
from .paths import open_folder, reveal_in_explorer, state_key
from .player import Player, decode, envelope, volume_gain
from .state import KEEP, State

FILTERS = {
    "All": None,
    "Needs attention": {"ambiguous", "not_found", "no_synced", "no_metadata", "error", "rejected", "unsupported"},
    "Found (to apply)": {"proposed", "chosen", "locked", "aligned"},
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

GPU = "⚡"                 # marks everything done on the NVIDIA GPU (aligning by ear)
HAND = "✎"                # marks timing or lyrics you set yourself in the player

SHORT_STATUS = {
    "proposed": "Found", "embedded": "Saved", "lrc_written": ".lrc saved", "already_embedded": "Up to date",
    "has_synced": "Has lyrics", "not_found": "Not found", "no_synced": "Plain only online",
    "instrumental": "Instrumental", "no_metadata": "No tags", "ambiguous": "Ambiguous", "error": "Error",
    "unsupported": "Unsupported", "locked": "Your choice", "chosen": "Your choice", "unchanged": "Unchanged",
    "rejected": "Rejected", "restored": "Restored", "aligned": GPU + " Aligned", "searching": "Looking up…", "": "",
}
CHOICE_TEXT = {"local": "Local", "online": "Online", "keep": "Don't change", "custom": "Custom",
               "remove": "Remove lyrics", "": ""}
DONE_STATUSES = {"embedded", "lrc_written", "already_embedded", "unchanged"}
SAVED_STATUSES = {"embedded", "lrc_written"}          # this app wrote the lyrics
PROBLEM_STATUSES = {"error", "ambiguous", "unsupported", "rejected"}


LEGEND_HINTS = {
    "c_found": "Will write the lyrics as found, with their found timing.",
    "c_aligned": "Will write the lyrics with the GPU's timing (the Aligned timeline).",
    "c_edited": "Will write your Edited timeline (times or lines you changed).",
    "c_saved": "Saved: this app wrote the lyrics.",
    "c_check": "Check it: the GPU heard too little to be sure, or its timing is far from the found one.",
    "c_local": "Apply will use local lyrics: a sidecar .lrc, your collection, or embedded lyrics (.lrc mode).",
    "c_online": "Apply will use lyrics found online (LRCLIB).",
    "c_custom": "Apply will use lyrics you chose or re-timed in the player.",
    "c_keep": "Not changed: nothing will be written (kept as it is, or nothing found).",
    "c_done": "Saved: the lyrics are written (or the song already had them).",
    "c_problem": "Needs a look: error, ambiguous .lrc match, or unsupported file.",
}
COLUMN_HINTS = {
    "#0": "Songs by Artist ▸ Album or by folder (switch with the box above). Ctrl/Shift+click selects several "
          "songs; a group row counts as all its songs. Right-click for Save now, sources, Reset, Restore original, "
          "opening the file's location and Remove from the list (also the Delete key).",
    "status": "What happened to the song so far.",
    "local": "Lyrics found locally: a sidecar .lrc beside the song, your collection, or embedded lyrics.",
    "online": "Result of the LRCLIB lookup: found, plain lyrics only, instrumental, or not found.",
    "choice": "What Apply will do. Click a Choice cell to cycle Local → Online → Don't change. On an artist/album "
          "row or a multi-selection it changes all of them.",
    "match": "How sure it is that the lyrics in Choice belong to this song (100% = exact title, artist, album "
          "and length). Click the header to filter by it.",
    "fit": "How well the lyrics in Choice fit this recording: its length, lines past the end, how the lines "
          "spread over the song and, once the audio has been checked, lines that start on silence. "
          "Click the header to filter by it.",
    "find": "Find lyrics: ⟳ being looked up now · ✓ done · ✗ error.",
    "align": "Alignment. ⚡ = on the GPU: ⟳ 42% aligning now · ⚡✓ re-timed · ⚡≈ partly: fewer words heard than "
             "Aligned lyrics must match, so only the lines it was sure of were placed (check it) · "
             "⚡– heard too little, kept as found · "
             "⚡✗ error. ✎ = timing you set yourself in the player (offset, stretch, pins or edited times). "
             "Empty = not aligned.",
    "save": "Saving (Apply / Save now): ⟳ writing now · ✓ written · – already up to date · ✗ error.",
}
COLUMNS = (("status", 70, "Status"), ("find", 40, "Find"), ("align", 60, "Align"), ("save", 40, "Save"),
           ("choice", 96, "Choice ⟳"), ("match", 56, "Match"), ("fit", 48, "Fit"),
           ("local", 58, "Local"), ("online", 66, "Online"))
OPS = ("find", "align", "save")
OP_MARK = {"busy": "⟳", "done": "✓", "skip": "–", "fail": "✗", "": ""}
ALIGN_MARK = {"busy": "⟳", "done": GPU + "✓", "partial": GPU + "≈", "skip": GPU + "–", "fail": GPU + "✗", "": ""}

VIEWS = {"artist": "Artist ▸ Album", "folder": "Folders"}
VIEW_HEADINGS = {"artist": "Artist ▸ Album ▸ Song", "folder": "Folder ▸ Song"}

ROW_KEYS = ("status", "source", "artist", "title", "local", "online", "choice", "album", "conf_local",
            "conf_online", "fit_local", "fit_online", "op_find", "op_align", "op_save", "hand", "level", "check")
SCORE_COLS = {"match": ("Match", "list_min_match"), "fit": ("Fit", "list_min_fit")}
FOUND_STATUSES = {"proposed", "chosen", "locked", "aligned"}


RULER_H = 16
# Timelines (lanes) under the waveforms, top to bottom: Current, Found ("new"), Aligned (what the GPU
# placed), Edited (what will be written: the only one you can change; drag bars down into it).
LANES = ("cur", "new", "aligned", "edit")
LANE_LABEL = {"cur": "Current", "new": "Found", "aligned": GPU + " Aligned", "edit": HAND + " Edited"}
OV_LANE_Y = {"cur": 44, "new": 56, "aligned": 68, "edit": 80}       # overview lanes (10 px)
OV_LANE_H = 10
OV_EDIT_Y, OV_RULER_Y = 80, 92
SC_CUR_Y, SC_NEW_Y = 64, 83              # scrolling waveform lanes (17 px each)
SC_ALIGN_Y, SC_EDIT_Y, SC_RULER_Y = 102, 121, 140
SC_LANE_Y = {"cur": SC_CUR_Y, "new": SC_NEW_Y, "aligned": SC_ALIGN_Y, "edit": SC_EDIT_Y}
SC_LANE_H = 17
LABEL_W = 70                             # the lane names, left of the timelines
EDIT_A, EDIT_B, EDIT_ON = "#3b67a3", "#4573b0", "#bcd8ff"   # edited lane (blue): lines that moved along
EDIT_AI = "#5468cf"                      # edited lane: lines taken from the aligned timeline
EDIT_USER = "#7db7ff"                    # edited lane: times you set yourself (light blue)
SEL_OUTLINE = "#ffffff"
GHOST = "#bcd8ff"
EDGE_PX = 6                              # how close to a lyric's edge a press resizes it
HEARD_LO, HEARD_HI = "#3d2a55", "#d58cff"   # heard words: low → high confidence
RULER_FG, RULER_TICK = "#9aa0a6", "#5f6368"


def _ruler_step(duration: float, width: float, min_px: float = 70.0) -> float:
    """A 'nice' label interval so labels are at least min_px apart."""
    for step in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
        if duration <= 0 or step / duration * width >= min_px:
            return float(step)
    return 3600.0


def _draw_ruler(c: tk.Canvas, start: float, end: float, to_x, y0: int, width: float,
                step: float | None = None) -> None:
    """Time ruler at y0: a baseline, labelled ticks every `step` s and small ticks in between."""
    if end <= start:
        return
    step = step or _ruler_step(end - start, width)
    minor = step / 5 if step >= 5 else (step / 2 if step > 1 else 0.5)
    c.create_line(to_x(start), y0, to_x(end), y0, fill=RULER_TICK, tags="ruler")
    t = start
    k = 0
    while t <= end + 1e-6:
        x = to_x(t)
        major = abs((t / step) - round(t / step)) < 1e-6
        c.create_line(x, y0, x, y0 + (6 if major else 3), fill=RULER_TICK, tags="ruler")
        if major:
            c.create_text(x + 2, y0 + 3, text=fmt_clock(t), anchor="nw", fill=RULER_FG,
                          font=("TkDefaultFont", 7), tags="ruler")
        k += 1
        t = start + k * minor


def fmt_clock(t: float) -> str:
    t = int(round(max(0.0, t)))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


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


def heard_color(p: float) -> str:
    """Colour of a heard word: dim violet (unsure) → bright violet (sure)."""
    p = max(0.0, min(1.0, float(p or 0.0)))
    lo, hi = _hex_rgb(HEARD_LO), _hex_rgb(HEARD_HI)
    return "#%02x%02x%02x" % tuple(int(a + (b - a) * p) for a, b in zip(lo, hi))


def _fmt_left(sec: float) -> str:
    sec = int(round(max(0.0, sec)))
    return f"{sec // 60}:{sec % 60:02d}" if sec >= 60 else f"{sec} s"


@dataclass
class Bar:
    """One lyric line in a timeline: when, what, and which lyric (key = its found time) it is."""
    start: float
    end: float
    text: str
    key: str | None
    row: int | None = None               # its row in the Edited list (edit lane only)
    words: list = field(default_factory=list)
    fill: str = ""


def resize_lyric(times: list, texts: list, idx: int, edge: str, t: float, duration: float) -> dict:
    """New times after dragging the start or end of lyric `idx` to `t`. Rows are the New list's
    lines in time order; empty rows are gaps. Returns {row: new time}; ("end", idx): time for a new
    gap right after the lyric; ("stop", idx): the first row left untouched after a push.
    - Start: moves only this lyric (between the previous line and its own end).
    - End, when a gap follows: the gap's start moves; if the gap is used up, the lyrics after it are
      pushed later (ripple) until the next gap absorbs the rest.
    - End, when the next lyric follows directly: later makes the following lyrics ripple later until
      a gap absorbs it; earlier opens a new gap instead of moving the next lyric."""
    n = len(times)
    out: dict = {}

    def gap(i):
        return not (texts[i] or "").strip()
    if edge == "start":
        lo = (times[idx - 1] + 0.05) if idx > 0 else 0.0
        hi = (times[idx + 1] - 0.1) if idx + 1 < n else duration - 0.1
        out[idx] = round(max(lo, min(t, hi)), 2)
        return out
    e = max(times[idx] + 0.1, min(t, duration))
    nxt = idx + 1
    if nxt >= n:                                           # the last line: give it an end
        if e < duration - 0.05:
            out[("end", idx)] = round(e, 2)
        return out

    def push(j, shift):
        m = j
        while m < n and shift > 0.005:
            if gap(m):
                new = times[m] + shift
                after = m + 1
                limit = (times[after] - 0.05) if after < n else duration
                out[m] = round(min(new, duration - 0.01), 2)
                if new <= limit:
                    out[("stop", idx)] = after if after < n else None
                    return
                shift = new - times[after] + 0.01
                m = after
                continue
            out[m] = round(min(times[m] + shift, duration - 0.05), 2)
            m += 1
        if m < n:
            out[("stop", idx)] = m

    if gap(nxt):
        after = nxt + 1
        limit = (times[after] - 0.05) if after < n else duration
        if e <= limit:
            out[nxt] = round(max(times[idx] + 0.1, e), 2)
        else:
            out[nxt] = round(e, 2)
            push(after, e - times[after] + 0.01)
    elif e < times[nxt] - 0.05:
        out[("end", idx)] = round(e, 2)                    # shorter: open a gap, the next lyric stays
    elif e > times[nxt]:
        push(nxt, e - times[nxt])
    return out


def _diff_as_edits(orig: str, aligned: str) -> dict:
    """Older alignments kept only the result: express it as fixed times (pins) for the lines that
    moved, plus end marks for the empty lines it added, on top of the original timing."""
    o = [(t, b) for t, b in lrcmod.display_lines(orig) if t is not None]
    a = [(t, b) for t, b in lrcmod.display_lines(aligned) if t is not None]
    pins, ends = {}, {}
    j = 0
    last_lyric = None
    for t, b in a:
        if j < len(o) and (b.strip() == o[j][1].strip()):
            if abs(t - o[j][0]) > 0.005:
                pins[lrcmod.pin_key(o[j][0])] = round(t, 2)
            if b.strip():
                last_lyric = o[j][0]
            j += 1
        elif not b.strip() and last_lyric is not None:     # an empty line the aligner added
            ends[lrcmod.pin_key(last_lyric)] = round(t, 2)
    return {"pins": pins, "ends": ends, "mode": "fixed", "confs": {}}


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


def _blank_row() -> dict:
    row = {k: (None if k.startswith(("conf_", "fit_")) else "") for k in ROW_KEYS}
    row["hand"] = False                       # re-timed by you in the player (✎ in the Align column)
    row["check"] = False                      # flagged: look at it before saving
    return row


def _busy(r: dict) -> bool:
    return any(r.get("op_" + op) == "busy" for op in OPS)


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
        self.ai_keys: set[str] = set()            # pins placed by the GPU aligner
        self.end_marks: dict[str, float] = {}     # lyric key -> time it ends early (a gap after it)
        self.ai_ends: set[str] = set()            # ... of those, the ones the aligner set
        self.ai_conf: dict[str, float] = {}       # lyric key -> 0-1 how surely the aligner heard it
        self._marker_rows: dict[int, str] = {}    # New-list row of an end mark -> its lyric key
        self.orig_segs: list = []                 # the found lyrics as found (lane 2)
        self.lane_bars: dict[str, list] = {k: [] for k in LANES}
        self.sel_lane: str | None = None          # the timeline bars are selected in (one at a time)
        self.sel_keys: set[str] = set()           # the selected lyrics (keys)
        self._bar_drag: dict | None = None        # dragging selected bars (down into Edited, or along it)
        self._band: dict | None = None            # drawing a selection range along a timeline
        self.found_text: str | None = None        # the found lyrics before any line was added or deleted
        self.aligned_text: str | None = None      # the aligned timeline for this candidate (GPU result)
        self.aligned_verify: float | None = None
        self.aligned_pins: dict = {}
        self.aligned_ends: dict = {}
        self.aligned_conf: dict = {}
        self.aligned_mode = "slope"
        self.aligned_by_key: dict[str, float] = {}
        self.cand_scores: dict = {}
        self._auto_job = None
        self._resize = None                       # an edge being dragged in the timeline
        self.warp_mode = "slope"                  # how unpinned lines follow (ripple when Ripple changes is on)
        self.heard_by_cand: dict[str, list] = {}  # candidate key -> words the aligner heard
        self._lanes_ver = 0                       # bumped whenever a lane's contents change
        self._align_req = None                    # (song path, candidate key, n) of a pending GPU request
        self._editor = None
        self.orig_lines: list = []
        self.new_text: str | None = None
        self.cur_lines: list = []
        self.new_lines: list = []
        self.cur_segs: list = []
        self.new_segs: list = []
        self.checkpoints: list[float | None] = []   # numbered by when they were made (None = deleted slot)
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
        self._no_click_focus(self)
        self.after(TICK_MS, self._tick)

    @staticmethod
    def _no_click_focus(w) -> None:
        """Buttons and switches here don't keep the keyboard focus when clicked: Space and the number
        keys belong to the player (Space used to press the last-clicked button again, e.g. Auto-align)."""
        for child in w.winfo_children():
            if isinstance(child, (ttk.Button, ttk.Checkbutton, ttk.Radiobutton, ttk.Menubutton,
                                  tk.Button, tk.Checkbutton)):
                try:
                    child.configure(takefocus=0)
                except tk.TclError:
                    pass
            ReviewTab._no_click_focus(child)

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
        self.v_view = tk.StringVar(value=VIEWS.get(getattr(self.app.settings, "list_view", "artist"),
                                                   VIEWS["artist"]))
        vcmb = theme.hint(ttk.Combobox(top, textvariable=self.v_view, values=list(VIEWS.values()), state="readonly",
                                       width=14),
                          "Arrange the list by Artist ▸ Album (from the tags) or by the folders on disk.")
        vcmb.pack(side="left", padx=(0, 6))
        vcmb.bind("<<ComboboxSelected>>", lambda e: self._view_changed())
        self.v_filter = tk.StringVar(value="All")
        cmb = theme.hint(ttk.Combobox(top, textvariable=self.v_filter, values=list(FILTERS), state="readonly", width=16), 'Show only some songs: needing a look, found, changed, your choices…')
        cmb.pack(side="left")
        cmb.bind("<<ComboboxSelected>>", lambda e: self._refill())
        self.v_search = tk.StringVar()
        ent = theme.hint(ttk.Entry(top, textvariable=self.v_search), 'Search by artist, album, title or path.')
        ent.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.v_search.trace_add("write", lambda *a: self._search_changed())
        legend = ttk.Frame(left)
        legend.pack(fill="x", pady=(4, 0))
        for tag, text in (("c_found", "Found"), ("c_aligned", GPU + " Aligned"), ("c_edited", HAND + " Edited"),
                          ("c_keep", "Not changed"), ("c_saved", "Saved"), ("c_check", "Check"),
                          ("c_problem", "Problem")):
            pill = theme.register(tk.Label(legend, text=text), "pill", tag=tag)
            theme.hint(pill, LEGEND_HINTS[tag]).pack(side="left", padx=(0, 4))
        flt = ttk.Frame(left)
        flt.pack(fill="x", pady=(4, 0))
        st0 = self.app.settings
        self.v_only_found = tk.BooleanVar(value=bool(getattr(st0, "only_found", True)))
        self.v_hide_saved = tk.BooleanVar(value=bool(getattr(st0, "hide_saved", False)))
        self.v_hide_low = tk.BooleanVar(value=bool(getattr(st0, "hide_low_confidence", False)))
        for text, var, key, tip in (
                ("Lyrics found", self.v_only_found, "",
                 "Show only songs with lyrics found that can be written (local or online). The default for "
                 "each run is set on the Find lyrics tab."),
                ("Hide saved", self.v_hide_saved, "hide_saved",
                 "Hide songs whose lyrics are already saved or already up to date."),
                ("Hide low confidence", self.v_hide_low, "hide_low_confidence",
                 "Hide songs whose found lyrics are below the High confidence or Good fit level set on the "
                 "Find lyrics tab.")):
            theme.hint(ttk.Checkbutton(flt, text=text, variable=var,
                                       command=lambda k=key, v=var: self._filter_toggled(k, v)), tip).pack(
                side="left", padx=(0, 10))
        tf = ttk.Frame(left)
        tf.pack(fill="both", expand=True, pady=4)
        cols = tuple(c for c, _, _ in COLUMNS)
        self.min_score = {k: snap5(getattr(st0, attr, 0) or 0) for k, (_, attr) in SCORE_COLS.items()}
        self._score_popup = None
        self.tree = ttk.Treeview(tf, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text=VIEW_HEADINGS[self._view()])
        self.tree.tag_configure("viewing", font=theme.font("strong"))   # the song in the player (bold, ▶)
        self.tree.tag_configure("busy")             # created first so it outranks the row colours
        self.group_dirs: dict[str, str] = {}      # group row → its folder on disk
        # Group-row totals are kept per group and refreshed once per batch of changes (bottom-up), and the
        # "shown" count is tracked incrementally, so adding a song costs the same with 50 or 50,000 listed.
        self._group_stats: dict[str, tuple] = {}
        self._dirty_groups: set[str] = set()
        self._flush_pending = False
        self._shown: set[str] = set()
        self._search_job = None
        self.tree.column("#0", width=180, minwidth=140, stretch=True)
        for col, w, label in COLUMNS:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=w, minwidth=34, stretch=False, anchor="center" if col in OPS else "w")
        self._apply_columns(getattr(self.app.settings, "list_columns", None) or [c for c, _, _ in COLUMNS],
                            save=False)
        for col in SCORE_COLS:
            self.tree.heading(col, command=lambda c=col: self._score_filter_popup(c))
            self._score_heading(col)
        theme.register(self, "callback", fn=self._restyle)
        self.tree.bind("<Button-1>", self._tree_click, add="+")
        self.tree.bind("<Button-3>", self._tree_menu)
        self.tree.bind("<Delete>", self._delete_key)
        self._head_tip = Tooltip(self.tree)
        self.tree.bind("<Motion>", self._tree_motion, add="+")
        self.tree.bind("<Leave>", lambda e: self._head_tip.hide(), add="+")
        if sys.platform == "darwin":
            self.tree.bind("<Button-2>", self._tree_menu)
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tf, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sb.set, xscrollcommand=hsb.set)
        hsb.pack(side="bottom", fill="x")          # for narrow lists: the columns stay readable
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        bf = ttk.Frame(left)
        bf.pack(fill="x")
        theme.hint(ttk.Button(bf, text="Load earlier results", command=self.load_earlier), 'Add every song from earlier runs to the list.').pack(side="left")
        theme.hint(ttk.Button(bf, text="Open file…", command=self._open_file), 'Open any song in the player.').pack(side="left", padx=6)
        cb = ttk.Menubutton(bf, text="Columns")
        cb.bind("<Button-1>", lambda e: cb.configure(menu=self._columns_menu()), add="+")
        cb.configure(menu=self._columns_menu())
        theme.hint(cb, "Show or hide columns (also: right-click a column header).").pack(side="left")
        bf2 = ttk.Frame(left)
        bf2.pack(fill="x", pady=(6, 0))
        mb = ttk.Menubutton(bf2, text=GPU + " Align (GPU)")
        menu = theme.register(tk.Menu(mb, tearoff=0), "menu")
        menu.add_command(label=GPU + " Align low-confidence songs in the list",
                         command=lambda: self.app.align_command("low"))
        menu.add_command(label=GPU + " Align selected songs", command=lambda: self.app.align_command("selected"))
        menu.add_command(label=GPU + " Align all songs in the list", command=lambda: self.app.align_command("all"))
        mb["menu"] = menu
        theme.hint(mb, "Align lyrics to the vocals on the NVIDIA GPU: the low-confidence songs in this list, or the "
                       "selected songs. Songs already aligned are skipped. Progress shows at the top of the "
                       "window.").pack(side="left")
        theme.hint(ttk.Button(bf2, text="Clear cached data",
                              command=lambda: self.app.clear_cache_command() if hasattr(self.app, "clear_cache_command")
                              else None),
                   "Forget what was worked out for the selected songs (online lookup, what the GPU heard, Find's "
                   "record) so the next Find / Align does them again. A GPU alignment is undone. Your choices, "
                   "edits and backups stay; no files are changed. Also: right-click.").pack(side="left", padx=6)
        self.v_count = tk.StringVar(value="0 files")
        ttk.Label(bf2, textvariable=self.v_count).pack(side="right")

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
                                   style="Accent.TButton" if theme._state["sv"] else "TButton"), 'Play / pause (Space).\nSpace: play / pause · keys 1–9: play checkpoint N (an unset number is set at the needle) · 0: next checkpoint by number · ` or ~: next checkpoint in the song after the needle · Up/Down or W/S: previous / next song\nClick a waveform to jump, drag it to scrub · Ctrl+click the overview to add or remove a checkpoint')
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
        self.lbl_volume.bind("<Button-1>", lambda e: self._edit_volume(e))
        self.sld_volume.bind("<ButtonRelease-1>", lambda e: self._save_globals())
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.sld_volume.bind(seq, self._volume_wheel)
        self.player.volume = volume_gain(self.v_volume.get())
        self.v_audio_note = tk.StringVar()
        ttk.Label(right, textvariable=self.v_audio_note, style="Caption.TLabel").grid(row=r, column=0, sticky="w"); r += 1

        ovf = ttk.Frame(right)
        ovf.grid(row=r, column=0, sticky="ew"); r += 1
        self.ov_lbl = tk.Canvas(ovf, width=LABEL_W, height=OV_RULER_Y + RULER_H, background=BG,
                                highlightthickness=0)
        self.ov_lbl.pack(side="left")
        self.ov = tk.Canvas(ovf, height=OV_RULER_Y + RULER_H, background=BG, highlightthickness=0, cursor="hand2")
        self.ov.pack(side="left", fill="x", expand=True)
        cpf = ttk.Frame(right)
        cpf.grid(row=r, column=0, sticky="ew"); r += 1
        ttk.Frame(cpf, width=LABEL_W).pack(side="left")
        self.cpbar = theme.register(tk.Canvas(cpf, height=28, highlightthickness=0), "bg")
        self.cpbar.pack(side="left", fill="x", expand=True)
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
        scf = ttk.Frame(right)
        scf.grid(row=r, column=0, sticky="ew"); r += 1
        self.sc_lbl = tk.Canvas(scf, width=LABEL_W, height=SC_RULER_Y + RULER_H, background=BG,
                                highlightthickness=0)
        self.sc_lbl.pack(side="left")
        self.sc = tk.Canvas(scf, height=SC_RULER_Y + RULER_H, background=BG, highlightthickness=0,
                            cursor="sb_h_double_arrow")
        self.sc.pack(side="left", fill="x", expand=True)

        tcard = theme.card(right, padding=(12, 8))
        tcard.grid(row=r, column=0, sticky="ew", pady=(8, 4)); r += 1
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
        theme.hint(ttk.Button(tm2, text=HAND + " Line to playhead", command=self._align), "Align the selected line to the playhead: click a line in New, play to where it's actually sung, then press. One aligned line sets the offset, two set offset and stretch.").pack(side="left", padx=(0, 6))
        self.btn_interp = theme.flat_button(tm2, "Interpolate", self._toggle_interpolate, padx=10)
        theme.hint(self.btn_interp, "Re-time every line you haven't pinned or edited by stretching the original "
                                    "timing between your pins. Updates live as you add pins; click again to turn off.")
        self.btn_interp.pack(side="left")
        theme.hint(ttk.Button(tm2, text="Clear pins", command=self.clear_pins), 'Remove all pins (edited times stay).').pack(side="left", padx=(6, 0))
        tm3 = ttk.Frame(tcard, style=cs)
        tm3.pack(fill="x", pady=(8, 0))
        theme.hint(ttk.Button(tm3, text="⇣ Found → Edited", command=lambda: self.transfer_all("new")),
                   "Start Edited over from the Found timeline: clears offset, stretch, pins and every edited "
                   "time. To bring only some lyrics: select them in Found and drag them down onto Edited."
                   ).pack(side="left")
        self.btn_from_aligned = theme.hint(
            ttk.Button(tm3, text="⇣ Aligned → Edited", command=lambda: self.transfer_all("aligned")),
            "Bring every lyric down from the Aligned timeline into Edited (the GPU's timing). Times you set "
            "yourself stay. To bring only some: select them in the Aligned timeline (click, Ctrl+click, or drag "
            "along it) and drag them down onto Edited.")
        self.btn_from_aligned.pack(side="left", padx=(4, 0))
        self.v_pins = tk.StringVar()
        self.btn_ai = theme.hint(ttk.Button(tm3, text=GPU + " Auto-align (GPU)", command=self.auto_align),
                                 "Locate these lyrics in this song's vocals on the NVIDIA GPU (⚡). If enough lyric words "
                                 "are heard being sung (Aligned lyrics must match, Find lyrics tab), the lines it placed "
                                 "get those times and show in the Edited lane (purple, brighter = more sure); the others "
                                 "follow. Progress shows here and at the top. The result is the Aligned timeline; "
                                 "⇣ Found → Edited goes back to the found timing.")
        self.btn_ai.pack(side="left", padx=(6, 0))
        self.v_ripple = tk.BooleanVar(value=bool(getattr(self.app.settings, "ripple", False)))
        theme.hint(ttk.Checkbutton(tm2, text="Ripple changes", variable=self.v_ripple, command=self._ripple_toggled,
                                   style="Switch.TCheckbutton" if theme._state["sv"] else "TCheckbutton"),
                   "When a time is edited (by you or the aligner), move the lines around it too: lines between "
                   "two pinned or edited lines are stretched to fit, and lines before the first / after the last "
                   "move by the same amount, squeezed so they stay within the song.").pack(side="left", padx=(6, 0))
        ttk.Label(tm2, textvariable=self.v_pins, style="Muted.TLabel").pack(side="left", padx=8)
        self.v_ai_note = tk.StringVar()
        note = ttk.Label(tcard, textvariable=self.v_ai_note, style="Muted.TLabel", wraplength=700, justify="left")

        def show_note(*_):                       # only takes room when there is something to say
            if self.v_ai_note.get():
                note.pack(anchor="w", pady=(6, 0))
            else:
                note.pack_forget()
        self.v_ai_note.trace_add("write", show_note)
        self.spn_offset = None
        self.v_anchors = tk.StringVar(value="")
        ttk.Label(tm, textvariable=self.v_anchors, style="Muted.TLabel").pack(side="left", padx=6)

        self.v_offset.trace_add("write", lambda *a: self._timing_changed())
        self.v_stretch.trace_add("write", lambda *a: self._timing_changed())

        right.rowconfigure(r, weight=1)
        lp = theme.paned(right)
        lp.grid(row=r, column=0, sticky="nsew"); r += 1
        lf = theme.card(lp, padding=(10, 8))
        cs_ = "Card.TFrame" if theme._state["sv"] else "TFrame"
        lh = ttk.Frame(lf, style=cs_)
        lh.pack(fill="x", pady=(0, 6))
        self.lbl_cur = ttk.Label(lh, text="Current (embedded)", style="Section.TLabel")
        self.lbl_cur.pack(side="left")
        theme.hint(ttk.Button(lh, text="◂ Hide", command=lambda: self.set_current_shown(False), width=7),
                   "Hide the current lyrics to give the new ones the whole width.").pack(side="right")
        nf = theme.card(lp, padding=(10, 8))
        nh = ttk.Frame(nf, style=cs_)
        nh.pack(fill="x", pady=(0, 6))
        self.btn_show_cur = theme.hint(ttk.Button(nh, text="▸ Current", command=lambda: self.set_current_shown(True),
                                                  width=10),
                                       "Show the song's current lyrics beside the new ones.")
        ttk.Label(nh, text=HAND + " Edited", style="Section.TLabel").pack(side="left", padx=(0, 8))
        self._new_head = nh
        lp.add(lf, minsize=180, stretch="always")
        lp.add(nf, minsize=180, stretch="always")
        self._lf, self._nf = lf, nf
        self._lyrics_paned = lp
        lp.bind("<Map>", lambda e: self.after(30, self._even_lyrics_split), add="+")
        self.cmb_cand = theme.hint(ttk.Combobox(nh, state="readonly"), 'Lyrics to compare and choose from: what Find found, sidecars, collection, LRCLIB, the current lyrics (to re-time), the originals, or none.')
        self.cmb_cand.pack(side="left", fill="x", expand=True)
        self.v_fit = tk.StringVar()
        fit_lbl = theme.hint(ttk.Label(nf, textvariable=self.v_fit, style="Caption.TLabel", wraplength=420,
                                       justify="left"),
                             "How well these lyrics fit this recording (length, lines past the end, spread over the "
                             "song, lines starting on silence).")

        def show_fit(*_):                        # only when something lowered the fit
            if self.v_fit.get():
                fit_lbl.pack(fill="x", pady=(0, 4), after=nh)
            else:
                fit_lbl.pack_forget()
        self.v_fit.trace_add("write", show_fit)
        self.cmb_cand.bind("<<ComboboxSelected>>", lambda e: (self._select_candidate(self.cmb_cand.current()),
                                                              self._grab_keys()))
        self.btn_keep = theme.flat_button(lf, "Keep current", self._lock, pady=8)
        theme.hint(self.btn_keep, "Leave this song's lyrics as they are (Apply skips it, later runs too).")
        self.btn_keep.pack(side="bottom", fill="x", pady=(8, 0))
        # What to write for this song: one of four levels (the most worked-on one is chosen by default)
        self._choice_row = ttk.Frame(nf, style=cs_)
        self._choice_row.pack(side="bottom", fill="x", pady=(8, 0))
        self.btn_c_keep = theme.flat_button(nf, "Keep current", self._lock, pady=7, padx=8)
        theme.hint(self.btn_c_keep, "Leave this song's lyrics as they are (Apply skips it, later runs too).")
        self.btn_c_found = theme.flat_button(nf, "Use found", lambda: self._use_level("found"), pady=7, padx=8)
        theme.hint(self.btn_c_found, "Write the lyrics as found, with their found timing (the Found timeline). "
                                     "The % is how well they match the song.")
        self.btn_c_aligned = theme.flat_button(nf, GPU + " Use aligned", lambda: self._use_level("aligned"),
                                               pady=7, padx=8)
        theme.hint(self.btn_c_aligned, "Write the lyrics with the GPU's timing (the Aligned timeline). The % is how "
                                       "many of the lyric's words were heard being sung.")
        self.btn_c_edited = theme.flat_button(nf, HAND + " Use edited", lambda: self._use_level("edited"),
                                              pady=7, padx=8)
        theme.hint(self.btn_c_edited, "Write the Edited timeline: everything you moved, stretched, typed or "
                                      "brought down from Found or Aligned. Chosen by itself once you change it.")
        for i, b in enumerate((self.btn_c_keep, self.btn_c_found, self.btn_c_aligned, self.btn_c_edited)):
            b.pack(in_=self._choice_row, side="left", fill="x", expand=True, padx=(0 if i == 0 else 4, 0))
            b.lift(self._choice_row)                   # created before the row: keep them drawn on top
        self.btn_apply, self.btn_keep2 = self.btn_c_edited, self.btn_c_keep   # older names
        self.txt_cur = self._lyrics_text(lf, "cur")
        self.txt_new = self._lyrics_text(nf, "new")
        self.set_current_shown(bool(getattr(self.app.settings, "show_current", False)), save=False)

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
        # default: ~40% of the window for the list (between 520 and 680 px), leaving the player its toolbar
        x = int(getattr(self.app.settings, "review_split", 0) or
                max(520, min(680, int(0.40 * max(self._paned.winfo_width(), self.winfo_toplevel().winfo_width())))))
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
        dark = theme.mode() == "dark"
        t.tag_configure("ai_edited", background="#4b2a63" if dark else "#ecdcf8",
                        foreground="#f0d9ff" if dark else "#4b1c66", font=theme.font("strong"))
        t.tag_configure("moved", foreground="#d9b44a" if dark else "#8a6a00")
        t.tag_raise("now")
        t.tag_raise("edited")
        t.tag_raise("ai_edited")

    def _restyle(self) -> None:
        """Re-apply palette colours (light/dark switch)."""
        p = theme.palette()
        for tag, (bg, fg) in p["rows"].items():
            if tag in ("group", "subgroup"):
                self.tree.tag_configure(tag, background=bg, foreground=fg,
                                        font=theme.font("strong") if tag == "group" else theme.font("body"))
            else:
                self.tree.tag_configure(tag, background=bg, foreground=fg)
        self.tree.tag_configure("busy", background="#2f4f7a" if theme.mode() == "dark" else "#cfe1ff",
                                foreground="#ffffff" if theme.mode() == "dark" else "#0b2a52")
        for t in (getattr(self, "txt_cur", None), getattr(self, "txt_new", None)):
            if t is not None:
                self._tag_lyrics(t)
        if hasattr(self, "btn_keep"):
            self._update_choice_buttons()
            self._style_interp()

    def set_current_shown(self, shown: bool, save: bool = True) -> None:
        """Show or collapse the Current lyrics pane (collapsed by default; remembered)."""
        lp = self._lyrics_paned
        panes = [str(x) for x in lp.panes()]
        if shown and str(self._lf) not in panes:
            lp.add(self._lf, before=self._nf, minsize=180, stretch="always")
            self._lyrics_split_done = False
            self.after(30, self._even_lyrics_split)
        elif not shown and str(self._lf) in panes:
            lp.forget(self._lf)
        if shown:
            self.btn_show_cur.pack_forget()
        else:
            self.btn_show_cur.pack(side="left", padx=(0, 8), before=self._new_head.winfo_children()[1])
        self.current_shown = shown
        if save and getattr(self.app.settings, "show_current", None) != shown:
            self.app.settings.show_current = shown
            try:
                save_settings(self.app.settings)
            except OSError:
                pass

    def _even_lyrics_split(self) -> None:
        if getattr(self, "_lyrics_split_done", False) or len(self._lyrics_paned.panes()) < 2:
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
                choice: str | None = None, album: str | None = None, conf_local: float | None = None,
                conf_online: float | None = None, fit_local: float | None = None,
                fit_online: float | None = None, ops: dict | None = None) -> None:
        """Add or update a song row; None keeps the current value."""
        path = state_key(path)
        row = self.rows.get(path) or _blank_row()
        for k, v in (("status", status), ("source", source), ("artist", artist), ("title", title),
                     ("local", local), ("online", online), ("choice", choice), ("album", album),
                     ("conf_local", conf_local), ("conf_online", conf_online),
                     ("fit_local", fit_local), ("fit_online", fit_online)):
            if v is not None:
                row[k] = v
        for op, v in (ops or {}).items():
            row["op_" + op] = v
        self.rows[path] = row
        if hasattr(self.app, "session_changed"):
            self.app.session_changed()
        if not getattr(self.app, "_apply_bar_shown", True):
            self.app.show_apply_bar()
        if self._matches(path):
            self._upsert(path)
        elif path in self._shown:
            parent = self.tree.parent(path)
            self.tree.delete(path)
            self._shown.discard(path)
            self._mark_dirty(parent)
        self._update_count()

    def _search_changed(self) -> None:
        """Typing in the search box: refill once the typing pauses."""
        if self._search_job is not None:
            try:
                self.after_cancel(self._search_job)
            except tk.TclError:
                pass
        self._search_job = self.after(250, self._search_now)

    def _search_now(self) -> None:
        self._search_job = None
        self._refill()

    # -- grouping
    def _group_keys(self, path: str) -> tuple[str, str]:
        r = self.rows[path]
        folder = os.path.dirname(display_path(path))
        artist = r["artist"] or os.path.basename(os.path.dirname(folder)) or "(no artist)"
        album = r.get("album") or os.path.basename(folder) or "(no album)"
        return artist, album

    def _view(self) -> str:
        return next((k for k, v in VIEWS.items() if v == self.v_view.get()), "artist")

    def _view_changed(self) -> None:
        view = self._view()
        self.tree.heading("#0", text=VIEW_HEADINGS[view])
        self.app.settings.list_view = view
        try:
            save_settings(self.app.settings)
        except OSError:
            pass
        self._group_target.clear()
        self._refill()

    def _folder_chain(self, path: str) -> list[tuple[str, str]]:
        """[(label, folder)] from the top folder shown down to the song's own folder."""
        folder = os.path.normpath(os.path.dirname(display_path(path)))
        root = os.path.normpath(self.app.settings.music_folder) if self.app.settings.music_folder else ""
        if root and os.path.normcase(folder + os.sep).startswith(os.path.normcase(root.rstrip("\\/") + os.sep)):
            chain = [(os.path.basename(root.rstrip("\\/")) or root, root)]
            cur = root
            rel = os.path.relpath(folder, root)
            for part in ([] if rel == "." else rel.split(os.sep)):
                cur = os.path.join(cur, part)
                chain.append((part, cur))
            return chain
        drive, rest = os.path.splitdrive(folder)
        anchor = drive + os.sep if drive or folder.startswith(os.sep) else ""
        chain = [(anchor or ".", anchor or ".")] if anchor else []
        cur = anchor
        for part in [x for x in rest.split(os.sep) if x]:
            cur = os.path.join(cur, part) if cur else part
            chain.append((part, cur))
        return chain or [(folder, folder)]

    def _ensure_groups(self, path: str) -> str:
        many = len(self.rows) > 600
        if self._view() == "folder":
            parent = ""
            for depth, (label, folder) in enumerate(self._folder_chain(path)):
                iid = "grp:dir:" + os.path.normcase(folder)
                if not self.tree.exists(iid):
                    self.tree.insert(parent, "end", iid=iid, text=label, open=not many or depth == 0,
                                     tags=("group" if depth == 0 else "subgroup",))
                self.group_dirs[iid] = folder
                parent = iid
            return parent
        artist, album = self._group_keys(path)
        a_id = "grp:" + artist.casefold()
        b_id = a_id + "\x1f" + album.casefold()
        if not self.tree.exists(a_id):
            self.tree.insert("", "end", iid=a_id, text=artist, open=not many, tags=("group",))
        if not self.tree.exists(b_id):
            self.tree.insert(a_id, "end", iid=b_id, text=album, open=not many, tags=("subgroup",))
        return b_id

    def group_folder(self, node: str) -> str:
        """The folder on disk for a group row: its own folder, or the folder its songs share."""
        if node in self.group_dirs and self._view() == "folder":
            return self.group_dirs[node]
        dirs = [os.path.dirname(display_path(p)) for p in self._songs_under(node)]
        if not dirs:
            return ""
        try:
            return os.path.commonpath(dirs)
        except ValueError:
            return dirs[0]

    def open_location(self, node: str) -> None:
        try:
            if node.startswith("grp:"):
                folder = self.group_folder(node)
                if folder:
                    open_folder(folder)
            else:
                reveal_in_explorer(node)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not open the location:\n{e}")

    def _songs_under(self, node: str) -> list[str]:
        out = []
        for c in self.tree.get_children(node):
            if c.startswith("grp:"):
                out += self._songs_under(c)
            else:
                out.append(c)
        return out

    def _refresh_groups(self, node: str) -> None:
        """Update the totals on a group row and the groups above it (now)."""
        self._mark_dirty(node)
        self._flush_groups()

    def _mark_dirty(self, node: str) -> None:
        if node and node.startswith("grp:"):
            self._dirty_groups.add(node)
            if not self._flush_pending:
                self._flush_pending = True
                self.after_idle(self._flush_groups)

    def _flush_groups(self) -> None:
        """Recompute the totals of changed groups from their direct children, deepest first; a parent
        group adds up its child groups' saved totals instead of walking every song again."""
        self._flush_pending = False
        if not self._dirty_groups:
            return
        dirty, depth = set(), {}
        for n in self._dirty_groups:
            chain = []
            while n and n.startswith("grp:") and n not in dirty and self.tree.exists(n):
                chain.append(n)
                n = self.tree.parent(n)
            dirty.update(chain)
        self._dirty_groups.clear()
        for n in dirty:
            d, x = 0, n
            while x:
                x = self.tree.parent(x)
                d += 1
            depth[n] = d
        thr_c, thr_f = self._confident_threshold(), self._fit_threshold()
        for node in sorted(dirty, key=lambda n: -depth[n]):
            if not self.tree.exists(node):
                continue
            count = n_local = n_online = low = poor = 0
            choices: set = set()
            op_done = {op: 0 for op in OPS}
            op_busy = {op: 0 for op in OPS}
            for c in self.tree.get_children(node):
                if c.startswith("grp:"):
                    st = self._group_stats.get(c)
                    if st is None:
                        continue
                    count += st[0]; n_local += st[1]; n_online += st[2]; low += st[4]; poor += st[5]
                    choices |= st[3]
                    for op in OPS:
                        op_done[op] += st[6][op]
                        op_busy[op] += st[7][op]
                    continue
                r = self.rows.get(c)
                if r is None:
                    continue
                count += 1
                n_local += 1 if r["local"] else 0
                n_online += 1 if r["online"] == "found" else 0
                choices.add(r["choice"])
                cv, fv = self.row_confidence(r), self.row_fit(r)
                low += 1 if cv is not None and cv < thr_c else 0
                poor += 1 if fv is not None and fv < thr_f else 0
                for op in OPS:
                    v = r.get("op_" + op, "")
                    op_done[op] += 1 if v in ("done", "partial") else 0
                    op_busy[op] += 1 if v == "busy" else 0
            if not count:
                self.tree.delete(node)
                self._group_stats.pop(node, None)
                self.group_dirs.pop(node, None)
                continue
            self._group_stats[node] = (count, n_local, n_online, frozenset(choices), low, poor, op_done, op_busy)
            choice = CHOICE_TEXT.get(next(iter(choices)), "") if len(choices) == 1 else "mixed"
            vals = {"status": f"{count} song{'s' if count != 1 else ''}",
                    "local": f"{n_local} local" if n_local else "—",
                    "online": f"{n_online} found" if n_online else "—",
                    "choice": f"⟳ {choice}" if choice else "",
                    "match": f"{low} low" if low else "", "fit": f"{poor} poor" if poor else ""}
            for op in OPS:
                vals[op] = "⟳" if op_busy[op] else (f"{op_done[op]}✓" if op_done[op] else "")
            self.tree.item(node, values=tuple(vals[c] for c, _, _ in COLUMNS))

    def _update_count(self) -> None:
        self.v_count.set(f"{len(self._shown)} shown / {len(self.rows)} songs")

    def _row_tags(self, row: dict, path: str | None = None) -> tuple:
        tags = (self._row_tag(row), "busy") if _busy(row) else (self._row_tag(row),)
        if path is not None and self.song is not None and self.song.path == path:
            tags = ("viewing",) + tags
        return tags

    def _row_tag(self, row: dict) -> str:
        """Colour by what will happen: saved, needs a check, or the timeline level it will use."""
        if row["status"] in PROBLEM_STATUSES:
            return "c_problem"
        if row["status"] in SAVED_STATUSES:
            return "c_saved"
        if row["status"] in DONE_STATUSES:
            return "c_keep"
        if row.get("check"):
            return "c_check"
        return {"found": "c_found", "aligned": "c_aligned", "edited": "c_edited"}.get(self.row_level(row), "c_keep")

    @staticmethod
    def row_level(row: dict) -> str:
        """keep / found / aligned / edited: which timeline Apply will write for this song."""
        if row.get("choice") in ("keep", "remove"):
            return "keep"
        lv = row.get("level")
        if lv in ("found", "aligned", "edited", "keep"):
            return lv
        if row.get("choice") == "custom":
            return "edited"
        if row.get("status") == "aligned" or row.get("op_align") in ("done", "partial"):
            return "aligned"
        if row.get("choice") in ("local", "online") and row.get("status") in FOUND_STATUSES:
            return "found"
        return "keep"

    def _row_values(self, path):
        r = self.rows[path]
        status = SHORT_STATUS.get(r["status"], STATUS_LABELS.get(r["status"], r["status"]))
        choice = CHOICE_TEXT.get(r["choice"], r["choice"])
        conf, fit = self.row_confidence(r), self.row_fit(r)
        if r.get("choice") in ("custom", "remove"):
            match = fitv = "yours"
        else:
            match = "—" if conf is None else f"{conf:.0f}%"
            fitv = "—" if fit is None else f"{fit:.0f}%"
        vals = {"status": status, "local": r["local"] or "—", "online": r["online"] or "—",
                "choice": f"⟳ {choice}" if choice else "", "match": match, "fit": fitv}
        for op in OPS:
            vals[op] = OP_MARK.get(r.get("op_" + op, ""), "")
        st = r.get("op_align", "")
        vals["align"] = (f"⟳ {r['align_pct']:.0f}%" if st == "busy" and r.get("align_pct") is not None
                         else ALIGN_MARK.get(st, ""))
        if not st and r.get("status") == "aligned":
            vals["align"] = ALIGN_MARK["done"]
        if r.get("hand") and r.get("choice") == "custom":
            vals["align"] += HAND                 # you re-timed it yourself in the player
        return tuple(vals[c] for c, _, _ in COLUMNS)

    def set_align_pct(self, path: str, pct: float | None) -> None:
        """Show how far the GPU is with this song in its Align cell (cheap: one cell)."""
        key = state_key(path)
        r = self.rows.get(key)
        if r is None:
            return
        r["align_pct"] = pct
        if key in self._shown and r.get("op_align") == "busy":
            try:
                self.tree.set(key, "align", "⟳" if pct is None else f"⟳ {pct:.0f}%")
            except tk.TclError:
                pass

    @staticmethod
    def _by_choice(r: dict, prefix: str) -> float | None:
        choice = r.get("choice")
        if choice == "local":
            return r.get(prefix + "local")
        if choice == "online":
            return r.get(prefix + "online")
        if choice in ("keep", "custom", "remove"):
            return None
        vals = [c for c in (r.get(prefix + "local"), r.get(prefix + "online")) if c is not None]
        return max(vals) if vals else None

    @classmethod
    def row_confidence(cls, r: dict) -> float | None:
        """Match confidence of the lyrics the song's Choice would write (None = nothing found / not known)."""
        return cls._by_choice(r, "conf_")

    @classmethod
    def row_fit(cls, r: dict) -> float | None:
        """How well the lyrics the song's Choice would write fit the recording (None = not known)."""
        return cls._by_choice(r, "fit_")

    def _confident_threshold(self) -> float:
        try:
            return float(self.app.settings.confident_threshold)
        except (AttributeError, TypeError, ValueError):
            return 95.0

    def _fit_threshold(self) -> float:
        try:
            return float(self.app.settings.confident_fit)
        except (AttributeError, TypeError, ValueError):
            return 80.0

    # -- Match / Fit header filters
    def _score_heading(self, col: str) -> None:
        label = SCORE_COLS[col][0]
        n = self.min_score.get(col, 0)
        self.tree.heading(col, text=f"{label} ≥{n}" if n else f"{label} ▾")

    def set_min_score(self, col: str, value: int, save: bool = True) -> None:
        value = snap5(value)
        if self.min_score.get(col) == value:
            return
        self.min_score[col] = value
        self._score_heading(col)
        attr = SCORE_COLS[col][1]
        setattr(self.app.settings, attr, value)
        if save:
            try:
                save_settings(self.app.settings)
            except OSError:
                pass
        self._refill()

    def _score_level(self, col: str) -> int:
        return int(self._confident_threshold() if col == "match" else self._fit_threshold())

    def set_score_level(self, col: str, value: int) -> None:
        """The level that counts as High confidence (Match) or Good fit (Fit): used by Apply confident,
        Hide low confidence and the low/poor counts. Same setting as on the Find lyrics tab."""
        if hasattr(self.app, "set_score_levels"):
            if col == "match":
                self.app.set_score_levels(confident=value)
            else:
                self.app.set_score_levels(fit=value)
        else:
            setattr(self.app.settings, "confident_threshold" if col == "match" else "confident_fit", snap5(value))
            self._refill()

    def _score_filter_popup(self, col: str) -> None:
        """Header click: sliders (in steps of 5%) for hiding songs below a score, and for the level that
        counts as High confidence / Good fit."""
        if self._score_popup is not None:
            try:
                self._score_popup.destroy()
            except tk.TclError:
                pass
            self._score_popup = None
            return
        label = SCORE_COLS[col][0]
        level_name = "High confidence" if col == "match" else "Good fit"
        lo = 50 if col == "match" else 0
        top = tk.Toplevel(self)
        top.wm_overrideredirect(True)
        try:
            top.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        self._score_popup = top
        p = theme.palette()
        frame = tk.Frame(top, background=p["surface"], highlightthickness=1, highlightbackground=p["border"],
                         padx=10, pady=8)
        frame.pack(fill="both", expand=True)

        def slider(caption_fn, start, lo_, apply_fn, tip):
            var = tk.IntVar(value=snap5(start, lo_))
            cap = tk.StringVar(value=caption_fn(var.get()))
            tk.Label(frame, textvariable=cap, background=p["surface"], foreground=p["fg"],
                     font=theme.font("body")).pack(anchor="w")
            sc = ttk.Scale(frame, from_=lo_, to=100, orient="horizontal", length=240)

            def moved(v):
                snapped = snap5(float(v), lo_)
                if snapped != var.get():
                    var.set(snapped)
                    cap.set(caption_fn(snapped))

            sc.configure(command=moved)
            sc.set(var.get())
            sc.pack(fill="x", pady=(4, 8))
            theme.hint(sc, tip)

            def release(_e=None):
                sc.set(var.get())                      # the handle rests on the 5% step
                apply_fn(var.get())
            sc.bind("<ButtonRelease-1>", release, add="+")
            for key, step in (("<Left>", -5), ("<Down>", -5), ("<Right>", 5), ("<Up>", 5)):
                sc.bind(key, lambda e, d=step: (var.set(snap5(var.get() + d, lo_)), cap.set(caption_fn(var.get())),
                                                release(), "break")[-1])
            return var, sc

        fvar, fsc = slider(lambda v: f"Show {label} ≥ {v}%" if v else f"Show every {label} score",
                           self.min_score.get(col, 0), 0, lambda v: self.set_min_score(col, v),
                           f"Hide songs whose {label} score is below this (steps of 5%). Songs without a score "
                           "stay listed.")
        lvar, _ = slider(lambda v: f"{level_name} at ≥ {v}%", self._score_level(col), lo,
                         lambda v: self.set_score_level(col, v),
                         f"The {label} score that counts as {level_name}: Apply confident, Hide low confidence and "
                         f"the group counts use it. Same setting as on the Find lyrics tab.")
        row = tk.Frame(frame, background=p["surface"])
        row.pack(fill="x")

        def close():
            self.set_min_score(col, fvar.get())
            if lvar.get() != self._score_level(col):
                self.set_score_level(col, lvar.get())
            try:
                top.destroy()
            except tk.TclError:
                pass
            self._score_popup = None
        theme.hint(ttk.Button(row, text="Clear", command=lambda: (fvar.set(0), fsc.set(0), close())),
                   f"Show every {label} score again.").pack(side="left")
        theme.hint(ttk.Button(row, text="Done", command=close), "Close the filter.").pack(side="right")
        top.bind("<Escape>", lambda e: close())
        top.bind("<Return>", lambda e: close())
        try:
            shown = self._shown_columns()
            bbox_x = sum(int(self.tree.column(c, "width")) for c in ["#0"] + shown[:shown.index(col)])
        except (tk.TclError, ValueError):
            bbox_x = 0
        x = self.tree.winfo_rootx() + max(0, min(bbox_x, self.tree.winfo_width() - 270))
        y = self.tree.winfo_rooty() + 26
        top.geometry(f"+{x}+{y}")
        top.update_idletasks()
        try:
            top.focus_force()
            fsc.focus_set()
        except tk.TclError:
            pass
        top.bind("<FocusOut>", lambda e: self.after(150, lambda: self._close_popup_if_unfocused(top, close)),
                 add="+")

    def _close_popup_if_unfocused(self, top, close) -> None:
        try:
            if not top.winfo_exists():
                return
            f = top.focus_get()
        except (tk.TclError, KeyError):
            f = None
        if f is None or not str(f).startswith(str(top)):
            close()

    @staticmethod
    def has_found_lyrics(r: dict) -> bool:
        """Lyrics were found for this song (it can be, or was, written)."""
        return bool(r.get("local") or r.get("online") == "found" or r.get("choice") in ("custom", "remove")
                    or r.get("status") in FOUND_STATUSES or r.get("status") in SAVED_STATUSES)

    def _filter_toggled(self, key: str, var: tk.BooleanVar) -> None:
        if key:
            setattr(self.app.settings, key, bool(var.get()))
            try:
                save_settings(self.app.settings)
            except OSError:
                pass
        self._refill()
        if hasattr(self.app, "session_changed"):
            self.app.session_changed()

    # -- columns: show / hide
    def _shown_columns(self) -> list[str]:
        dc = self.tree["displaycolumns"]
        if isinstance(dc, str):
            dc = (dc,)
        dc = [str(c) for c in dc]
        return [c for c, _, _ in COLUMNS] if dc in (["#all"], []) else dc

    def _column_at(self, x: int) -> str:
        """Column id under x (display positions shift when columns are hidden)."""
        ident = self.tree.identify_column(x)
        if ident == "#0":
            return "#0"
        try:
            return self._shown_columns()[int(ident.lstrip("#")) - 1]
        except (ValueError, IndexError):
            return ""

    def _apply_columns(self, cols, save: bool = True) -> None:
        order = [c for c, _, _ in COLUMNS]
        shown = [c for c in order if c in set(cols)]
        self.tree["displaycolumns"] = shown or ["status"]
        if save:
            self.app.settings.list_columns = list(shown)
            try:
                save_settings(self.app.settings)
            except OSError:
                pass

    def toggle_column(self, col: str) -> None:
        shown = self._shown_columns()
        self._apply_columns([c for c in shown if c != col] if col in shown else shown + [col])

    def _columns_menu(self) -> tk.Menu:
        m = theme.register(tk.Menu(self, tearoff=0), "menu")
        shown = set(self._shown_columns())
        self._col_vars = {}
        for col, _, label in COLUMNS:
            v = tk.BooleanVar(value=col in shown)
            self._col_vars[col] = v
            m.add_checkbutton(label=label.replace(" ⟳", ""), variable=v, command=lambda c=col: self.toggle_column(c))
        m.add_separator()
        m.add_command(label="Show all columns", command=lambda: self._apply_columns([c for c, _, _ in COLUMNS]))
        return m

    # -- per-song progress of the batch operations
    def clear_busy(self, op: str) -> None:
        """An operation ended (finished, stopped or failed): no song is in progress in it any more."""
        for p, r in self.rows.items():
            if r.get("op_" + op) == "busy":
                self.add_row(p, None if r.get("status") != "searching" else "", ops={op: ""})

    def set_op(self, path: str, op: str, state: str, status: str | None = None) -> None:
        """Mark a song's progress in one operation column: busy (⟳), done (✓), skip (–), fail (✗)."""
        key = state_key(path)
        if key not in self.rows and not (op == "find" and state == "busy"):
            return                                # only Find adds songs to the list
        self.add_row(key, status, ops={op: state})

    def remove_from_list(self, songs: list[str]) -> int:
        """Take songs out of the list (files and saved choices are untouched; Apply leaves them out)."""
        n = 0
        for p in songs:
            if self.rows.pop(p, None) is None:
                continue
            n += 1
            self.row_status_before_choice.pop(p, None)
            if p in self._shown:
                parent = self.tree.parent(p)
                self.tree.delete(p)
                self._shown.discard(p)
                self._mark_dirty(parent)
        self._flush_groups()
        self._update_count()
        if n:
            if hasattr(self.app, "session_changed"):
                self.app.session_changed()
            if hasattr(self.app, "refresh_apply_bar"):
                self.app.refresh_apply_bar()
        return n

    def _delete_key(self, _e=None) -> str:
        songs = self.selected_songs()
        if songs:
            self.remove_from_list(songs)
        return "break"

    def clear_list(self) -> None:
        """Empty the song list (a new Find run starts a new list). The player keeps the open song."""
        self.rows.clear()
        self.tree.delete(*self.tree.get_children())
        self.group_dirs.clear()
        self._group_stats.clear()
        self._dirty_groups.clear()
        self._shown.clear()
        self._group_target.clear()
        self.row_status_before_choice.clear()
        self._update_count()

    def _row_text(self, path) -> str:
        r = self.rows[path]
        name = r["title"] or os.path.basename(display_path(path))
        if self.song is not None and self.song.path == path:
            return "▶ " + name                     # the song in the player
        return ("⚠ " if r.get("check") else "") + name

    def _upsert(self, path):
        vals, tags = self._row_values(path), self._row_tags(self.rows[path], path)
        parent = self._ensure_groups(path)
        if path in self._shown:
            old_parent = self.tree.parent(path)
            if old_parent != parent:
                self.tree.move(path, parent, "end")
                self._mark_dirty(old_parent)
            self.tree.item(path, text=self._row_text(path), values=vals, tags=tags)
        else:
            self.tree.insert(parent, "end", iid=path, text=self._row_text(path), values=vals, tags=tags)
            self._shown.add(path)
        self._mark_dirty(parent)

    def _matches(self, path) -> bool:
        statuses = FILTERS.get(self.v_filter.get())
        r = self.rows[path]
        if _busy(r):
            return True                                   # being worked on right now: always shown
        if statuses is not None and r["status"] not in statuses:
            return False
        if self.v_only_found.get() and not self.has_found_lyrics(r):
            return False
        if self.v_hide_saved.get() and r["status"] in DONE_STATUSES:
            return False
        if self.v_hide_low.get() and r["status"] not in DONE_STATUSES:
            c, f = self.row_confidence(r), self.row_fit(r)
            if (c is not None and c < self._confident_threshold()) or (f is not None and f < self._fit_threshold()):
                return False
        for col, value in (("match", self.row_confidence(r)), ("fit", self.row_fit(r))):
            n = self.min_score.get(col, 0)
            if n and value is not None and value < n:
                return False
        q = self.v_search.get().strip().casefold()
        return not q or q in f"{r['artist']} {r.get('album', '')} {r['title']} {path}".casefold()

    def _refill(self) -> None:
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self.group_dirs.clear()
        self._group_stats.clear()
        self._dirty_groups.clear()
        self._shown.clear()
        for p in self.rows:
            if self._matches(p):
                parent = self._ensure_groups(p)
                self.tree.insert(parent, "end", iid=p, text=self._row_text(p), values=self._row_values(p),
                                 tags=self._row_tags(self.rows[p], p))
                self._shown.add(p)
                self._dirty_groups.add(parent)
        self._flush_groups()
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
            self.tree.see(sel[0])
        self._update_count()

    def load_earlier(self) -> None:
        try:
            st = State(self.app.settings.resolved_state_db())
            rows = st.iter_files()
            choices = {r[0]: st.current_choice(r[0]) for r in rows}
            scores = st.all_candidate_scores()
            st.close()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not read earlier results:\n{e}")
            return
        for path, status, source, artist, title, _, local, online, album in rows:
            if path not in self.rows:
                c = scores.get(path, {})
                lo, on = c.get("local", (None, None)), c.get("online", (None, None))
                self.rows[path] = {"status": status, "source": source or "", "artist": artist or "",
                                   "title": title or "", "local": local or "", "online": online or "",
                                   "choice": choices.get(path, ""), "album": album or "",
                                   "conf_local": lo[0], "conf_online": on[0], "fit_local": lo[1],
                                   "fit_online": on[1]}
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
        self.v_only_found.set(False)
        self.v_hide_saved.set(False)
        self.v_hide_low.set(False)
        self._refill()
        self.tree.selection_set(p)
        self.tree.see(p)
        self.app.show_apply_bar()

    def _tree_click(self, e):
        """Clicking a Choice cell cycles: found sources → Don't change."""
        if self.tree.identify_region(e.x, e.y) != "cell" or self._column_at(e.x) != "choice":
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
            text = COLUMN_HINTS.get(self._column_at(e.x), "")
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
        if self.tree.identify_region(e.x, e.y) == "heading":
            self._columns_menu().tk_popup(e.x_root, e.y_root)
            return "break"
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
        if node.startswith("grp:"):
            m.add_command(label="Open folder", command=lambda: self.open_location(node))
        else:
            m.add_command(label="Open file location", command=lambda: self.open_location(node))
        m.add_separator()
        target = "embed into" if self.app.settings.output_mode == "embed" else "save .lrc for"
        m.add_command(label=f"Save now — {target} {what}", command=lambda: self.app.apply_paths(songs))
        m.add_separator()
        m.add_command(label="Use Local lyrics", command=lambda: self.set_many(songs, "local"))
        m.add_command(label="Use Online lyrics", command=lambda: self.set_many(songs, "online"))
        m.add_command(label="Don't change", command=lambda: self.set_many(songs, "keep"))
        m.add_command(label="Reset to default", command=lambda: self.reset_many(songs))
        if hasattr(self.app, "align_command"):
            m.add_command(label=f"{GPU} Align on the GPU ({what})",
                          command=lambda: self.app.align_command("songs", songs))
        if hasattr(self.app, "clear_cache_command"):
            m.add_command(label=f"Clear cached data ({what})…", command=lambda: self.app.clear_cache_command(songs))
        m.add_separator()
        m.add_command(label=f"Restore original lyrics ({what})…", command=lambda: self.app.restore_paths(songs))
        m.add_separator()
        where = "this " + ("folder" if self._view() == "folder" else "group") if node.startswith("grp:") and \
            len(self.tree.selection()) == 1 else what
        m.add_command(label=f"Remove {where} from the list", command=lambda: self.remove_from_list(songs))
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
                if p in self.rows:
                    self.rows[p]["hand"] = False
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
                "search": self.v_search.get(), "only_found": bool(self.v_only_found.get())}

    def load_session_rows(self, data: dict, include_saved: bool = False) -> int:
        """Bring back last session's songs. Songs whose lyrics were already saved (written to the file
        or a .lrc) are left out unless include_saved; then they're listed as Saved. Files are never
        changed by this. Returns how many songs were loaded."""
        rows = dict(data.get("rows") or {})
        try:
            st = State(self.app.settings.resolved_state_db())
            try:
                scores = st.all_candidate_scores()
                for p, r in list(rows.items()):
                    c = scores.get(p, {})
                    lo, on = c.get("local", (None, None)), c.get("online", (None, None))
                    r["conf_local"], r["conf_online"], r["fit_local"], r["fit_online"] = lo[0], on[0], lo[1], on[1]
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
            base = _blank_row()
            base.update({k: v for k, v in r.items() if k in base})
            for op in OPS:                                # nothing is in progress after a restart
                if base["op_" + op] == "busy":
                    base["op_" + op] = ""
            if base["status"] == "searching":
                base["status"] = ""
            self.rows[p] = base
        if "only_found" in data:
            self.v_only_found.set(bool(data["only_found"]))
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
        songs = [k for k in sel if not k.startswith("grp:")]
        if songs and hasattr(self.app, "prioritize"):
            self.app.prioritize(songs[:1] if len(songs) > 20 else songs)
        focus = self.tree.focus()
        if focus in sel and not focus.startswith("grp:"):
            if self.song is None or self.song.path != focus:
                self.load_song(focus)
            return
        if len(sel) == 1 and not sel[0].startswith("grp:"):
            if self.song is not None and self.song.path == sel[0]:
                return                                   # already showing it: don't reload (edits stay)
            self.load_song(sel[0])

    # ========================================================= song loading
    def load_song(self, path: str) -> None:
        if self._auto_job is not None:            # your last edit on the previous song: choose Edited now
            self._cancel_auto()
            try:
                self._auto_use_edited()
            except Exception:
                pass
        self.player.stop()
        self.cp_range = None
        self._token += 1
        token = self._token
        name = os.path.basename(display_path(path))
        self.v_title.set(f"Loading {name}…")
        s = self.app.settings
        threading.Thread(target=self._load_worker, args=(token, path, s), daemon=True).start()

        def show_if_slow():                       # quick loads don't flash a row
            act = getattr(self.app, "activity", None)
            if act is not None and token == self._token and getattr(self, "_loaded_token", 0) != token:
                act.set("load", "Loading song", None, detail=name)
        self.after(400, show_if_slow)

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
        fits = {}
        if not err:
            try:
                audio = decode(display_path(path))
            except Exception as e:
                audio_err = f"Could not decode audio: {type(e).__name__}: {e}"
            if audio is not None:
                try:
                    fits = self._store_audio_fits(path, s, audio)
                except Exception:
                    fits = {}
        self.app.events.put({"type": "review_loaded", "token": token, "path": path, "info": info,
                             "audio": audio, "err": err, "audio_err": audio_err, "fits": fits})

    @staticmethod
    def _store_audio_fits(path: str, s, audio) -> dict:
        """Re-score the found lyrics against the decoded audio and save the result. {kind: fit}."""
        st = State(s.resolved_state_db())
        out = {}
        try:
            pend = st.get_pending(path)
            for kind, (_label, text, _lp, length) in st.get_candidates(path).items():
                f = fitmod.fit_score(text, audio.duration, length or None, audio.peaks)[0]
                st.set_candidate_fit(path, kind, f)
                out[kind] = f
                if pend is not None and lrcmod.same_lyrics(pend[2], text):
                    st.set_pending_fit(path, text, f)
            st.commit()
        finally:
            st.close()
        return out

    def handle(self, e: dict) -> None:
        t = e["type"]
        if t == "review_loaded":
            self._loaded_token = e["token"]
            if e["token"] != self._token:
                return
            if e["err"]:                         # e.g. the file was moved: say so without blocking
                self.v_title.set(os.path.basename(display_path(e["path"])))
                self.v_audio_note.set(f"Couldn't open this song: {e['err']}")
                return
            fits = e.get("fits") or {}
            if fits and e["path"] in self.rows:
                self.add_row(e["path"], fit_local=fits.get("local"), fit_online=fits.get("online"))
                self.app.refresh_apply_bar()
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
                    row = self.rows.get(state_key(e["path"]))
                    if row is not None:
                        row["hand"] = bool(e.get("hand")) and e["status"] == "chosen"
                        row["level"] = e.get("level") or ""
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
        prev = self.song.path if self.song is not None else None
        self.song, self.audio = info, audio
        for p in {prev, info.path}:                 # move the ▶ marker to the song now in the player
            if p and p in self.rows and p in self._shown:
                self._upsert(p)
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
        self._lanes_ver += 1
        self._render(self.txt_cur, self.cur_lines,
                     empty="(no .lrc file yet)" if lrc_mode else "(no embedded lyrics)")
        self.cands = list(info.candidates)
        self.heard_by_cand = {}
        self.v_ai_note.set("")
        self._ai_button()
        self._attach_alignment(info)
        self._fill_candidates()
        prefs = info.prefs or {}
        idx = 0
        if prefs.get("cand"):
            idx = next((i for i, c in enumerate(self.cands) if c.key == prefs["cand"]), 0)
        self.cp_custom = "checkpoints" in prefs
        self.checkpoints = [None if t is None else float(t) for t in prefs.get("checkpoints", [])]
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

    def _levels(self) -> dict:
        """The texts each level would write for this song: found, aligned, edited (None = not available)."""
        c = self.cand
        if c is None:
            return {"found": None, "aligned": None, "edited": None}
        tidy = self.app.settings.tidy
        found = tidy(self.found_text if self.found_text is not None else c.text) if c.text is not None else None
        return {"found": found, "aligned": tidy(self.aligned_text) if self.aligned_text else None,
                "edited": self.new_text}

    def current_level(self) -> tuple[str, bool]:
        """(level Apply will write: keep / found / aligned / edited / other, whether it's the default)."""
        if self.song is None:
            return "", False
        ov = self.song.override
        try:
            st = State(self.app.settings.resolved_state_db())
            pend = st.get_pending(self.song.path)
            choice = st.current_choice(self.song.path)
            st.close()
        except Exception:
            pend, choice = None, ""
        if choice == "keep":
            return "keep", ov is None
        text = ov[1] if ov is not None else (pend[2] if pend is not None else None)
        if not text:
            return ("keep" if ov is None else "other"), ov is None
        lv = self._levels()
        for name in ("aligned", "found", "edited"):        # the same text can be several levels: name the
            if lv[name] and lrcmod.same_lyrics(lv[name], text):      # most specific one that isn't edits
                if name != "edited" or self._user_changes():
                    return name, ov is None
        if lv["edited"] and lrcmod.same_lyrics(lv["edited"], text):
            return "edited", ov is None
        return "other", ov is None

    def _user_changes(self) -> bool:
        """Changes made by you (not only the aligner's) in the Edited timeline."""
        lines = self.cand is not None and self.found_text is not None and self.cand.text != self.found_text
        return bool(self.edited - self.ai_keys) or bool(set(self.end_marks) - self.ai_ends) or \
            abs(self.offset) > 1e-6 or abs(self.factor - 1) > 1e-9 or lines

    def _update_choice_buttons(self) -> None:
        """Green ✓ on the level Apply will write for this file; scores on Found and Aligned."""
        lv = self._levels()
        level, default = self.current_level()
        found_pct = self._found_score()
        names = {"keep": "Keep current", "found": "Use found" + (f" · {found_pct:.0f}%" if found_pct is not None
                                                                 else ""),
                 "aligned": GPU + " Use aligned" + (f" · {self.aligned_verify:.0f}%" if self.aligned_verify
                                                     is not None else ""),
                 "edited": HAND + " Use edited"}
        for name, btn in (("keep", self.btn_c_keep), ("found", self.btn_c_found), ("aligned", self.btn_c_aligned),
                          ("edited", self.btn_c_edited)):
            on = level == name
            self._style_btn(btn, on, names[name] + (" (default)" if on and default else ""))
            avail = self.song is not None and (name == "keep" or bool(lv.get(name)))
            btn.config(state="normal" if avail else "disabled")
        self._style_btn(self.btn_keep, level == "keep", "Keeping current" if level == "keep" else "Keep current")
        try:
            self.btn_from_aligned.state(["!disabled"] if self.aligned_text else ["disabled"])
        except (tk.TclError, AttributeError):
            pass
        note = ""
        if level == "other" and self.song is not None:
            ov = self.song.override
            note = f"Will use: {ov[2]}" if ov is not None and ov[2] else "Will use other lyrics (see Choice)"
        self.v_choice_note.set(note)

    def _found_score(self) -> float | None:
        c = self.cand
        if c is None:
            return None
        sc = self.cand_scores.get(c.key + "-original") or self.cand_scores.get(c.key)
        return sc[0] if sc and sc[0] is not None else None

    def _cancel_auto(self) -> None:
        """You chose a level yourself: a pending switch to Edited (from your last edit) is dropped."""
        if self._auto_job is not None:
            try:
                self.after_cancel(self._auto_job)
            except tk.TclError:
                pass
            self._auto_job = None

    def _use_level(self, level: str) -> None:
        """Choose what to write for this song: the found, aligned or edited timeline."""
        self._cancel_auto()
        if self.song is None or self.cand is None:
            return
        if level == "edited":
            self._apply()
            return
        lv = self._levels()
        text = lv.get(level)
        if not text:
            return
        c = self.cand
        label = re.split(r" · (?:partly )?aligned \(", c.label)[0]
        label = label + (" (found timing)" if level == "found" else
                         f" · aligned ({self.aligned_verify or 0:.0f}% heard)")
        self._run_action(lambda s: review.choose(self.song.path, text, label, s, c.path), "chosen",
                         level=level)

    def _duration(self) -> float:
        if self.audio is not None:
            return self.audio.duration
        return self.song.duration if self.song else 0.0

    # ========================================================== candidates
    def _fill_candidates(self) -> None:
        self.cand_fit = {}
        dur = self._duration()
        peaks = self.audio.peaks if self.audio is not None else None
        for c in self.cands:
            if c.text and c.key != "current":
                self.cand_fit[c.key] = fitmod.fit_score(c.text, dur, c.src_length or None, peaks)
        labels = []
        for c in self.cands:
            f = self.cand_fit.get(c.key, (None, []))[0]
            labels.append(f"{c.label}  ·  fit {f:.0f}%" if f is not None else c.label)
        self.cmb_cand.config(values=labels or ["(no candidates — try Search LRCLIB)"])

    def _show_fit_note(self) -> None:
        f, why = getattr(self, "cand_fit", {}).get(self.cand.key, (None, [])) if self.cand else (None, [])
        if f is None:
            self.v_fit.set("")
        else:
            self.v_fit.set(f"Fit {f:.0f}%: " + "; ".join(why) if why else "")

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
        self._show_fit_note()
        self.anchors = []
        self.sel_line = None
        self.sel_lane, self.sel_keys = None, set()
        ad = getattr(self.cand, "ai_detail", None)
        if not hasattr(self.cand, "found_text"):
            self.cand.found_text = self.cand.text          # as found, before any line was added or deleted
        self.found_text = self.cand.found_text
        mine = restore if restore and restore.get("cand") == self.cand.key else None
        if mine and mine.get("base") and self.cand.text is not None:
            self.cand.text = mine["base"]                  # your added / deleted lines
        self._set_aligned(ad if ad and ad.get("text") else (mine or {}).get("aligned"))
        if restore and restore.get("cand") == self.cand.key:
            self.pins = {k: float(v) for k, v in (restore.get("pins") or {}).items()}
            self.edited = set(restore.get("edited") or [])
            self.interpolate = bool(restore.get("interpolate")) and bool(self.pins)
            self.ai_keys = set(restore.get("ai") or []) & set(self.pins)
            self.warp_mode = restore.get("mode") or "slope"
            self.end_marks = {k: float(v) for k, v in (restore.get("ends") or {}).items()}
            self.ai_ends = set(restore.get("ai_ends") or []) & set(self.end_marks)
            self.ai_conf = {k: float(v) for k, v in (restore.get("ai_conf") or {}).items()}
            self._set_timing(restore.get("offset", 0.0), restore.get("factor", 1.0))
            if ad and restore.get("ai_stamp") != ad.get("stamp"):
                # aligned (again) on the GPU since you last had this song open: show that alignment,
                # keeping the times you set yourself
                self._overlay_ai(ad)
        else:
            self.pins, self.edited, self.interpolate = {}, set(), False
            self.ai_keys, self.warp_mode = set(), "slope"
            self.end_marks, self.ai_ends, self.ai_conf = {}, set(), {}
            if ad:                     # aligned while finding: show it as the aligner's edits on the original
                self._overlay_ai(ad)
            self._set_timing(0.0, 1.0)
        self._recompute()

    def _set_aligned(self, ad: dict | None) -> None:
        """The Aligned timeline for the shown lyrics (a GPU result), or none."""
        ad = ad or {}
        self.aligned_text = ad.get("text") or None
        self.aligned_verify = ad.get("verify")
        self.aligned_pins = {k: float(v) for k, v in (ad.get("pins") or {}).items()}
        self.aligned_ends = {k: float(v) for k, v in (ad.get("ends") or {}).items()}
        self.aligned_conf = {k: float(v) for k, v in (ad.get("confs") or {}).items()}
        self.aligned_mode = ad.get("mode") or "slope"

    def _overlay_ai(self, ad: dict) -> None:
        """Put a stored GPU alignment on the current timing: its pins (purple), gaps and certainty.
        Times you set yourself stay."""
        mine = self.edited - self.ai_keys
        for k in list(self.ai_keys):
            if k not in mine:
                self.pins.pop(k, None)
                self.edited.discard(k)
        self.ai_keys = set()
        for k, v in (ad.get("pins") or {}).items():
            if k in mine:
                continue
            self.pins[k] = float(v)
            self.edited.add(k)
            self.ai_keys.add(k)
        for k in list(self.ai_ends):
            self.end_marks.pop(k, None)
        self.ai_ends = set()
        for k, v in (ad.get("ends") or {}).items():
            if k not in self.end_marks:
                self.end_marks[k] = float(v)
                self.ai_ends.add(k)
        self.ai_conf = dict(ad.get("confs") or {})
        self.interpolate = bool(self.pins) and ad.get("mode") != "fixed"
        if ad.get("mode") in ("slope", "ripple"):
            self.warp_mode = ad["mode"]
        self._ai_stamp = ad.get("stamp")

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
        self._user_edited()
        if self._recompute_job:
            self.after_cancel(self._recompute_job)
        self._recompute_job = self.after(150, self._recompute)

    def _recompute(self) -> None:
        self._recompute_job = None
        c = self.cand
        text = c.text if c else None
        self._marker_rows = {}
        self.orig_segs = lrcmod.segments(self.app.settings.tidy(text), self._duration()) \
            if text and c.synced else []
        if text and c.synced:
            plain = lrcmod.display_lines(text)
            marked, keys = self._with_end_marks(text, plain)
            self.orig_lines = lrcmod.display_lines(marked)
            for i, (t, b) in enumerate(self.orig_lines):
                k = keys.get(lrcmod.pin_key(t)) if t is not None and not b.strip() else None
                if k is not None:
                    self._marker_rows[i] = k
            timed = lrcmod.retime(text, self.offset, self.factor, self.pins, self._warping(), mode=self._mode(),
                                  duration=self._duration() or None)
            text = lrcmod.insert_blank_lines(timed, self._end_mark_times(plain, lrcmod.display_lines(timed)))
        else:
            self.orig_lines = []
        if text:
            text = self.app.settings.tidy(text)           # show exactly what would be written
        self._style_interp()
        self.new_text = text
        self.new_lines = lrcmod.display_lines(text)
        self.new_segs = lrcmod.segments(text, self._duration())
        self._build_bars()
        self._lanes_ver += 1                              # the plots follow every change, live
        if c is None:
            empty = "(nothing to compare)"
        elif c.text is None:
            empty = "(lyrics will be removed)"
        else:
            empty = "(empty)"
        self._render(self.txt_new, self.new_lines, empty=empty, pins=bool(self.orig_lines))
        if self.sel_line is not None and self.sel_line < len(self.new_lines):
            self.txt_new.tag_add("pick", f"{self.sel_line + 1}.0", f"{self.sel_line + 1}.end")
        self._update_choice_buttons()
        if not self.cp_custom:
            src = text if lrcmod.is_synced(text) else self.song.current if self.song else ""
            self.checkpoints = review.default_checkpoints(src, self._duration())
        self._now = {"cur": -1, "new": -1}
        self._draw_overview()
        self._draw_cp_buttons()
        self._save_prefs_later()

    # -- end marks: a lyric that ends before the next one starts (a gap: talk, a break)
    def _with_end_marks(self, text: str, plain) -> tuple[str, dict]:
        """The found lyrics with an empty line right after each lyric that has an end mark (at its
        original time + 0.01 s), so New-list rows and original lines stay one-to-one."""
        if not self.end_marks:
            return text, {}
        times = [t for t, _ in plain if t is not None]
        ins, keys = [], {}
        for i, t in enumerate(times):
            k = lrcmod.pin_key(t)
            if k in self.end_marks and (i + 1 >= len(times) or times[i + 1] - t > 0.02):
                ins.append(t + 0.01)
                keys[lrcmod.pin_key(t + 0.01)] = k
        return lrcmod.insert_blank_lines(text, ins), keys

    def _end_mark_times(self, plain, timed) -> list[float]:
        """Where each end mark goes now: its time, kept between its lyric and the next line."""
        out = []
        dur = self._duration() or 1e9
        for i, ((ot, _), (nt, _)) in enumerate(zip(plain, timed)):
            if ot is None or lrcmod.pin_key(ot) not in self.end_marks:
                continue
            if i + 1 < len(plain) and plain[i + 1][0] is not None and plain[i + 1][0] - ot <= 0.02:
                continue
            nxt = timed[i + 1][0] if i + 1 < len(timed) and timed[i + 1][0] is not None else dur
            want = self.end_marks[lrcmod.pin_key(ot)]
            out.append(max(nt + 0.05, min(want, nxt - 0.01)))
        return out

    def has_edits(self) -> bool:
        return bool(self.pins or self.edited or self.end_marks or abs(self.offset) > 1e-6
                    or abs(self.factor - 1.0) > 1e-9)

    def _render(self, widget: tk.Text, lines, empty: str, pins: bool = False) -> None:
        y = widget.yview()[0]
        widget.config(state="normal")
        widget.delete("1.0", "end")
        if not lines:
            widget.insert("end", empty, "ts")
        for i, (t, txt) in enumerate(lines):
            if pins and t is not None and i in self._marker_rows:
                widget.insert("end", "   ", ("pin", "unpinned"))
                widget.insert("end", " ")
            elif pins and t is not None:
                pinned = i < len(self.orig_lines) and lrcmod.pin_key(self.orig_lines[i][0]) in self.pins
                widget.insert("end", "📌" if pinned else " ○ ", ("pin", "pinned" if pinned else "unpinned"))
                widget.insert("end", " ")
            if t is not None:
                key = lrcmod.pin_key(self.orig_lines[i][0]) if pins and i < len(self.orig_lines) else None
                tags = ("ts",)
                if i in self._marker_rows:
                    tags = ("ts", "ai_edited" if self._marker_rows[i] in self.ai_ends else "edited")
                elif key in self.ai_keys and key in self.pins:
                    tags = ("ts", "ai_edited")
                elif key in self.edited:
                    tags = ("ts", "edited")
                elif key in self.pins:
                    tags = ("ts", "ts_pinned")
                elif pins and self._warping() and i < len(self.orig_lines) and \
                        abs(t - (self.orig_lines[i][0] * self.factor + self.offset)) > 0.05:
                    tags = ("ts", "moved")                # moved by interpolation / ripple
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
            self._editor_serial = getattr(e, "serial", None)     # this click opened it: don't close it
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
        if self._ripple_on():
            messagebox.showinfo(APP_TITLE, "Ripple changes is on, so the other lines already follow your pins "
                                           "and edits. Turn Ripple changes off to place lines one by one.")
            return
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
        self.ai_keys = set()
        self.end_marks, self.ai_ends, self.ai_conf = {}, set(), {}
        if not self.pins:
            self.interpolate = False
        self._recompute()

    def _style_interp(self) -> None:
        if not hasattr(self, "btn_interp"):
            return
        n_edit = len(self.edited)
        n_pin = len(self.pins) - n_edit
        self.v_pins.set(f"{n_pin} pinned · {n_edit} edited" if (n_pin or n_edit) else "")
        if self._ripple_on() and self.pins:
            n = len(self.pins)
            self.btn_interp.config(text=f"✓ Ripple · {n}")
            theme.set_flat_kind(self.btn_interp, "success")
        elif self.interpolate:
            n = len(self.pins)
            self.btn_interp.config(text=f"✓ Interpolated · {n}")
            theme.set_flat_kind(self.btn_interp, "success")
        else:
            self.btn_interp.config(text="Interpolate")
            theme.set_flat_kind(self.btn_interp, "neutral")

    def toggle_pin(self, idx: int) -> None:
        """Pin / unpin a New-pane line at its current time."""
        if not (0 <= idx < len(self.orig_lines)) or idx >= len(self.new_lines) or idx in self._marker_rows:
            return
        key = lrcmod.pin_key(self.orig_lines[idx][0])
        if key in self.pins:
            del self.pins[key]
            if not self.pins:
                self.interpolate = False
        else:
            self.pins[key] = float(self.new_lines[idx][0])
        self.edited.discard(key)
        self.ai_keys.discard(key)
        self._recompute()

    # ---------------------------------------------------- editing timestamps
    def _line_key(self, idx: int) -> str | None:
        if idx in self._marker_rows:
            return "end:" + self._marker_rows[idx]
        if 0 <= idx < len(self.orig_lines):
            return lrcmod.pin_key(self.orig_lines[idx][0])
        return None

    def set_line_time(self, idx: int, seconds: float | None) -> None:
        """Give line `idx` of the New pane a fixed time; None resets it to the computed time."""
        key = self._line_key(idx)
        if key is None:
            return
        if key.startswith("end:"):                # the end of a lyric (a gap after it)
            k = key[4:]
            self.ai_ends.discard(k)
            if seconds is None:
                self.end_marks.pop(k, None)
            else:
                self.end_marks[k] = max(0.0, float(seconds))
            self._recompute()
            return
        self.ai_keys.discard(key)                 # a time set by hand is the user's own
        self._user_edited()
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
        """Inline editor over the timestamp: Enter or clicking anywhere else saves, Esc cancels, empty +
        Enter resets."""
        prev = getattr(self, "_editor_commit", None)
        if getattr(self, "_editor", None) is not None and prev is not None:
            prev()                                # another timestamp clicked: keep what was typed in this one
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
        self.after_idle(lambda: ent.winfo_exists() and self._editor is ent and ent.focus_set())   # the click's
        self._editor = ent                                                     # own handling moves focus away

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
        self._editor_commit = commit
        self._editor_serial = None
        self._watch_clicks()

    def _watch_clicks(self) -> None:
        """Clicks on things that don't take the keyboard focus (the lyric list, the waveforms, empty space)
        don't end an inline editor by themselves: watch every click while one is open."""
        if getattr(self, "_clicks_watched", False):
            return
        self._clicks_watched = True
        self.bind_all("<ButtonPress>", self._click_away, add="+")

    def _click_away(self, e) -> None:
        ed = getattr(self, "_editor", None)
        if ed is not None and e.widget is not ed and getattr(e, "serial", None) != self._editor_serial:
            commit = getattr(self, "_editor_commit", None)
            if commit is not None:
                commit()
        vol = getattr(self, "_vol_editor", None)
        if vol is not None and e.widget is not vol and getattr(e, "serial", None) != getattr(self, "_vol_serial", None):
            close = getattr(self, "_vol_close", None)
            if close is not None:
                close(True)

    def _close_editor(self) -> None:
        ed = getattr(self, "_editor", None)
        self._editor = None
        self._editor_commit = None
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
        m.add_separator()
        lyric = not str(key).startswith("end:")
        m.add_command(label="Insert a line before…", command=lambda: self.insert_line(idx, after=False),
                      state="normal" if lyric else "disabled")
        m.add_command(label="Insert a line after…", command=lambda: self.insert_line(idx, after=True),
                      state="normal" if lyric else "disabled")
        m.add_command(label="Delete this line", command=lambda: self.delete_line(idx),
                      state="normal" if lyric else "disabled")
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()
        return "break"

    # -- adding and deleting lines (the Edited lyrics only; Found keeps the lines as found)
    def delete_line(self, idx: int) -> None:
        if self.cand is None or not self.cand.synced or idx in self._marker_rows or \
                not 0 <= idx < len(self.orig_lines):
            return
        t, body = self.orig_lines[idx]
        if t is None:
            return
        key = lrcmod.pin_key(t)
        new = lrcmod.delete_line(self.cand.text, t, body)
        if new == self.cand.text:
            return
        self.cand.text = new
        for d in (self.pins, self.end_marks, self.ai_conf):
            d.pop(key, None)
        for st in (self.edited, self.ai_keys, self.ai_ends, self.sel_keys):
            st.discard(key)
        self.sel_line = None
        self._user_edited()
        self._recompute()

    def insert_line(self, idx: int, after: bool, text: str | None = None) -> None:
        """A new lyric line before / after row idx, timed halfway to its neighbour (move it after)."""
        if self.cand is None or not self.cand.synced or idx in self._marker_rows or \
                not 0 <= idx < len(self.orig_lines):
            return
        if text is None:
            from tkinter import simpledialog
            text = simpledialog.askstring(APP_TITLE, "Lyric line to insert (leave empty for a gap):",
                                          parent=self)
            if text is None:
                return
        times = [t for t, _ in self.orig_lines if t is not None]
        t, body = self.orig_lines[idx]
        j = times.index(t)
        if after:
            nxt = times[j + 1] if j + 1 < len(times) else min(t + 4.0, (self._duration() or t + 4.0))
            new_t = (t + nxt) / 2 if nxt > t else t + 0.5
        else:
            prv = times[j - 1] if j > 0 else max(0.0, t - 2.0)
            new_t = (prv + t) / 2 if t > prv else max(0.0, t - 0.5)
        taken = {lrcmod.pin_key(x) for x in times}
        while lrcmod.pin_key(new_t) in taken:
            new_t += 0.01
        self.cand.text = lrcmod.insert_line(self.cand.text, t, body, new_t, text.strip(), after)
        self._user_edited()
        self._recompute()

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
        self.ai_keys = set()
        self.end_marks, self.ai_ends, self.ai_conf = {}, set(), {}
        self._set_timing(0.0, 1.0)
        self._recompute()

    # ============================================================ timelines
    def _build_bars(self) -> None:
        """The four timelines as bars, each knowing which lyric (key) it is."""
        dur = self._duration()
        tidy = self.app.settings.tidy
        c = self.cand
        bars: dict[str, list] = {k: [] for k in LANES}
        bars["cur"] = [Bar(sg.start, sg.end, sg.text, None, words=sg.words, fill=(CUR_A, CUR_B)[i % 2])
                       for i, sg in enumerate(self.cur_segs)]
        src = self.found_text if self.found_text is not None else (c.text if c else None)
        fsegs = lrcmod.segments(tidy(src), dur) if src and lrcmod.is_synced(src) else []
        self.orig_segs = fsegs
        bars["new"] = [Bar(sg.start, sg.end, sg.text, lrcmod.pin_key(sg.start), words=sg.words,
                           fill=(NEW_A, NEW_B)[i % 2]) for i, sg in enumerate(fsegs)]
        keys = [b.key for b in bars["new"]]
        self.aligned_by_key = {}
        if self.aligned_text and c is not None and c.synced:
            for i, sg in enumerate(lrcmod.segments(tidy(self.aligned_text), dur)):
                k = keys[i] if i < len(keys) else None
                if k is not None:
                    self.aligned_by_key[k] = sg.start
                conf = self.aligned_conf.get(k, 0.8) if k else 0.5
                bars["aligned"].append(Bar(sg.start, sg.end, sg.text, k, words=sg.words, fill=heard_color(conf)))
        if c is not None and c.synced:
            rows = [j for j, (t, b) in enumerate(self.new_lines) if t is not None and b.strip()]
            for i, sg in enumerate(self.new_segs):
                row = rows[i] if i < len(rows) else None
                k = self._line_key(row) if row is not None else None
                if k is not None and k.startswith("end:"):
                    k = None
                fill = EDIT_AI if k in self.ai_keys else EDIT_USER if k in self.edited else (EDIT_A, EDIT_B)[i % 2]
                bars["edit"].append(Bar(sg.start, sg.end, sg.text, k, row=row, words=sg.words, fill=fill))
        self.lane_bars = bars
        if self.sel_lane and self.sel_keys:
            have = {b.key for b in bars.get(self.sel_lane, [])}
            self.sel_keys &= have

    def transfer(self, lane: str, keys=None) -> int:
        """Bring lyrics down from the Found or Aligned timeline into Edited (their times, and the
        aligner's gaps). keys=None: every lyric. Returns how many were brought down."""
        if lane not in ("new", "aligned"):
            return 0
        if keys is None:
            self.transfer_all(lane)
            return len(self.lane_bars.get(lane, []))
        valid = {lrcmod.pin_key(t) for t, b in self.orig_lines if t is not None and b.strip()}
        n = 0
        dur = self._duration() or 1e9
        for b in self.lane_bars.get(lane, []):
            k = b.key
            if not k or k not in keys or k not in valid:
                continue
            self.pins[k] = round(max(0.0, min(b.start, dur)), 2)
            self.edited.add(k)
            n += 1
            if lane == "aligned":
                self.ai_keys.add(k)
                if k in self.aligned_ends:
                    self.end_marks[k] = self.aligned_ends[k]
                    self.ai_ends.add(k)
            else:
                self.ai_keys.discard(k)
                self.end_marks.pop(k, None)
                self.ai_ends.discard(k)
        if n:
            self._user_edited()
            self._recompute()
        return n

    def transfer_all(self, lane: str) -> None:
        """Found → Edited: start over from the found timing. Aligned → Edited: the GPU's timing again
        (times you set yourself are kept)."""
        if lane == "new":
            self._reset_timing()
        elif lane == "aligned" and self.aligned_text:
            mine = self.edited - self.ai_keys
            self._overlay_ai({"pins": self.aligned_pins or dict(self.aligned_by_key), "ends": self.aligned_ends,
                              "confs": self.aligned_conf, "mode": self.aligned_mode})
            self.edited |= mine
            self._recompute()
        else:
            return
        self._user_edited()

    def _user_edited(self) -> None:
        """You changed the Edited timeline: it becomes this song's choice (shortly, once you pause)."""
        if self._auto_job is not None:
            try:
                self.after_cancel(self._auto_job)
            except tk.TclError:
                pass
        self._auto_job = self.after(900, self._auto_use_edited)

    def _auto_use_edited(self) -> None:
        self._cancel_auto()
        if self.song is None or self.cand is None or not self.cand.synced:
            return
        lv = self._levels()
        e = lv["edited"]
        if not e or any(lv[k] and lrcmod.same_lyrics(lv[k], e) for k in ("found", "aligned")):
            return                               # nothing of your own: the level it matches stands
        if self.current_level()[0] != "edited":
            self._apply()

    # ============================================================ GPU alignment
    def edited_segs(self) -> list:
        """The edited lyrics as lane segments (none while nothing has been changed)."""
        return self.new_segs if self.has_edits() else []

    def _edit_colours(self) -> list[str]:
        """Edited lane (blue): lines taken from Aligned indigo, your own times light blue, lines that only
        moved along (offset, stretch, interpolate, ripple) mid blue."""
        return [b.fill for b in self.lane_bars.get("edit", [])]

    def _ripple_on(self) -> bool:
        return bool(self.v_ripple.get()) if hasattr(self, "v_ripple") else False

    def _mode(self) -> str:
        return "ripple" if self._ripple_on() else self.warp_mode

    def _warping(self) -> bool:
        return bool(self.pins) and (self.interpolate or self._ripple_on())

    def _ripple_toggled(self) -> None:
        on = bool(self.v_ripple.get())
        self.app.settings.ripple = on
        if on:
            self.warp_mode = "ripple"
        elif self.warp_mode == "ripple":
            self.warp_mode = "slope"
        try:
            save_settings(self.app.settings)
        except OSError:
            pass
        self._recompute()

    def _attach_alignment(self, info) -> None:
        """A song aligned while finding: show its found lyrics as the original timing plus the aligner's
        pins (so the changes are visible and adjustable), and the words it heard."""
        try:
            st = State(self.app.settings.resolved_state_db())
            try:
                det = st.get_aligned_detail(info.path)
            finally:
                st.close()
        except Exception:
            det = None
        if not det or not det["ok"] or not det.get("text"):
            return
        orig = det.get("orig") or ""
        if not orig:                               # aligned by 1.2.1: rebuild from the kept original timing
            o = next((c for c in self.cands if c.key.endswith("-original") and c.text), None)
            if o is None:
                return
            orig = o.text
            det = dict(det, **_diff_as_edits(orig, det["text"]))
            det["orig"] = orig
        import hashlib
        det["stamp"] = hashlib.sha1((det["text"] or "").encode("utf-8", "replace")).hexdigest()[:16]
        for c in self.cands:
            if c.text and lrcmod.same_lyrics(c.text, det["text"]):
                c.text = orig                      # same result, shown as edits on the original timing
                c.ai_detail = det

    def auto_align(self) -> None:
        """Auto-align (GPU) button: align the lyrics in the New pane to this song's vocals."""
        from . import ai_align

        if self.song is None or self.cand is None or not self.cand.synced or not self.cand.text:
            messagebox.showinfo(APP_TITLE, "Choose synced lyrics in the New pane first.")
            return
        app = self.app
        if not app._ensure_ai():
            return                                       # refused, no GPU, or installing: try again after
        items = ai_align.lyric_lines(self.cand.text)
        if len(items) < 2:
            messagebox.showinfo(APP_TITLE, "These lyrics have fewer than 2 timed lines to align.")
            return
        n = (self._align_req[2] + 1) if self._align_req else 1
        self._align_req = (self.song.path, self.cand.key, n)
        svc = app._align_service()
        if svc.paused:
            self.v_ai_note.set("Waiting — aligning is paused (see the message)")
        elif svc.busy:
            self.v_ai_note.set("Waiting for the GPU to finish the song it's on…")
        else:
            self.v_ai_note.set("Starting…")
        self._ai_button()
        self._grab_keys()
        app.activity.set("autoalign", GPU + " Auto-align", None,
                         detail=os.path.basename(display_path(self.song.path)), stop=self.cancel_auto_align)
        svc.request_raw(self.song.path, [b for _, b in items], items[0][0], self._align_req,
                        duration=self._duration() or None)
        app._align_tick()

    def _ai_button(self, pct: float | None = None) -> None:
        """Auto-align is busy only for the song its request is for; any other song can be aligned."""
        mine = self._align_req is not None and self.song is not None and self._align_req[0] == self.song.path
        try:
            if mine:
                self.btn_ai.config(state="disabled",
                                   text=f"{GPU} Aligning… {pct:.0f}%" if pct is not None else f"{GPU} Aligning…")
            else:
                self.btn_ai.config(state="normal", text=GPU + " Auto-align (GPU)")
        except tk.TclError:
            pass

    def auto_align_progress(self, snap: dict | None, waiting: bool) -> None:
        """Called by the app a few times a second while this song's Auto-align request is pending."""
        mine = self._align_req is not None and self.song is not None and self._align_req[0] == self.song.path
        if not mine:
            return
        if snap is None:
            self._ai_button()
            if waiting:
                self.v_ai_note.set("Waiting for the GPU to finish the song it's on…")
            return
        pct = snap["frac"] * 100
        self._ai_button(pct)
        self.v_ai_note.set(f"{snap['label']} ({snap['stage_frac'] * 100:.0f}%) · about {_fmt_left(snap['left'])} left")

    def cancel_auto_align(self) -> None:
        """Forget the pending Auto-align request (its answer is ignored when it arrives)."""
        self._align_req = None
        self._ai_button()
        self.v_ai_note.set("Auto-align cancelled.")
        self.app.activity.done("autoalign")

    def align_result(self, e: dict) -> None:
        """The GPU's answer to Auto-align: heard words always shown; times applied if verified."""
        from . import ai_align

        if e.get("token") != self._align_req:
            return
        self._align_req = None
        self._ai_button()
        if self.song is None or self.cand is None or e.get("path") != self.song.path:
            self.app._log(f"Auto-align finished for {os.path.basename(display_path(e.get('path') or ''))} after you "
                          "moved to another song: not applied. Open it and press Auto-align again.")
            return
        res = e.get("result") or {}
        if not res.get("ok"):
            self.v_ai_note.set(f"Auto-align failed: {res.get('error', 'unknown error')}")
            return
        minimum = float(getattr(self.app.settings, "ai_align_min", 70))
        out = ai_align.evaluate(self.cand.text, res, self._duration() or None, minimum, self._mode())
        if not out.ok:
            self.v_ai_note.set(f"Not applied — {out.reason}.")
            return
        self._set_aligned({"text": out.text, "verify": out.verify, "pins": out.pin_map, "ends": out.ends,
                           "confs": out.line_conf, "mode": self._mode()})
        mine = self.edited - self.ai_keys                 # times you set yourself stay as they are
        for k in list(self.ai_keys):
            if k not in mine:
                self.pins.pop(k, None)
                self.edited.discard(k)
        self.ai_keys = set()
        for k, v in out.pin_map.items():
            if k in mine:
                continue
            self.pins[k] = v
            self.edited.add(k)
            self.ai_keys.add(k)
        for k in list(self.ai_ends):
            self.end_marks.pop(k, None)
        self.ai_ends = set()
        for k, v in (out.ends or {}).items():
            if k not in self.end_marks:
                self.end_marks[k] = v
                self.ai_ends.add(k)
        self.ai_conf = dict(out.line_conf or {})
        self.interpolate = True
        if not self._ripple_on():
            self.warp_mode = "slope"
        self._recompute()
        follow = "ripple around them" if self._ripple_on() else "are interpolated"
        kept = f"; kept your {len(mine)} edited time(s)" if mine else ""
        if out.partial:
            self.v_ai_note.set(f"{GPU} Partly aligned: {out.verify:.0f}% of words heard (below your {minimum:.0f}%), so "
                               f"only the {len(self.ai_keys)} of {out.lines} lines it was sure of were placed "
                               f"(purple); the others {follow}{kept}. Check them.")
        else:
            self.v_ai_note.set(f"{GPU} Aligned: {out.verify:.0f}% of words heard · {len(self.ai_keys)} of {out.lines} "
                               f"lines placed (purple), the others {follow}{kept}.")

    # ========================================================= checkpoints
    def _ov_time(self, x: float) -> float:
        w = max(1, self.ov.winfo_width())
        return max(0.0, min(self._duration(), x / w * self._duration()))

    def _ov_press(self, e) -> None:
        self._grab_keys()
        lane = self._lane_at(self.ov, e.y)
        if lane:
            self._lane_press(self.ov, e, lane, ctrl=bool(e.state & 0x4))
            return
        self._ov_drag = {"x": e.x, "moved": False}

    def _ov_motion(self, e) -> None:
        if self._band is not None or self._bar_drag is not None:
            self._lane_motion(e)
            return
        d = self._ov_drag
        if d is None or self.audio is None:
            return
        if d["moved"] or abs(e.x - d["x"]) > 4:
            d["moved"] = True
            self.cp_range = None
            self.player.seek(self._ov_time(e.x))

    def _ov_release(self, e) -> None:
        """A plain click (no drag) on the overview jumps there."""
        if self._band is not None or self._bar_drag is not None:
            self._lane_release(e)
            return
        d, self._ov_drag = self._ov_drag, None
        if d is None or d["moved"] or self.audio is None:
            return
        self.cp_range = None
        self.player.seek(self._ov_time(e.x))

    def _ov_click(self, e) -> None:
        """Add a checkpoint at x."""
        if not self._duration():
            return
        self.add_checkpoint(round(self._ov_time(e.x), 2))

    def add_checkpoint(self, t: float, slot: int | None = None) -> int:
        """A new checkpoint: in `slot` (key number - 1), or the first free number. Numbers follow the
        order they were made in, not their place in the song, and stay when others are deleted."""
        cps = self.checkpoints
        if slot is None:
            slot = next((i for i, x in enumerate(cps) if x is None), len(cps))
        while len(cps) <= slot:
            cps.append(None)
        cps[slot] = round(max(0.0, min(t, self._duration() or t)), 2)
        self.cp_custom = True
        self._checkpoints_changed()
        return slot

    def _nearest_checkpoint(self, x: float) -> int | None:
        if not self.checkpoints or not self._duration():
            return None
        w = max(1, self.ov.winfo_width())
        xs = {i: t / self._duration() * w for i, t in enumerate(self.checkpoints) if t is not None}
        if not xs:
            return None
        i = min(xs, key=lambda k: abs(xs[k] - x))
        return i if abs(xs[i] - x) <= max(12, w * 0.01) else None

    def _ov_ctrl_click(self, e) -> str:
        """Ctrl+click on the overview: on a lyric bar, add it to / take it out of the selection; on the
        waveform, remove the checkpoint under the pointer, or add one there."""
        self._grab_keys()
        self._ov_drag = None
        lane = self._lane_at(self.ov, e.y)
        if lane:
            self._lane_press(self.ov, e, lane, ctrl=True)
            return "break"
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
        if 0 <= i < len(self.checkpoints) and self.checkpoints[i] is not None:
            self.checkpoints[i] = None                 # the others keep their numbers
            while self.checkpoints and self.checkpoints[-1] is None:
                self.checkpoints.pop()
            self.cp_custom = True
            if self.cp_range is not None:
                self.player.pause()
                self.cp_range = None
            self.cp_cycle = -1
            self._checkpoints_changed()
        return "break"

    def _sc_press(self, e) -> None:
        self._grab_keys()
        if self._sc_tool(e):
            return
        edge = self._edge_at(e.x, e.y)
        if edge is not None:
            self._resize_start(edge)
            return
        lane = self._lane_at(self.sc, e.y)
        if lane:
            self._lane_press(self.sc, e, lane, ctrl=bool(e.state & 0x4))
            return
        if self.audio is not None:
            self._sc_drag = {"x": e.x, "pos": self.player.position(), "moved": False}

    def _sc_motion(self, e) -> None:
        if self._resize is not None:
            self._resize_move(e)
            return
        if self._band is not None or self._bar_drag is not None:
            self._lane_motion(e)
            return
        d = self._sc_drag
        if d is None or self.audio is None:
            return
        if d["moved"] or abs(e.x - d["x"]) > 3:
            d["moved"] = True
            w = max(1, self.sc.winfo_width())
            self.cp_range = None
            self.player.seek(d["pos"] - (e.x - d["x"]) / w * WINDOW)   # drag the waveform like tape

    def _sc_release(self, e) -> None:
        if self._resize is not None:
            self._resize_move(e, final=True)
            self._resize = None
            return
        if self._band is not None or self._bar_drag is not None:
            self._lane_release(e)
            return
        d, self._sc_drag = self._sc_drag, None
        if d is not None and not d["moved"]:
            self._sc_click(e)

    # -- resizing lyrics in the timeline
    def _sc_time(self, x: int) -> float:
        sc = getattr(self, "_scene", None)
        if not sc:
            return 0.0
        return (self.sc.canvasx(x) - sc["pad"]) / sc["pps"]

    def _edge_at(self, x: int, y: int):
        """(row in the Edited list, "start" | "end") when (x, y) is on a handle of a lyric in the Edited
        timeline (the only one that can be stretched), else None. Every lyric has a handle at its start;
        it has one at its end only when a gap (an empty line) or the end of the song follows it."""
        sc = getattr(self, "_scene", None)
        if not sc or self.song is None or not self.orig_lines:
            return None
        if not SC_EDIT_Y <= y <= SC_EDIT_Y + SC_LANE_H:
            return None
        wx, pad, pps = self.sc.canvasx(x), sc["pad"], sc["pps"]
        best = None
        rows = self.new_lines
        for b in self.lane_bars.get("edit", []):
            if b.row is None:
                continue
            nxt = b.row + 1
            gap_after = nxt >= len(rows) or not rows[nxt][1].strip()
            for edge, tx in (("end", b.end), ("start", b.start)):
                if edge == "end" and not gap_after:
                    continue
                d = abs(wx - (pad + tx * pps))
                if d <= EDGE_PX and (best is None or d < best[0]):
                    best = (d, b.row, edge)
        return (best[1], best[2]) if best else None

    # -- selecting lyric bars in a timeline, dragging them down into Edited or along it
    def _lane_at(self, cv, y: int) -> str | None:
        ys, h = (OV_LANE_Y, OV_LANE_H) if cv is self.ov else (SC_LANE_Y, SC_LANE_H)
        for k in ("new", "aligned", "edit"):
            if ys[k] - 1 <= y <= ys[k] + h + 1 and (k != "aligned" or self.lane_bars.get("aligned")):
                return k
        return None

    def _t_at(self, cv, x: int) -> float:
        return self._ov_time(x) if cv is self.ov else self._sc_time(x)

    def _x_world(self, cv, t: float) -> float:
        if cv is self.ov:
            dur = self._duration() or 1.0
            return t / dur * max(1, self.ov.winfo_width())
        sc = self._scene or {"pad": 0.0, "pps": 1.0}
        return sc["pad"] + t * sc["pps"]

    def _bar_at(self, cv, lane: str, x: int):
        t = self._t_at(cv, x)
        return next((b for b in self.lane_bars.get(lane, []) if b.key and b.start <= t < b.end), None)

    def _lane_press(self, cv, e, lane: str, ctrl: bool) -> None:
        b = self._bar_at(cv, lane, e.x)
        if self.sel_lane != lane:
            self.sel_lane, self.sel_keys = lane, set()
        was_selected = b is not None and b.key in self.sel_keys
        before = set(self.sel_keys)
        if b is not None:
            if ctrl:
                self.sel_keys ^= {b.key}
            elif b.key not in self.sel_keys:
                self.sel_keys = {b.key}
            snap = None
            if lane == "edit":
                self.player.pause()
                snap = {"state": (dict(self.pins), set(self.edited), dict(self.end_marks), set(self.ai_keys),
                                  set(self.ai_ends)),
                        "times": {x.key: x.start for x in self.lane_bars["edit"] if x.key}}
            self._bar_drag = {"cv": cv, "lane": lane, "x": e.x, "y": e.y, "moved": False, "ctrl": ctrl,
                              "t0": self._t_at(cv, e.x), "bar": b, "snap": snap, "last": 0.0,
                              "was_selected": was_selected, "before": before}
        else:
            base = set(self.sel_keys) if ctrl else set()
            if not ctrl:
                self.sel_keys = set()
            self._band = {"cv": cv, "lane": lane, "x0": e.x, "moved": False, "base": base, "ctrl": ctrl}
        self._selection_changed()

    def _lane_motion(self, e) -> None:
        if self._band is not None:
            d = self._band
            cv = d["cv"]
            if not d["moved"] and abs(e.x - d["x0"]) <= 3:
                return
            d["moved"] = True
            t0, t1 = sorted((self._t_at(cv, d["x0"]), self._t_at(cv, e.x)))
            self.sel_lane = d["lane"]
            self.sel_keys = d["base"] | {b.key for b in self.lane_bars.get(d["lane"], [])
                                         if b.key and b.end > t0 and b.start < t1}
            ys, h = (OV_LANE_Y, OV_LANE_H) if cv is self.ov else (SC_LANE_Y, SC_LANE_H)
            y0 = ys[d["lane"]]
            cv.delete("band")
            x0, x1 = self._x_world(cv, t0), self._x_world(cv, t1)
            cv.create_rectangle(x0, y0 - 3, x1, y0 + h + 3, outline=SEL_OUTLINE, dash=(3, 2), tags="band")
            self._selection_changed()
            return
        d = self._bar_drag
        if d is None:
            return
        cv = d["cv"]
        if not d["moved"] and abs(e.x - d["x"]) <= 4 and abs(e.y - d["y"]) <= 4:
            return
        if not d["moved"]:
            # what the drag is: along the timeline from Found / Aligned, or from a lyric not selected in
            # Edited, it draws a selection range; down from Found / Aligned, or a selected lyric in Edited,
            # it carries the selected lyrics
            along = abs(e.x - d["x"]) > abs(e.y - d["y"])
            if (d["lane"] in ("new", "aligned") and along) or (d["lane"] == "edit" and not d["was_selected"]):
                self._bar_drag = None
                self._band = {"cv": cv, "lane": d["lane"], "x0": d["x"], "moved": True, "ctrl": d["ctrl"],
                              "base": d["before"] if d["ctrl"] else set()}
                self._lane_motion(e)
                return
        d["moved"] = True
        if d["lane"] in ("new", "aligned"):           # dragging lyrics down into the Edited timeline
            ys, h = (OV_LANE_Y, OV_LANE_H) if cv is self.ov else (SC_LANE_Y, SC_LANE_H)
            dy = e.y - d["y"]
            cv.delete("ghost")
            for b in self.lane_bars.get(d["lane"], []):
                if b.key in self.sel_keys:
                    x0, x1 = self._x_world(cv, b.start), self._x_world(cv, b.end) - 1
                    cv.create_rectangle(x0, ys[d["lane"]] + dy, x1, ys[d["lane"]] + dy + h, outline=GHOST,
                                        dash=(4, 2), width=2, tags="ghost")
            over = ys["edit"] - 4 <= e.y <= ys["edit"] + h + 4
            if over:
                cv.create_rectangle(self._x_world(cv, 0.0), ys["edit"] - 2, self._x_world(cv, self._duration()),
                                    ys["edit"] + h + 2, outline=EDIT_ON, width=1, tags="ghost")
            return
        # dragging along the Edited timeline: the selected lyrics move with the pointer
        now = time.monotonic()
        d["pending"] = self._t_at(cv, e.x) - d["t0"]
        if now - d["last"] < 0.035:
            if not d.get("job"):
                d["job"] = self.after(40, lambda: self._move_selected(d))
            return
        self._move_selected(d)

    def _move_selected(self, d) -> None:
        d["job"] = None
        if d is not self._bar_drag or d.get("pending") is None or d.get("snap") is None:
            return
        d["last"] = time.monotonic()
        pins, edited, ends, ai_keys, ai_ends = d["snap"]["state"]
        self.pins, self.edited, self.end_marks = dict(pins), set(edited), dict(ends)
        self.ai_keys, self.ai_ends = set(ai_keys), set(ai_ends)
        dt, dur = d["pending"], self._duration() or 1e9
        valid = {lrcmod.pin_key(t) for t, b in self.orig_lines if t is not None and b.strip()}
        for k in self.sel_keys:
            t = d["snap"]["times"].get(k)
            if t is None or k not in valid:
                continue
            self.pins[k] = round(max(0.0, min(dur, t + dt)), 2)
            self.edited.add(k)
            self.ai_keys.discard(k)
        self._recompute()

    def _lane_release(self, e) -> None:
        if self._band is not None:
            d, self._band = self._band, None
            d["cv"].delete("band")
            if not d["moved"] and not d["ctrl"]:      # a plain click on empty timeline: jump there
                self.sel_keys, self.sel_lane = set(), None
                self._selection_changed()
                if self.audio is not None:
                    self.cp_range = None
                    self.player.seek(self._t_at(d["cv"], e.x))
            return
        d, self._bar_drag = self._bar_drag, None
        if d is None:
            return
        cv = d["cv"]
        cv.delete("ghost")
        if not d["moved"]:
            if not d["ctrl"]:                              # a click on a lyric: just it, and jump to it
                if self.sel_keys != {d["bar"].key}:
                    self.sel_keys = {d["bar"].key}
                    self._selection_changed()
                if self.audio is not None:
                    self.cp_range = None
                    self.player.seek(d["bar"].start)
            return
        if d["lane"] in ("new", "aligned"):
            ys, h = (OV_LANE_Y, OV_LANE_H) if cv is self.ov else (SC_LANE_Y, SC_LANE_H)
            if ys["edit"] - 4 <= e.y <= ys["edit"] + h + 4:
                n = self.transfer(d["lane"], set(self.sel_keys))
                if n:
                    self.v_ai_note.set(f"Brought {n} lyric(s) down from {LANE_LABEL[d['lane']]} into Edited.")
            return
        if d.get("job"):
            try:
                self.after_cancel(d["job"])
            except tk.TclError:
                pass
        d["pending"] = self._t_at(cv, e.x) - d["t0"]
        self._bar_drag = d
        self._move_selected(d)
        self._bar_drag = None
        self._user_edited()
        self._save_prefs_later()

    def _selection_changed(self) -> None:
        self._lanes_ver += 1
        self._draw_overview()
        if self.audio is not None:
            self._draw_scroll(self._disp_pos)

    def _resize_start(self, edge) -> None:
        self.player.pause()
        self._resize = {"edge": edge, "snap": (dict(self.pins), set(self.edited), dict(self.end_marks),
                                               set(self.ai_keys), set(self.ai_ends)),
                        "times": [t for t, _ in self.new_lines], "texts": [b for _, b in self.new_lines],
                        "last": 0.0, "pending": None}

    def _resize_move(self, e, final: bool = False) -> None:
        r = self._resize
        t = round(self._sc_time(e.x), 2)
        r["pending"] = t
        now = time.monotonic()
        if not final and now - r["last"] < 0.035:          # ~30 updates a second while dragging
            if not r.get("job"):
                r["job"] = self.after(40, lambda: self._resize_apply(r))
            return
        self._resize_apply(r)
        if final:
            self._user_edited()
            self._save_prefs_later()

    def _resize_apply(self, r) -> None:
        r["job"] = None
        if r is not self._resize or r["pending"] is None:
            return
        r["last"] = time.monotonic()
        pins, edited, ends, ai_keys, ai_ends = r["snap"]
        self.pins, self.edited, self.end_marks = dict(pins), set(edited), dict(ends)
        self.ai_keys, self.ai_ends = set(ai_keys), set(ai_ends)
        idx, edge = r["edge"]
        changes = resize_lyric(r["times"], r["texts"], idx, edge, r["pending"], self._duration() or 1e9)
        for i, v in changes.items():
            if not isinstance(i, int):
                continue
            key = self._line_key(i)
            if key is None:
                continue
            if key.startswith("end:"):
                k = key[4:]
                self.end_marks[k] = v
                self.ai_ends.discard(k)
            else:
                self.pins[key] = v
                self.edited.add(key)
                self.ai_keys.discard(key)
        new_end = changes.get(("end", idx))
        if new_end is not None:                            # a new gap right after the lyric
            k = self._line_key(idx)
            if k and not k.startswith("end:"):
                self.end_marks[k] = new_end
                self.ai_ends.discard(k)
        if self._warping():                                # keep the warp from moving lines past the gap
            stop = changes.get(("stop", idx))
            if isinstance(stop, int):
                k = self._line_key(stop)
                if k and not k.startswith("end:") and k not in self.pins:
                    self.pins[k] = r["times"][stop]
        self._recompute()

    def _sc_tool(self, e) -> bool:
        """A press on one of the small buttons above the needle."""
        if self.audio is None:
            return False
        x = self.sc.canvasx(e.x)
        for item in self.sc.find_overlapping(x - 1, e.y - 1, x + 1, e.y + 1):
            tags = self.sc.gettags(item)
            if "tool_target" in tags:
                self.next_line_to_needle()
                return True
            if "tool_copy" in tags:
                self.copy_needle_time()
                return True
        return False

    def next_line_to_needle(self) -> None:
        """⌖: give the next lyric (the first starting after the needle) the needle's time."""
        if self.audio is None:
            return
        t = self.player.position()
        nxt = next((b for b in self.lane_bars.get("edit", []) if b.row is not None and b.start > t + 0.01), None)
        if nxt is None:
            self.v_ai_note.set("No lyric after the needle.")
            return
        self.set_line_time(nxt.row, round(t, 2))
        self.v_ai_note.set(f"⌖ “{nxt.text[:40]}” now starts at {lrcmod.format_ts(t)}.")

    def copy_needle_time(self) -> None:
        """⧉: copy the needle's time, to paste into a lyric's timestamp."""
        t = self.player.position() if self.audio is not None else 0.0
        txt = lrcmod.format_ts(t)
        try:
            self.clipboard_clear()
            self.clipboard_append(txt)
        except tk.TclError:
            return
        self.v_ai_note.set(f"⧉ Copied {txt}: click a timestamp and paste it (Ctrl+V).")

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
        if self.audio is None or i < 0:
            return
        if i >= len(self.checkpoints) or self.checkpoints[i] is None:
            self.add_checkpoint(self.player.position(), slot=i)    # an unset key: set it at the needle
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
        idx = [i for i, t in enumerate(self.checkpoints) if t is not None]
        if idx:
            self.play_checkpoint(next((i for i in idx if i > self.cp_cycle), idx[0]))

    def cycle_checkpoint(self) -> None:
        """` / ~: the next checkpoint after the needle in the song (then round to the first)."""
        if self.audio is None:
            return
        pos = self.player.position()
        if self.cp_range is not None and 0 <= self.cp_cycle < len(self.checkpoints) and \
                self.checkpoints[self.cp_cycle] is not None:
            pos = self.checkpoints[self.cp_cycle]           # playing one: go on from it, not from its lead-in
        order = sorted((t, i) for i, t in enumerate(self.checkpoints) if t is not None)
        if not order:
            return
        nxt = next(((t, i) for t, i in order if t > pos + 0.05), order[0])
        self.play_checkpoint(nxt[1])

    def _draw_cp_buttons(self) -> None:
        for b in self._cp_buttons:
            b.destroy()
        self._cp_buttons = []
        self.cpbar.delete("all")
        dur, w = self._duration(), max(1, self.cpbar.winfo_width())
        if not dur:
            return
        for i, t in enumerate(self.checkpoints):
            if t is None:
                continue
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

    def _edit_volume(self, e=None) -> None:
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
        self._vol_close = close
        self._vol_serial = getattr(e, "serial", None)
        self._watch_clicks()

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
                 "pins": dict(self.pins), "edited": sorted(self.edited), "interpolate": self.interpolate,
                 "ai": sorted(self.ai_keys & set(self.pins)), "mode": self.warp_mode,
                 "ends": dict(self.end_marks), "ai_ends": sorted(self.ai_ends & set(self.end_marks)),
                 "ai_conf": dict(self.ai_conf)}
        ad = getattr(self.cand, "ai_detail", None) if self.cand else None
        if ad and ad.get("stamp"):
            prefs["ai_stamp"] = ad["stamp"]        # this alignment has been shown (and maybe adjusted)
        elif self.aligned_text:                    # an Auto-align result: keep its timeline
            prefs["aligned"] = {"text": self.aligned_text, "verify": self.aligned_verify,
                                "pins": self.aligned_pins, "ends": self.aligned_ends,
                                "confs": self.aligned_conf, "mode": self.aligned_mode}
        if self.cand is not None and self.found_text is not None and self.cand.text != self.found_text:
            prefs["base"] = self.cand.text         # lines you added or deleted
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
        self._ov_lane_items = {k: [] for k in LANES}
        self._ov_lane_fill = {k: [] for k in LANES}
        self._ov_active = {k: -1 for k in LANES}
        self._draw_lane_labels()
        w, h = max(2, c.winfo_width()), int(c.cget("height"))
        dur = self._duration()
        if not dur:
            return
        mid, amp = 20, 18
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
        for which in LANES:
            y0 = OV_LANE_Y[which]
            for b in self.lane_bars.get(which, []):
                x0, x1 = b.start / dur * w, max(b.start / dur * w + 1, b.end / dur * w - 1)
                item = c.create_rectangle(x0, y0, x1, y0 + OV_LANE_H, fill=b.fill, outline="")
                self._ov_lane_items[which].append(item)
                self._ov_lane_fill[which].append(b.fill)
                for ws, _, _ in b.words[1:]:
                    xw = ws / dur * w
                    c.create_line(xw, y0, xw, y0 + OV_LANE_H, fill=BG)
        if self.sel_lane and self.sel_keys:
            y0 = OV_LANE_Y[self.sel_lane]
            for b in self.lane_bars.get(self.sel_lane, []):
                if b.key in self.sel_keys:
                    c.create_rectangle(b.start / dur * w, y0 - 1, max(b.start / dur * w + 2, b.end / dur * w - 1),
                                       y0 + OV_LANE_H + 1, outline=SEL_OUTLINE, width=2)
        _draw_ruler(c, 0.0, dur, lambda t: t / dur * w, OV_RULER_Y, w)
        for i, t in enumerate(self.checkpoints):
            if t is None:
                continue
            x = t / dur * w
            c.create_line(x, 0, x, h, fill=CP_COLOR, dash=(3, 2))
            c.create_text(x + 3, 2, text=str(i + 1) if i < 9 else "", anchor="nw", fill=CP_COLOR,
                          font=("TkDefaultFont", 8, "bold"))
        self._ov_pos = c.create_line(0, 0, 0, h, fill=PLAYHEAD, width=2)

    def _draw_lane_labels(self) -> None:
        """The timelines' names, left of the overview and the scrolling waveform."""
        shown = {"cur": bool(self.lane_bars.get("cur")), "new": True,
                 "aligned": bool(self.lane_bars.get("aligned")), "edit": True}
        for cv, ys, h in ((self.ov_lbl, OV_LANE_Y, OV_LANE_H), (self.sc_lbl, SC_LANE_Y, SC_LANE_H)):
            cv.delete("all")
            for k in LANES:
                if not shown[k]:
                    continue
                col = {"cur": CUR_A, "new": NEW_A, "aligned": HEARD_HI, "edit": EDIT_USER}[k]
                cv.create_rectangle(LABEL_W - 5, ys[k], LABEL_W - 2, ys[k] + h, fill=col, outline="")
                cv.create_text(LABEL_W - 8, ys[k] + h / 2, text=LANE_LABEL[k], anchor="e", fill=RULER_FG,
                               font=("TkDefaultFont", 7 if cv is self.ov_lbl else 8))
            if cv is self.sc_lbl:
                cv.create_text(LABEL_W - 8, 30, text="Waveform", anchor="e", fill=RULER_TICK,
                               font=("TkDefaultFont", 7))

    # ------------------------------------------- scrolling waveform (pre-rendered, scrolled view)
    # The waveform is rendered once into image tiles and the lyric lanes are created once, in
    # "world" coordinates (x = pad + seconds * px_per_second). Each frame only scrolls the canvas
    # view and moves a few items (playhead, progress fill, current word) instead of redrawing.
    TILE = 2048
    WAVE_H = 60

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
            _draw_ruler(c, 0.0, dur, lambda t, sc=sc: sc["pad"] + t * sc["pps"], SC_RULER_Y, sc["total"],
                        step=1.0 if pps >= 55 else 2.0)
            sc["center"] = c.create_line(0, 0, 0, h, fill=PLAYHEAD, width=2, tags="center")
            # two small buttons riding above the needle: move the next lyric here · copy this time
            sc["tools"] = []
            for name, glyph in (("tool_target", "⌖"), ("tool_copy", "⧉")):
                bg = c.create_rectangle(0, 0, 0, 0, fill="#2b3442", outline=PLAYHEAD, tags=("tool", name))
                tx = c.create_text(0, 0, text=glyph, fill="#ffffff", font=("TkDefaultFont", 10, "bold"),
                                   tags=("tool", name))
                sc["tools"].append((bg, tx))
            self._scene = sc
        lanes = self._lanes_ver
        if sc["lanes"] != lanes:
            self._build_lanes(sc)
            sc["lanes"] = lanes
        return sc

    def _build_lanes(self, sc: dict) -> None:
        c = self.sc
        c.delete("lane")
        pad, pps = sc["pad"], sc["pps"]
        sc["lane_items"] = {}
        for which in LANES:
            bars = self.lane_bars.get(which, [])
            segs = bars
            y0 = SC_LANE_Y[which]
            on = {"cur": CUR_ON, "new": NEW_ON, "aligned": HEARD_HI, "edit": EDIT_ON}[which]
            for b in bars:
                x0, x1 = pad + b.start * pps, pad + b.end * pps - 1
                c.create_rectangle(x0, y0, x1, y0 + SC_LANE_H, fill=b.fill, outline="", tags=("lane", "lanebg"))
            prog = c.create_rectangle(0, y0, 0, y0 + SC_LANE_H, fill=on, outline="", state="hidden",
                                      tags=("lane", "laneprog"))
            for b in bars:
                for ws, _, _ in b.words:
                    if ws > b.start:
                        x = pad + ws * pps
                        c.create_line(x, y0, x, y0 + SC_LANE_H, fill=BG, tags=("lane", "lanediv"))
            word = c.create_rectangle(0, y0, 0, y0 + SC_LANE_H, outline="#ffffff", width=1, state="hidden",
                                      tags=("lane", "laneword"))
            texts = []
            for b in bars:
                x0, x1 = pad + b.start * pps, pad + b.end * pps - 1
                room = int((x1 - x0) / 7)
                if room > 2 and b.text.strip():
                    txt = b.text if len(b.text) <= room else b.text[: max(1, room - 1)] + "…"
                    texts.append(c.create_text(x0 + 3, y0 + 9, text=txt, anchor="w",
                                               fill="#ffffff" if which in ("edit", "aligned") else "#111",
                                               font=("TkDefaultFont", 8), tags=("lane", "lanetext")))
                else:
                    texts.append(None)
            # label that sticks to the left edge for a line that started before the view
            sticky = c.create_text(0, y0 + 9, text="", anchor="w",
                                   fill="#ffffff" if which in ("edit", "aligned") else "#111",
                                   font=("TkDefaultFont", 8), state="hidden", tags=("lane", "lanetext"))
            sc["lane_items"][which] = (segs, prog, word, texts, sticky)
        if self.sel_lane and self.sel_keys:
            y0 = SC_LANE_Y[self.sel_lane]
            for b in self.lane_bars.get(self.sel_lane, []):
                if b.key in self.sel_keys:
                    c.create_rectangle(pad + b.start * pps, y0 - 1, pad + b.end * pps - 1, y0 + SC_LANE_H + 1,
                                       outline=SEL_OUTLINE, width=2, tags=("lane", "lanesel"))
        sc["sticky_hidden"] = {}
        c.tag_raise("laneprog", "lanebg")
        c.tag_raise("lanediv")
        c.tag_raise("laneword")
        c.tag_raise("lanetext")
        c.tag_raise("lanesel")
        c.tag_raise("center")

    def _tile(self, sc: dict, k: int) -> None:
        env = sc["env"]
        a, b = k * self.TILE, min(len(env), (k + 1) * self.TILE)
        if b <= a or k in sc["tiles"]:
            return
        import numpy as np

        h, mid, amp = self.WAVE_H, 30, 27
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
        for (bg, tx), dx in zip(sc.get("tools", []), (-13, 13)):
            c.coords(bg, cx + dx - 10, 1, cx + dx + 10, 19)
            c.coords(tx, cx + dx, 10)
        c.tag_raise("tool")
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
                    c.coords(sticky, left + 3, SC_LANE_Y[which] + 9)
                else:
                    c.itemconfigure(sticky, state="hidden")
                c.itemconfigure(texts[j], state="hidden")
                sc["sticky_hidden"][which] = j
            else:
                c.itemconfigure(sticky, state="hidden")
                sc["sticky_hidden"][which] = None
            i = lrcmod.segment_at(segs, pos)
            if i < 0 or not segs[i].text.strip():
                c.itemconfigure(prog, state="hidden")
                c.itemconfigure(word, state="hidden")
                continue
            sg = segs[i]
            y0 = SC_LANE_Y[which]
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
        for k in LANES:
            bars = self.lane_bars.get(k, [])
            if not bars:
                continue
            b = next((x for x in bars if x.start <= t < x.end), None)
            if b is not None:
                word = next((wd for ws, we, wd in b.words if ws <= t < we), "")
                extra = ""
                if k == "aligned" and b.key in self.aligned_conf:
                    extra = f"  ({self.aligned_conf[b.key] * 100:.0f}% sure)"
                parts.append(f"{LANE_LABEL[k]}: {b.text}" + (f"   [{word}]" if word else "") + extra)
            else:
                parts.append(f"{LANE_LABEL[k]}: —")
        eb = next((x for x in self.lane_bars.get("edit", []) if x.start <= t < x.end), None)
        if eb is not None and eb.key:
            if eb.key in self.ai_keys:
                parts.append(f"Edited line from the Aligned timeline ({GPU})")
            elif eb.key in self.edited:
                parts.append(f"Edited line: your time ({HAND})")
            elif self.has_edits():
                parts.append("Edited line: moved along with the others")
        cv = e.widget
        lane = self._lane_at(cv, e.y)
        if cv is self.sc:
            edge = self._edge_at(e.x, e.y)
            self.sc.configure(cursor="sb_h_double_arrow" if edge else ("hand2" if lane else ""))
            if edge:
                parts.append("Drag to stretch this lyric" + (" (its end: a gap follows)" if edge[1] == "end" else
                                                              " (its start)"))
        if lane in ("new", "aligned"):
            parts.append("Click: select · Ctrl+click: add/remove · drag along the timeline: select a range · "
                         "drag selected lyrics down onto Edited: use their times")
        elif lane == "edit":
            parts.append("Click: select · Ctrl+click: add/remove · drag selected lyrics left/right to move them · "
                         "drag a lyric's start (or its end before a gap) to stretch it")
        else:
            parts.append("Click: jump · drag: scrub" + (" · Ctrl+click: add/remove checkpoint" if cv is self.ov
                                                       else " · ⌖ next lyric to the needle · ⧉ copy the time"))
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
        if ch in ("grave", "asciitilde", "dead_grave"):
            self.cycle_checkpoint()
            return True
        if ch in ("Up", "Down") and cls == "Treeview":
            return False                    # the song list moves its own selection
        if ch in ("Up", "w", "W", "Down", "s", "S"):
            self.step_song(-1 if ch in ("Up", "w", "W") else 1)
            return True
        return False

    def step_song(self, d: int) -> None:
        """Up / W: previous song in the list · Down / S: next song (into and across groups)."""
        order = []

        def walk(node):
            for ch in self.tree.get_children(node):
                if ch.startswith("grp:"):
                    walk(ch)
                else:
                    order.append(ch)
        walk("")
        if not order:
            return
        cur = self.song.path if self.song is not None and self.song.path in order else None
        if cur is None:
            sel = [x for x in self.tree.selection() if not x.startswith("grp:")]
            cur = sel[0] if sel and sel[0] in order else None
        i = order.index(cur) + d if cur is not None else (0 if d > 0 else len(order) - 1)
        if not 0 <= i < len(order):
            return
        k = order[i]
        parent = self.tree.parent(k)
        while parent:
            self.tree.item(parent, open=True)
            parent = self.tree.parent(parent)
        self.tree.selection_set(k)
        self.tree.focus(k)
        self.tree.see(k)

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
                for which, on in (("cur", CUR_ON), ("new", NEW_ON), ("aligned", HEARD_HI), ("edit", EDIT_ON)):
                    self._ov_highlight(which, self.lane_bars.get(which, []), pos, on)
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

    def _ov_highlight(self, which, bars, pos, on) -> None:
        i = next((j for j, b in enumerate(bars) if b.start <= pos < b.end), -1)
        prev = self._ov_active.get(which, -1)
        if i == prev:
            return
        items = self._ov_lane_items.get(which, [])
        fills = self._ov_lane_fill.get(which, [])
        if 0 <= prev < len(items) and items[prev] is not None:
            self.ov.itemconfig(items[prev], fill=fills[prev] if prev < len(fills) else NEW_A)
        if 0 <= i < len(items) and items[i] is not None:
            self.ov.itemconfig(items[i], fill=on)
        self._ov_active[which] = i

    # ============================================================= actions
    def _apply(self) -> None:
        """Record the chosen lyrics for this file; the bottom bar writes them."""
        self._cancel_auto()
        if self.song is None or self.cand is None:
            return
        c = self.cand
        text = self.new_text if c.text is not None else None
        label = c.label
        if c.synced and (abs(self.offset) > 1e-6 or abs(self.factor - 1) > 1e-9 or self.pins):
            label += f" (offset {self.offset * 1000:+.0f} ms, stretch {self.factor * 100:.2f}%"
            label += (f", interpolated from {len(self.pins)} pins)" if self.interpolate
                      else f", {len(self.pins)} pinned)" if self.pins else ")")
        mine = bool(self.edited - self.ai_keys) or bool(set(self.end_marks) - self.ai_ends) or \
            abs(self.offset) > 1e-6 or abs(self.factor - 1) > 1e-9
        self._run_action(lambda s: review.choose(self.song.path, text, label, s, c.path), "chosen",
                         hand=bool(c.synced and mine), level="edited")

    def _lock(self) -> None:
        self._cancel_auto()
        if self.song is not None:
            self._run_action(lambda s: review.lock_current(self.song.path, s), "locked")

    def _clear(self) -> None:
        self._cancel_auto()
        if self.song is not None:
            self._run_action(lambda s: review.clear_choice(self.song.path, s), "cleared")

    def _run_action(self, fn, status, hand: bool = False, level: str = "") -> None:
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
                                     "msg": msg or "", "choice": choice, "hand": hand,
                                     "level": level or ("keep" if status == "locked" else "")})
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
        act = getattr(self.app, "activity", None)
        if act is not None:
            act.set("search", "Searching LRCLIB", None, detail=meta.title)

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
        for job in ("_auto_job", "_prefs_job", "_recompute_job"):
            j = getattr(self, job, None)
            if j is not None:
                try:
                    self.after_cancel(j)
                except tk.TclError:
                    pass
                setattr(self, job, None)
