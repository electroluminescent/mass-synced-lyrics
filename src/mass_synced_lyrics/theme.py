"""Look and feel: Sun Valley (sv-ttk) light/dark theme plus matching colours for the
classic Tk widgets (text panes, lists, flat buttons, split handles) that ttk themes don't cover."""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

PALETTES = {
    "light": {
        "bg": "#fafafa", "surface": "#ffffff", "fg": "#1b1b1b", "muted": "#6b6b6b", "border": "#e3e3e3",
        "accent": "#005fb8", "accent_fg": "#ffffff", "accent_hover": "#1a6fc2",
        "success": "#1e8e3e", "success_fg": "#ffffff", "success_hover": "#177a34",
        "neutral": "#f0f0f0", "neutral_fg": "#1b1b1b", "neutral_hover": "#e6e6e6",
        "select": "#cfe4fa", "sash": "#d6d6d6",
        "now": "#d3f2cf", "pick": "#cfe8ff", "ts_pinned": "#fff59d", "ts_pinned_fg": "#5d4a00",
        "edited": "#ffe14d", "edited_fg": "#3d3000", "pin": "#c0392b",
        "rows": {"c_local": ("#fff2cc", "#000000"), "c_online": ("#deeaff", "#000000"),
                 "c_custom": ("#ecdfff", "#000000"), "c_keep": ("#ffffff", "#7a7a7a"),
                 "c_done": ("#dbf3e2", "#1e5631"), "c_problem": ("#ffe1e1", "#7a1f1f"),
                 "group": ("#eceef1", "#1b1b1b"), "subgroup": ("#f5f6f8", "#1b1b1b")},
    },
    "dark": {
        "bg": "#1c1c1c", "surface": "#262626", "fg": "#f3f3f3", "muted": "#a3a3a3", "border": "#3a3a3a",
        "accent": "#4cc2ff", "accent_fg": "#000000", "accent_hover": "#6fcfff",
        "success": "#2ea043", "success_fg": "#ffffff", "success_hover": "#37b24d",
        "neutral": "#333333", "neutral_fg": "#f3f3f3", "neutral_hover": "#3d3d3d",
        "select": "#1f4a75", "sash": "#3a3a3a",
        "now": "#244b2b", "pick": "#1e3a5c", "ts_pinned": "#5c5200", "ts_pinned_fg": "#fff3b0",
        "edited": "#8a7600", "edited_fg": "#fffbe0", "pin": "#ff6b5e",
        "rows": {"c_local": ("#4a3d14", "#fff3d1"), "c_online": ("#1d3453", "#dfeaff"),
                 "c_custom": ("#3b2a55", "#efe6ff"), "c_keep": ("#262626", "#9a9a9a"),
                 "c_done": ("#1d3d27", "#c9f2d6"), "c_problem": ("#4d2020", "#ffd6d6"),
                 "group": ("#303030", "#f3f3f3"), "subgroup": ("#2a2a2a", "#f3f3f3")},
    },
}

_state = {"mode": "light", "sv": False}
_registry: list[tuple[tk.Misc, str, dict]] = []


def palette() -> dict:
    return PALETTES[_state["mode"]]


def mode() -> str:
    return _state["mode"]


def has_font(name: str) -> bool:
    try:
        return name in tkfont.names()
    except tk.TclError:
        return False


def font(kind: str = "body"):
    """Named Sun Valley fonts when available (Segoe UI Variable on Windows 11)."""
    names = {"body": "SunValleyBodyFont", "strong": "SunValleyBodyStrongFont", "caption": "SunValleyCaptionFont",
             "large": "SunValleyBodyLargeFont", "subtitle": "SunValleySubtitleFont", "title": "SunValleyTitleFont"}
    n = names.get(kind, "SunValleyBodyFont")
    if has_font(n):
        return n
    fallback = {"body": ("TkDefaultFont", 10), "strong": ("TkDefaultFont", 10, "bold"),
                "caption": ("TkDefaultFont", 8), "large": ("TkDefaultFont", 12),
                "subtitle": ("TkDefaultFont", 14, "bold"), "title": ("TkDefaultFont", 18, "bold")}
    return fallback[kind]


def apply(root: tk.Misc, mode_name: str = "light") -> dict:
    mode_name = mode_name if mode_name in PALETTES else "light"
    _state["mode"] = mode_name
    try:
        import sv_ttk

        sv_ttk.set_theme(mode_name, root)
        _state["sv"] = True
    except Exception:
        _state["sv"] = False
    p = palette()
    st = ttk.Style(root)
    st.configure("Treeview", rowheight=30)
    st.configure("App.Title.TLabel", font=font("subtitle"))
    st.configure("Section.TLabel", font=font("strong"))
    st.configure("Muted.TLabel", foreground=p["muted"])
    st.configure("Caption.TLabel", foreground=p["muted"], font=font("caption"))
    st.configure("Status.TLabel", foreground=p["muted"])
    try:
        root.configure(background=p["bg"])
    except tk.TclError:
        pass
    restyle_all()
    return p


def card(parent, padding=14) -> ttk.Frame:
    style = "Card.TFrame" if _state["sv"] else "TFrame"
    return ttk.Frame(parent, style=style, padding=padding)


# ------------------------------------------------------------- classic widgets
def register(widget: tk.Misc, kind: str, **opts) -> tk.Misc:
    _registry.append((widget, kind, opts))
    _style_one(widget, kind, opts)
    return widget


