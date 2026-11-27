"""The song list (Review & player): one row per song, kept by the backend and mirrored in the window.

The window groups, filters and draws the rows; every change here is sent to it in batches."""
from __future__ import annotations

from . import review
from .paths import state_key
from .state import State

ROW_KEYS = ("status", "source", "artist", "title", "local", "online", "choice", "album", "conf_local",
            "conf_online", "fit_local", "fit_online", "op_find", "op_align", "op_save", "hand", "level", "check")
OPS = ("find", "align", "save")
FOUND_STATUSES = {"proposed", "chosen", "locked", "aligned"}
SAVED_STATUSES = {"embedded", "lrc_written"}          # this app wrote the lyrics
DONE_STATUSES = {"embedded", "lrc_written", "already_embedded", "unchanged"}
PROBLEM_STATUSES = {"error", "ambiguous", "unsupported", "rejected"}


def blank_row() -> dict:
    row = {k: (None if k.startswith(("conf_", "fit_")) else "") for k in ROW_KEYS}
    row["hand"] = False                       # re-timed by you in the player (✎ in the Align column)
    row["check"] = False                      # flagged: look at it before saving
    return row


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


def _by_choice(r: dict, prefix: str):
    choice = r.get("choice")
    if choice == "local":
        return r.get(prefix + "local")
    if choice == "online":
        return r.get(prefix + "online")
    if choice in ("keep", "custom", "remove"):
        return None
    vals = [c for c in (r.get(prefix + "local"), r.get(prefix + "online")) if c is not None]
    return max(vals) if vals else None


def row_confidence(r: dict):
    return _by_choice(r, "conf_")


def row_fit(r: dict):
    return _by_choice(r, "fit_")


