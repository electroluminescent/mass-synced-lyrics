"""Header strip with one row per operation that is still running (Find, Apply, GPU aligning, add-on
install, …): a title, a progress bar (determinate when the amount of work is known), a detail line and
a Stop button. Rows appear when an operation starts and disappear when it ends, so ongoing work is
always visible from either tab."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import theme


class Activity:
    def __init__(self, parent: tk.Misc):
        self.frame = ttk.Frame(parent)
        self.rows: dict[str, dict] = {}

    def is_active(self, key: str) -> bool:
        return key in self.rows

    def keys(self) -> list[str]:
        return list(self.rows)

    def set(self, key: str, title: str | None = None, value: float | None = None, maximum: float | None = None,
            detail: str | None = None, stop=None, stop_text: str = "Stop") -> None:
        """Create or update a row. value None = busy without a known amount (moving bar)."""
        row = self.rows.get(key)
        if row is None:
            f = ttk.Frame(self.frame)
            f.pack(side="top", fill="x", pady=1)
            title_var, detail_var = tk.StringVar(), tk.StringVar()
            ttk.Label(f, textvariable=title_var, width=24, anchor="e").pack(side="left", padx=(0, 6))
            bar = ttk.Progressbar(f, length=170, mode="indeterminate")
            bar.pack(side="left")
            ttk.Label(f, textvariable=detail_var, style="Muted.TLabel", width=50, anchor="w").pack(
                side="left", padx=(6, 6))
            btn = theme.hint(ttk.Button(f, text=stop_text, width=6), "Stop this operation.")
            row = {"frame": f, "title": title_var, "detail": detail_var, "bar": bar, "btn": btn, "mode": None}
            self.rows[key] = row
            if not self.frame.winfo_ismapped():
                self.frame.pack(side="right", padx=(0, 18))
        if title is not None:
            row["title"].set(title)
        if detail is not None:
            row["detail"].set(detail)
        if stop is not None:
            row["btn"].configure(command=stop, state="normal")
            row["btn"].pack(side="left")
        elif str(row["btn"].cget("command")) == "":
            row["btn"].pack_forget()
        bar = row["bar"]
        if value is None:
            if row["mode"] != "busy":
                bar.configure(mode="indeterminate")
                bar.start(12)
                row["mode"] = "busy"
        else:
            if row["mode"] != "known":
                bar.stop()
                bar.configure(mode="determinate")
                row["mode"] = "known"
            bar.configure(maximum=max(1e-9, float(maximum or 100.0)), value=float(value))

    def detail(self, key: str) -> str:
        row = self.rows.get(key)
        return row["detail"].get() if row else ""

    def value(self, key: str) -> float | None:
        row = self.rows.get(key)
        if not row or row["mode"] != "known":
            return None
        return float(row["bar"].cget("value"))

    def done(self, key: str) -> None:
        row = self.rows.pop(key, None)
        if row is None:
            return
        try:
            row["bar"].stop()
            row["frame"].destroy()
        except tk.TclError:
            pass
        if not self.rows:
            self.frame.pack_forget()