def restyle_all() -> None:
    alive = []
    for w, kind, opts in _registry:
        try:
            if w.winfo_exists():
                _style_one(w, kind, opts)
                alive.append((w, kind, opts))
        except tk.TclError:
            pass
    _registry[:] = alive


def _style_one(w: tk.Misc, kind: str, opts: dict) -> None:
    p = palette()
    try:
        if kind == "text":
            w.configure(background=p["surface"], foreground=p["fg"], insertbackground=p["fg"],
                        selectbackground=p["select"], selectforeground=p["fg"], relief="flat", borderwidth=0,
                        highlightthickness=1, highlightbackground=p["border"], highlightcolor=p["accent"],
                        padx=10, pady=8)
            if not opts.get("mono"):
                w.configure(font=font("large") if opts.get("large") else font("body"))
        elif kind == "listbox":
            w.configure(background=p["surface"], foreground=p["fg"], selectbackground=p["select"],
                        selectforeground=p["fg"], relief="flat", borderwidth=0, highlightthickness=1,
                        highlightbackground=p["border"], highlightcolor=p["accent"], activestyle="none",
                        font=font("body"))
        elif kind == "paned":
            w.configure(background=p["sash"], sashrelief="flat", borderwidth=0)
        elif kind == "bg":
            w.configure(background=p["bg"])
        elif kind == "surface":
            w.configure(background=p["surface"])
        elif kind == "flat":
            set_flat_kind(w, getattr(w, "_flat_kind", "neutral"))
        elif kind == "pill":
            bg, fg = p["rows"].get(opts["tag"], (p["neutral"], p["fg"]))
            w.configure(background=bg, foreground=fg, relief="flat", borderwidth=0, padx=8, pady=2,
                        highlightthickness=1, highlightbackground=p["border"], font=font("caption"))
        elif kind == "menu":
            w.configure(background=p["surface"], foreground=p["fg"], activebackground=p["select"],
                        activeforeground=p["fg"], relief="flat", borderwidth=1, font=font("body"))
        elif kind == "callback":
            opts["fn"]()
    except tk.TclError:
        pass


def flat_button(parent, text: str, command, kind: str = "neutral", **kw) -> tk.Button:
    """A flat, web-style button whose colour says what it is (neutral / accent / success)."""
    b = tk.Button(parent, text=text, command=command, relief="flat", borderwidth=0, highlightthickness=0,
                  cursor="hand2", padx=kw.pop("padx", 14), pady=kw.pop("pady", 6), **kw)
    b._flat_kind = kind
    b.bind("<Enter>", lambda e: _hover(b, True), add="+")
    b.bind("<Leave>", lambda e: _hover(b, False), add="+")
    register(b, "flat")
    return b


def set_flat_kind(b: tk.Button, kind: str) -> None:
    p = palette()
    b._flat_kind = kind
    bg, fg = {"accent": (p["accent"], p["accent_fg"]), "success": (p["success"], p["success_fg"])}.get(
        kind, (p["neutral"], p["neutral_fg"]))
    b.configure(background=bg, foreground=fg, activebackground=bg, activeforeground=fg,
                disabledforeground=p["muted"], font=font("strong") if kind != "neutral" else font("body"))


def _hover(b: tk.Button, inside: bool) -> None:
    p = palette()
    kind = getattr(b, "_flat_kind", "neutral")
    if str(b.cget("state")) == "disabled":
        return
    base = {"accent": "accent", "success": "success"}.get(kind, "neutral")
    b.configure(background=p[base + "_hover"] if inside else p[base])


def paned(parent, orient="horizontal") -> tk.PanedWindow:
    w = tk.PanedWindow(parent, orient=orient, sashwidth=8, opaqueresize=True, sashpad=0, showhandle=False)
    return register(w, "paned")


# ------------------------------------------------------------------ hover hints
class _Hint:
    """Small delayed tooltip shown under a widget while the pointer rests on it."""

    def __init__(self, widget: tk.Misc, text, delay: int = 450):
        self.widget, self.text, self.delay = widget, text, delay
        self._after = None
        self.tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None) -> None:
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self) -> None:
        self._after = None
        text = self.text() if callable(self.text) else self.text
        if not text:
            return
        p = palette()
        try:
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            try:
                self.tip.wm_attributes("-topmost", True)
            except tk.TclError:
                pass
            tk.Label(self.tip, text=text, justify="left", background=p["surface"], foreground=p["fg"],
                     relief="solid", borderwidth=1, padx=10, pady=6, wraplength=380,
                     font=font("body")).pack()
            self.tip.update_idletasks()
            x = self.widget.winfo_rootx()
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            sw = self.widget.winfo_screenwidth()
            x = max(0, min(x, sw - self.tip.winfo_reqwidth() - 4))
            if y + self.tip.winfo_reqheight() > self.widget.winfo_screenheight():
                y = self.widget.winfo_rooty() - self.tip.winfo_reqheight() - 6
            self.tip.wm_geometry(f"+{x}+{y}")
        except tk.TclError:
            self.tip = None

    def _hide(self, _e=None) -> None:
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


def hint(widget: tk.Misc, text) -> tk.Misc:
    """Attach a hover hint (text or a function returning text) and return the widget."""
    widget._hint = _Hint(widget, text)
    return widget