class SongList:
    def __init__(self, app):
        self.app = app
        self.rows: dict[str, dict] = {}
        self.status_before_choice: dict[str, str] = {}
        self._group_target: dict[str, str] = {}
        self._dirty: set[str] = set()
        self._removed: set[str] = set()
        self._flush_job = None
        # what the window shows (kept for the session file)
        self.ui = {"selected": "", "filter": "All", "search": "", "only_found": True}

    # ------------------------------------------------------------ changes → window
    def _changed(self, path: str) -> None:
        self._dirty.add(path)
        self._removed.discard(path)
        self._schedule()

    def _schedule(self) -> None:
        if self._flush_job is None:
            self._flush_job = self.app.loop.after(60, self.flush)

    def flush(self) -> None:
        self._flush_job = None
        if not self._dirty and not self._removed:
            return
        up = {p: self.rows[p] for p in self._dirty if p in self.rows}
        rem = sorted(self._removed)
        self._dirty.clear()
        self._removed.clear()
        self.app.emit("rows", {"upsert": up, "remove": rem})

    def send_all(self) -> None:
        self._dirty.clear()
        self._removed.clear()
        self.app.emit("rows", {"clear": True, "upsert": self.rows})

    # ------------------------------------------------------------ rows
    def add_row(self, path: str, status: str | None = None, source: str | None = None,
                artist: str | None = None, title: str | None = None, local: str | None = None,
                online: str | None = None, choice: str | None = None, album: str | None = None,
                conf_local: float | None = None, conf_online: float | None = None,
                fit_local: float | None = None, fit_online: float | None = None, ops: dict | None = None,
                **extra) -> None:
        """Add or update a song row; None keeps the current value."""
        path = state_key(path)
        row = self.rows.get(path) or blank_row()
        for k, v in (("status", status), ("source", source), ("artist", artist), ("title", title),
                     ("local", local), ("online", online), ("choice", choice), ("album", album),
                     ("conf_local", conf_local), ("conf_online", conf_online),
                     ("fit_local", fit_local), ("fit_online", fit_online)):
            if v is not None:
                row[k] = v
        for op, v in (ops or {}).items():
            row["op_" + op] = v
        row.update(extra)
        self.rows[path] = row
        self.app.session_changed()
        self.app.show_apply_bar()
        self._changed(path)

    def set_op(self, path: str, op: str, state: str, status: str | None = None) -> None:
        """A song's progress in one operation column: busy (↻), done (✓), skip (–), fail (✗)."""
        key = state_key(path)
        if key not in self.rows and not (op == "find" and state == "busy"):
            return                                # only Find adds songs to the list
        self.add_row(key, status, ops={op: state})

    def set_align_pct(self, path: str, pct: float | None) -> None:
        key = state_key(path)
        r = self.rows.get(key)
        if r is None:
            return
        if r.get("align_pct") != (None if pct is None else round(pct)):
            r["align_pct"] = None if pct is None else round(pct)
            self._changed(key)

    def clear_busy(self, op: str) -> None:
        """An operation ended: no song is in progress in it any more."""
        for p, r in list(self.rows.items()):
            if r.get("op_" + op) == "busy":
                self.add_row(p, None if r.get("status") != "searching" else "", ops={op: ""})

    def remove(self, songs) -> int:
        n = 0
        for p in songs:
            if self.rows.pop(p, None) is None:
                continue
            n += 1
            self.status_before_choice.pop(p, None)
            self._dirty.discard(p)
            self._removed.add(p)
        if n:
            self._schedule()
            self.app.session_changed()
            self.app.refresh_apply_bar()
        return n

    def clear(self) -> None:
        self.rows.clear()
        self._group_target.clear()
        self.status_before_choice.clear()
        self._dirty.clear()
        self._removed.clear()
        self.app.emit("rows", {"clear": True, "upsert": {}})

    def paths(self) -> list[str]:
        return list(self.rows.keys())

    # ------------------------------------------------------------ earlier results, session
    def load_earlier(self) -> int:
        st = State(self.app.settings.resolved_state_db())
        try:
            rows = st.iter_files()
            choices = {r[0]: st.current_choice(r[0]) for r in rows}
            scores = st.all_candidate_scores()
        finally:
            st.close()
        n = 0
        for path, status, source, artist, title, _, local, online, album in rows:
            if path not in self.rows:
                c = scores.get(path, {})
                lo, on = c.get("local", (None, None)), c.get("online", (None, None))
                row = blank_row()
                row.update({"status": status, "source": source or "", "artist": artist or "",
                            "title": title or "", "local": local or "", "online": online or "",
                            "choice": choices.get(path, ""), "album": album or "",
                            "conf_local": lo[0], "conf_online": on[0], "fit_local": lo[1], "fit_online": on[1]})
                self.rows[path] = row
                self._dirty.add(path)
                n += 1
        self._schedule()
        self.app.show_apply_bar()
        self.app.session_changed()
        return n

    def session_data(self) -> dict:
        return {"rows": self.rows, "selected": self.ui.get("selected", ""), "filter": self.ui.get("filter", "All"),
                "search": self.ui.get("search", ""), "only_found": bool(self.ui.get("only_found", True))}

    def load_session_rows(self, data: dict, include_saved: bool = False) -> int:
        """Bring back last session's songs. Songs already saved are left out unless include_saved."""
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
                        r["status"] = info["status"]
                    r["choice"] = st.current_choice(p)
            finally:
                st.close()
        except Exception:
            pass
        if not include_saved:
            rows = {p: r for p, r in rows.items() if r.get("status") not in SAVED_STATUSES}
        for p, r in rows.items():
            base = blank_row()
            base.update({k: v for k, v in r.items() if k in base})
            for op in OPS:                                # nothing is in progress after a restart
                if base["op_" + op] == "busy":
                    base["op_" + op] = ""
            if base["status"] == "searching":
                base["status"] = ""
            self.rows[p] = base
        for k in ("selected", "filter", "search", "only_found"):
            if k in data:
                self.ui[k] = data[k]
        self.send_all()
        self.app.show_apply_bar()
        return len(rows)

    # ------------------------------------------------------------ choices
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
            self.status_before_choice[path] = row.get("status", "")
        self.add_row(path, status, choice=kind)

    def _after_choice(self, songs) -> None:
        self.app.refresh_apply_bar()
        self.app.session_changed()
        self.app.review.refresh_override(songs)

    def cycle_choice(self, path: str) -> str:
        s = self.app.settings
        opts = review.choice_options(path, s)
        kinds = [o[0] for o in opts]
        cur = self.rows.get(path, {}).get("choice", "")
        nxt = kinds[(kinds.index(cur) + 1) % len(kinds)] if cur in kinds else kinds[0]
        opt = next(o for o in opts if o[0] == nxt)
        self._set_choice(path, opt)
        self._after_choice([path])
        return opt[0]

    def cycle_many(self, songs: list[str], node: str) -> str:
        """Set every song to the next of: Local → Online → Don't change (where available)."""
        songs = [p for p in songs if p in self.rows]
        if not songs:
            return ""
        order = ["local", "online", "keep"]
        current = {self.rows[p]["choice"] for p in songs}
        cur = next(iter(current)) if len(current) == 1 else self._group_target.get(node, "")
        target = order[(order.index(cur) + 1) % 3] if cur in order else "local"
        self._group_target[node] = target
        s = self.app.settings
        for p in songs:
            opts = {o[0]: o for o in review.choice_options(p, s)}
            kind = target if target in opts else ("keep" if target == "keep" else None)
            if kind is None:
                continue
            self._set_choice(p, opts[kind])
        self._after_choice(songs)
        return target

    def set_many(self, songs: list[str], kind: str) -> int:
        s, changed = self.app.settings, 0
        for p in songs:
            opts = {o[0]: o for o in review.choice_options(p, s)}
            if kind in opts:
                self._set_choice(p, opts[kind])
                changed += 1
        self._after_choice(songs)
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
                prev = self.status_before_choice.pop(p, None)
                cur = self.rows.get(p, {}).get("status", "")
                self.add_row(p, prev if prev is not None else ("proposed" if cur in ("chosen", "locked") and
                                                               choice != "keep" else cur), choice=choice)
        finally:
            st.close()
        self._after_choice(songs)

    def choice_done(self, e: dict) -> None:
        """A choice made in the player was stored (Use found / aligned / edited, Keep, Reset)."""
        path = state_key(e["path"])
        st_ = self.rows.get(path, {}).get("status", "")
        row = self.rows.get(path)
        if row is not None:
            row["hand"] = bool(e.get("hand")) and e["status"] == "chosen"
            row["level"] = e.get("level") or ""
        if e["status"] == "cleared":
            self.add_row(path, self.status_before_choice.pop(path, st_), choice=e.get("choice"))
        else:
            if st_ not in ("chosen", "locked"):
                self.status_before_choice[path] = st_
            self.add_row(path, e["status"], choice=e.get("choice"))
