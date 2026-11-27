"""Review & player: the song open in the player and everything you can do to its timing.

This is the logic behind the player (no drawing): which lyrics are shown, the four timelines (Current,
Found, Aligned, Edited), pins, edited times, gaps, offset and stretch, checkpoints, the GPU's Auto-align
and the choice of what to write. The window draws the state this sends (`review` events) and plays the
audio itself; anything that needs the playback position is given it."""
from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass, field

from . import fit as fitmod
from . import lrc as lrcmod
from . import review
from .config import save_settings
from .engine import display_path
from .paths import state_key
from .state import KEEP, State

GPU = "⚡"                 # marks everything done on the NVIDIA GPU (aligning by ear)
HAND = "✎"                # marks timing or lyrics you set yourself in the player

LANES = ("cur", "new", "aligned", "edit")
LANE_LABEL = {"cur": "Current", "new": "Found", "aligned": GPU + " Aligned", "edit": HAND + " Edited"}
CUR_A, CUR_B = "#a7782a", "#c9973f"
NEW_A, NEW_B = "#2f8a5f", "#46b07f"
EDIT_A, EDIT_B = "#3b67a3", "#4573b0"     # edited lane (blue): lines that moved along
EDIT_AI = "#5468cf"                      # edited lane: lines taken from the aligned timeline
EDIT_USER = "#7db7ff"                    # edited lane: times you set yourself (light blue)
HEARD_LO, HEARD_HI = "#3d2a55", "#d58cff"


def _hex_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def heard_color(p: float) -> str:
    """Colour of a heard word: dim violet (unsure) → bright violet (sure)."""
    p = max(0.0, min(1.0, float(p or 0.0)))
    lo, hi = _hex_rgb(HEARD_LO), _hex_rgb(HEARD_HI)
    return "#%02x%02x%02x" % tuple(int(a + (b - a) * p) for a, b in zip(lo, hi))


def fmt_time(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60)}:{t % 60:04.1f}"


def fmt_left(sec: float) -> str:
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

    def to_json(self) -> list:
        return [round(self.start, 3), round(self.end, 3), self.text, self.key, self.row, self.fill,
                [[round(a, 3), round(b, 3), w] for a, b, w in self.words]]


class Audio:
    """What the backend keeps of the open song's audio: its length, the waveform peaks and (for a
    window that can't decode the format itself) the decoded samples."""

    def __init__(self, data):
        self.data = data
        self.duration = data.duration
        self.peaks = data.peaks


def move_lyrics(times: list, texts: list, rows, dt: float, duration: float) -> dict:
    """Move lyrics (rows of the Edited list) by dt, start and end together. The lyrics attached after
    each (no gap in between) are stretched or squeezed to fill up to the next gap (an empty line) or the
    end of the song, which stay where they are; a gap right after a moved lyric moves with it, and the
    lyric before ends where the moved one now starts. Returns {row: new time}."""
    n = len(times)
    sel = sorted({r for r in rows if 0 <= r < n and texts[r].strip()})
    if not sel or not dt:
        return {}
    blocks: list[list[int]] = []
    for r in sel:
        if blocks and r == blocks[-1][-1] + 1:
            blocks[-1].append(r)
        else:
            blocks.append([r])
    selected = set(sel)
    gap = 0.05

    def chain_end(z):
        r = z + 1
        while r < n and texts[r].strip() and r not in selected:
            r += 1
        if r >= n:
            return n, duration
        return r, times[r]

    lo, hi = -1e9, 1e9
    for b in blocks:
        a, z = b[0], b[-1]
        lo = max(lo, (times[a - 1] + gap - times[a]) if a > 0 else -times[a])
        nxt = z + 1
        if nxt < n and not texts[nxt].strip():
            limit = times[nxt + 1] if nxt + 1 < n else duration
            hi = min(hi, limit - gap - times[nxt])
        elif nxt < n:
            end_row, end_t = chain_end(z)
            count = end_row - nxt
            hi = min(hi, end_t - gap * (count + 1) - times[nxt])
        else:
            hi = min(hi, duration - times[z] - gap)
    dt = max(lo, min(hi, dt))
    if abs(dt) < 1e-6:
        return {}
    out: dict = {}
    for b in blocks:
        for r in b:
            out[r] = round(times[r] + dt, 2)
        nxt = b[-1] + 1
        if nxt < n and not texts[nxt].strip():
            out[nxt] = round(times[nxt] + dt, 2)
        elif nxt < n:
            end_row, end_t = chain_end(b[-1])
            old0, new0 = times[nxt], times[nxt] + dt
            span = end_t - old0
            for r in range(nxt, end_row):
                frac = (times[r] - old0) / span if span > 0 else 0.0
                out[r] = round(new0 + frac * (end_t - new0), 2)
    return out


def resize_lyric(times: list, texts: list, idx: int, edge: str, t: float, duration: float) -> dict:
    """New times after dragging the start or end of lyric `idx` to `t`. Rows are the Edited list's
    lines in time order; empty rows are gaps. Returns {row: new time}; ("end", idx): time for a new
    gap right after the lyric; ("stop", idx): the first row left untouched after a push."""
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
    if nxt >= n:
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
        out[("end", idx)] = round(e, 2)
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
        elif not b.strip() and last_lyric is not None:
            ends[lrcmod.pin_key(last_lyric)] = round(t, 2)
    return {"pins": pins, "ends": ends, "mode": "fixed", "confs": {}}


class ReviewSession:
    def __init__(self, app):
        self.app = app
        self.song: review.SongInfo | None = None
        self.audio: Audio | None = None
        self.cands: list[review.Candidate] = []
        self.cand: review.Candidate | None = None
        self.cand_fit: dict = {}
        self.offset, self.factor = 0.0, 1.0
        self.anchors: list[tuple[float, float]] = []
        self.anchors_note = ""
        self.pins: dict[str, float] = {}
        self.edited: set[str] = set()
        self.interpolate = False
        self.ai_keys: set[str] = set()
        self.end_marks: dict[str, float] = {}
        self.ai_ends: set[str] = set()
        self.ai_conf: dict[str, float] = {}
        self.word_marks: dict[str, dict[int, float]] = {}   # lyric key -> {word index: seconds after its start}
        self._marker_rows: dict[int, str] = {}
        self.orig_segs: list = []
        self.lane_bars: dict[str, list] = {k: [] for k in LANES}
        self.found_text: str | None = None
        self.aligned_text: str | None = None
        self.aligned_verify: float | None = None
        self.aligned_pins: dict = {}
        self.aligned_ends: dict = {}
        self.aligned_conf: dict = {}
        self.aligned_mode = "slope"
        self.aligned_by_key: dict[str, float] = {}
        self.cand_scores: dict = {}
        self.warp_mode = "slope"
        self._align_req = None
        self._align_words = False
        self.ai_note = ""
        self.audio_note = ""
        self.title = "Select a song on the left (or Open file…)."
        self.orig_lines: list = []
        self.new_text: str | None = None
        self.cur_lines: list = []
        self.new_lines: list = []
        self.cur_segs: list = []
        self.new_segs: list = []
        self.checkpoints: list[float | None] = []
        self.cp_custom = False
        self.sel_line: int | None = None
        self._collection = None
        self._collection_key = None
        self._token = 0
        self._loaded_token = 0
        self._auto_job = None
        self._prefs_job = None
        self._emit_job = None
        self._drag = None
        self._lanes_ver = 0
        self._ai_stamp = None
        self._ai_pct: float | None = None

    @property
    def settings(self):
        return self.app.settings

    # ================================================================ state → window
    def changed(self) -> None:
        """Something the window shows changed: send the state once this burst of work is done."""
        if self._emit_job is None:
            self._emit_job = self.app.loop.after(0, self._emit)

    def _emit(self) -> None:
        self._emit_job = None
        self.app.emit("review", self.view())

    def view(self) -> dict:
        lv = self._levels()
        level, default = self.current_level()
        found_pct = self._found_score()
        names = {"keep": "Keep current",
                 "found": "Use found" + (f" · {found_pct:.0f}%" if found_pct is not None else ""),
                 "aligned": GPU + " Use aligned" + (f" · {self.aligned_verify:.0f}%" if self.aligned_verify
                                                     is not None else ""),
                 "edited": HAND + " Use edited"}
        buttons = {}
        for name in ("keep", "found", "aligned", "edited"):
            on = level == name
            buttons[name] = {"label": names[name] + (" (default)" if on and default else ""), "on": on,
                             "avail": self.song is not None and (name == "keep" or bool(lv.get(name)))}
        note = ""
        if level == "other" and self.song is not None:
            ov = self.song.override
            note = f"Will use: {ov[2]}" if ov is not None and ov[2] else "Will use other lyrics (see Choice)"
        n_edit = len(self.edited)
        n_pin = len(self.pins) - n_edit
        if self._ripple_on() and self.pins:
            interp = {"text": f"✓ Ripple · {len(self.pins)}", "on": True}
        elif self.interpolate:
            interp = {"text": f"✓ Interpolated · {len(self.pins)}", "on": True}
        else:
            interp = {"text": "Interpolate", "on": False}
        mine = self._align_req is not None and self.song is not None and self._align_req[0] == self.song.path
        c = self.cand
        if c is None:
            empty = "(nothing to compare)"
        elif c.text is None:
            empty = "(lyrics will be removed)"
        else:
            empty = "(empty)"
        lrc_mode = self.settings.output_mode == "lrc"
        labels = []
        for cd in self.cands:
            f = self.cand_fit.get(cd.key, (None, []))[0]
            labels.append(f"{cd.label}  ·  fit {f:.0f}%" if f is not None else cd.label)
        f, why = self.cand_fit.get(c.key, (None, [])) if c else (None, [])
        return {
            "token": self._token, "loaded": self._loaded_token == self._token,
            "path": self.song.path if self.song else "", "title": self.title,
            "override": self._override_text(), "audio_note": self.audio_note,
            "duration": self._duration(), "has_audio": self.audio is not None,
            "cur_label": "Current (.lrc file beside the song)" if lrc_mode else "Current (embedded)",
            "cur_empty": "(no .lrc file yet)" if lrc_mode else "(no embedded lyrics)",
            "cur_lines": [[t, b] for t, b in self.cur_lines],
            "new_lines": self._new_rows(), "new_empty": empty,
            "cands": labels, "cand": self.cands.index(c) if c in self.cands else -1,
            "fit_note": (f"Fit {f:.0f}%: " + "; ".join(why)) if f is not None and why else "",
            "synced": bool(c is not None and c.synced),
            "offset_ms": round(self.offset * 1000, 1), "stretch_pct": round(self.factor * 100, 4),
            "anchors_note": self.anchors_note,
            "pins_note": f"{n_pin} pinned · {n_edit} edited" if (n_pin or n_edit) else "",
            "interp": interp, "ripple": self._ripple_on(),
            "ai_note": self.ai_note, "ai_busy": mine, "ai_pct": self._ai_pct if mine else None,
            "has_aligned": bool(self.aligned_text),
            "choice": {"level": level, "default": default, "buttons": buttons, "note": note},
            "lanes": {k: [b.to_json() for b in self.lane_bars.get(k, [])] for k in LANES},
            "lanes_ver": self._lanes_ver,
            "aligned_conf": self.aligned_conf, "ai_keys": sorted(self.ai_keys), "edited": sorted(self.edited),
            "has_edits": self.has_edits(),
            "checkpoints": list(self.checkpoints), "cp_custom": self.cp_custom,
            "sel_line": self.sel_line,
            "word_rows": self._word_rows(),
        }

    def _word_rows(self) -> dict:
        """Edited-list rows whose word times you set yourself: {row: [word indexes]}."""
        out = {}
        for i in range(len(self.new_lines)):
            k = self._line_key(i)
            if k in self.word_marks:
                out[i] = sorted(int(j) for j in self.word_marks[k])
        return out

    def _new_rows(self) -> list:
        pins = bool(self.orig_lines)
        out = []
        for i, (t, txt) in enumerate(self.new_lines):
            pin = None
            if pins and t is not None and i in self._marker_rows:
                pin = "marker"
            elif pins and t is not None:
                pinned = i < len(self.orig_lines) and lrcmod.pin_key(self.orig_lines[i][0]) in self.pins
                pin = "pinned" if pinned else "unpinned"
            tag = ""
            if t is not None:
                key = lrcmod.pin_key(self.orig_lines[i][0]) if pins and i < len(self.orig_lines) else None
                if i in self._marker_rows:
                    tag = "ai_edited" if self._marker_rows[i] in self.ai_ends else "edited"
                elif key in self.ai_keys and key in self.pins:
                    tag = "ai_edited"
                elif key in self.edited:
                    tag = "edited"
                elif key in self.pins:
                    tag = "ts_pinned"
                elif pins and self._warping() and i < len(self.orig_lines) and \
                        abs(t - (self.orig_lines[i][0] * self.factor + self.offset)) > 0.05:
                    tag = "moved"
            lyric = not str(self._line_key(i)).startswith("end:")
            out.append([t, txt, pin, tag, lyric])
        return out

    def _override_text(self) -> str:
        if self.song is None:
            return ""
        ov = self.song.override
        if not ov:
            return "No per-file choice — follows settings"
        if ov[0] == KEEP:
            return "Per-file choice: keep current lyrics"
        return f"Per-file choice: {ov[2] or 'chosen lyrics'} — written when you apply"

    # ================================================================ loading
    def load_song(self, path: str) -> None:
        if self._auto_job is not None:            # your last edit on the previous song: choose Edited now
            self._cancel_auto()
            try:
                self._auto_use_edited()
            except Exception:
                pass
        self._token += 1
        token = self._token
        name = os.path.basename(display_path(path))
        self.title = f"Loading {name}…"
        self.changed()
        s = self.settings
        threading.Thread(target=self._load_worker, args=(token, path, s), daemon=True).start()

        def show_if_slow():                       # quick loads don't flash a row
            if token == self._token and self._loaded_token != token:
                self.app.activity.set("load", "Loading song", None, detail=name)
        self.app.loop.after(400, show_if_slow)

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
        from .player import decode

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
        self.app.loop.post(self._loaded, {"token": token, "path": path, "info": info, "audio": audio,
                                          "err": err, "audio_err": audio_err, "fits": fits})

    @staticmethod
    def _store_audio_fits(path: str, s, audio) -> dict:
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

    def _loaded(self, e: dict) -> None:
        self._loaded_token = e["token"]
        self.app.activity.done("load")
        if e["token"] != self._token:
            return
        if e["err"]:
            self.title = os.path.basename(display_path(e["path"]))
            self.audio_note = f"Couldn't open this song: {e['err']}"
            self.changed()
            return
        fits = e.get("fits") or {}
        if fits and state_key(e["path"]) in self.app.rows.rows:
            self.app.rows.add_row(e["path"], fit_local=fits.get("local"), fit_online=fits.get("online"))
            self.app.refresh_apply_bar()
        self._show_song(e["info"], e["audio"], e["audio_err"])

    def _show_song(self, info: review.SongInfo, audio, audio_err: str) -> None:
        prev = self.song.path if self.song is not None else None
        self.song = info
        self.audio = Audio(audio) if audio is not None else None
        m = info.meta
        name = os.path.basename(display_path(info.path))
        self.title = f"{m.artist or '?'} — {m.title or name}" + (f"   ({name})" if m.title else "")
        self.audio_note = audio_err
        self.cur_lines = lrcmod.display_lines(info.current)
        self.cur_segs = lrcmod.segments(info.current, self._duration())
        self.cands = list(info.candidates)
        self.ai_note = ""
        self._ai_pct = None
        self._attach_alignment(info)
        self._fill_candidates()
        prefs = info.prefs or {}
        idx = 0
        if prefs.get("cand"):
            idx = next((i for i, c in enumerate(self.cands) if c.key == prefs["cand"]), 0)
        self.cp_custom = "checkpoints" in prefs
        self.checkpoints = [None if t is None else float(t) for t in prefs.get("checkpoints", [])]
        self.select_candidate(idx, restore=prefs)
        self.app.emit("review_audio", {"token": self._token, "path": display_path(info.path),
                                       "duration": self._duration(),
                                       "peaks": self._peaks_b64() if self.audio is not None else "",
                                       "rate": 100, "error": audio_err, "vocals": self._vocals()})
        self.app.song_shown(prev, info.path)

    def _vocals(self) -> str:
        """The song's isolated vocals (kept by the GPU aligner), for the player's Vocals switch."""
        from . import ai_align

        try:
            return ai_align.find_vocals(self.song.path) if self.song is not None else ""
        except Exception:
            return ""

    def check_vocals(self) -> None:
        """Vocals may arrive after the song was opened (the aligner saves them alongside its work)."""
        if self.song is not None:
            self.app.emit("review_vocals", {"token": self._token, "path": display_path(self.song.path),
                                            "vocals": self._vocals()})

    def _peaks_b64(self) -> str:
        import base64

        import numpy as np

        p = np.clip(np.asarray(self.audio.peaks, dtype=np.float32) * 255.0, 0, 255).astype(np.uint8)
        return base64.b64encode(p.tobytes()).decode("ascii")

    def audio_wav(self) -> str:
        """The open song as a WAV file (for formats the window can't decode itself)."""
        if self.audio is None or self.song is None:
            return ""
        import tempfile
        import wave

        folder = os.path.join(tempfile.gettempdir(), "mass-synced-lyrics-audio")
        os.makedirs(folder, exist_ok=True)
        for f in os.listdir(folder):                        # one at a time: the previous song's goes
            try:
                os.remove(os.path.join(folder, f))
            except OSError:
                pass
        out = os.path.join(folder, hashlib.sha1(self.song.path.encode("utf-8", "replace")).hexdigest()[:16] + ".wav")
        d = self.audio.data
        with wave.open(out, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(d.rate)
            w.writeframes(d.samples.tobytes())
        return out

    def refresh_override(self, songs=None) -> None:
        if self.song is None or (songs is not None and self.song.path not in songs):
            return
        try:
            st = State(self.settings.resolved_state_db())
            self.song.override = st.get_override(self.song.path)
            st.close()
        except Exception:
            pass
        self.changed()

    def reload(self) -> None:
        if self.song is not None:
            self.load_song(self.song.path)

    # ================================================================ levels
    def _levels(self) -> dict:
        c = self.cand
        if c is None:
            return {"found": None, "aligned": None, "edited": None}
        tidy = self.settings.tidy
        found = tidy(self.found_text if self.found_text is not None else c.text) if c.text is not None else None
        return {"found": found, "aligned": tidy(self.aligned_text) if self.aligned_text else None,
                "edited": self.new_text}

    def current_level(self) -> tuple[str, bool]:
        """(level Apply will write: keep / found / aligned / edited / other, whether it's the default)."""
        if self.song is None:
            return "", False
        ov = self.song.override
        try:
            st = State(self.settings.resolved_state_db())
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
        for name in ("aligned", "found", "edited"):
            if lv[name] and lrcmod.same_lyrics(lv[name], text):
                if name != "edited" or self._user_changes():
                    return name, ov is None
        if lv["edited"] and lrcmod.same_lyrics(lv["edited"], text):
            return "edited", ov is None
        return "other", ov is None

    def _user_changes(self) -> bool:
        lines = self.cand is not None and self.found_text is not None and self.cand.text != self.found_text
        return bool(self.edited - self.ai_keys) or bool(set(self.end_marks) - self.ai_ends) or \
            abs(self.offset) > 1e-6 or abs(self.factor - 1) > 1e-9 or lines or bool(self.word_marks)

    def _found_score(self) -> float | None:
        c = self.cand
        if c is None:
            return None
        sc = self.cand_scores.get(c.key + "-original") or self.cand_scores.get(c.key)
        return sc[0] if sc and sc[0] is not None else None

    def _cancel_auto(self) -> None:
        if self._auto_job is not None:
            self.app.loop.cancel(self._auto_job)
            self._auto_job = None

    def use_level(self, level: str) -> None:
        """Choose what to write for this song: the found, aligned or edited timeline (or keep)."""
        self._cancel_auto()
        if level == "keep":
            self.lock()
            return
        if self.song is None or self.cand is None:
            return
        if level == "edited":
            self.apply()
            return
        lv = self._levels()
        text = lv.get(level)
        if not text:
            return
        c = self.cand
        label = re.split(r" · (?:partly )?aligned \(", c.label)[0]
        label = label + (" (found timing)" if level == "found" else
                         f" · aligned ({self.aligned_verify or 0:.0f}% heard)")
        path = self.song.path
        self._run_action(lambda s: review.choose(path, text, label, s, c.path), "chosen", level=level)

    def _duration(self) -> float:
        if self.audio is not None:
            return self.audio.duration
        return self.song.duration if self.song else 0.0

    # ================================================================ candidates
    def _fill_candidates(self) -> None:
        self.cand_fit = {}
        dur = self._duration()
        peaks = self.audio.peaks if self.audio is not None else None
        for c in self.cands:
            if c.text and c.key != "current":
                self.cand_fit[c.key] = fitmod.fit_score(c.text, dur, c.src_length or None, peaks)

    def select_candidate(self, idx: int, restore: dict | None = None) -> None:
        if not self.cands:
            self.cand = None
            self._set_timing(0.0, 1.0)
            self._recompute()
            return
        idx = max(0, min(int(idx), len(self.cands) - 1))
        self.cand = self.cands[idx]
        self.anchors = []
        self.sel_line = None
        ad = getattr(self.cand, "ai_detail", None)
        if not hasattr(self.cand, "found_text"):
            self.cand.found_text = self.cand.text
        self.found_text = self.cand.found_text
        mine = restore if restore and restore.get("cand") == self.cand.key else None
        if mine and mine.get("base") and self.cand.text is not None:
            self.cand.text = mine["base"]
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
            self.word_marks = {k: {int(i): float(o) for i, o in (v or {}).items()}
                               for k, v in (restore.get("words") or {}).items()}
            self._set_timing(restore.get("offset", 0.0), restore.get("factor", 1.0))
            if ad and restore.get("ai_stamp") != ad.get("stamp"):
                self._overlay_ai(ad)
        else:
            self.pins, self.edited, self.interpolate = {}, set(), False
            self.ai_keys, self.warp_mode = set(), "slope"
            self.end_marks, self.ai_ends, self.ai_conf = {}, set(), {}
            self.word_marks = {}
            if ad:
                self._overlay_ai(ad)
            self._set_timing(0.0, 1.0)
        self._recompute()

    def _set_aligned(self, ad: dict | None) -> None:
        ad = ad or {}
        self.aligned_text = ad.get("text") or None
        self.aligned_verify = ad.get("verify")
        self.aligned_pins = {k: float(v) for k, v in (ad.get("pins") or {}).items()}
        self.aligned_ends = {k: float(v) for k, v in (ad.get("ends") or {}).items()}
        self.aligned_conf = {k: float(v) for k, v in (ad.get("confs") or {}).items()}
        self.aligned_mode = ad.get("mode") or "slope"

    def _overlay_ai(self, ad: dict) -> None:
        """Put a stored GPU alignment on the current timing. Times you set yourself stay."""
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
        self.offset, self.factor = float(offset), float(factor)
        self.anchors_note = f"{len(self.anchors)} aligned line(s)" if self.anchors else ""

    def set_timing(self, offset_ms: float, stretch_pct: float) -> None:
        """Offset (ms) and Stretch (%) typed in the player."""
        try:
            off, fac = float(offset_ms) / 1000.0, float(stretch_pct) / 100.0
        except (TypeError, ValueError):
            return
        if fac <= 0:
            return
        if abs(off - self.offset) < 1e-9 and abs(fac - self.factor) < 1e-12:
            return
        self.offset, self.factor = off, fac
        self.interpolate = False
        self._user_edited()
        self._recompute()

    def _recompute(self) -> None:
        c = self.cand
        text = c.text if c else None
        self._marker_rows = {}
        tidy = self.settings.tidy
        self.orig_segs = lrcmod.segments(tidy(text), self._duration()) if text and c.synced else []
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
            if self.aligned_text and lrcmod.WORD_TS_RE.search(self.aligned_text):
                text = lrcmod.carry_word_times(text, self.aligned_text)
            if self.word_marks:                            # word times you set yourself
                now = lrcmod.display_lines(text)
                by_time = {}
                for i, (t, _b) in enumerate(now):
                    k = self._line_key(i)
                    if t is not None and k in self.word_marks:
                        by_time[lrcmod.pin_key(t)] = self.word_marks[k]
                text = lrcmod.apply_word_offsets(text, by_time, self._duration() or None)
        else:
            self.orig_lines = []
        if text:
            text = tidy(text)
        self.new_text = text
        self.new_lines = lrcmod.display_lines(text)
        self.new_segs = lrcmod.segments(text, self._duration())
        self._build_bars()
        self._lanes_ver += 1
        if self.sel_line is not None and self.sel_line >= len(self.new_lines):
            self.sel_line = None
        if not self.cp_custom:
            src = text if lrcmod.is_synced(text) else self.song.current if self.song else ""
            self.checkpoints = review.default_checkpoints(src, self._duration())
        self._save_prefs_later()
        self.changed()

    def _with_end_marks(self, text: str, plain) -> tuple[str, dict]:
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
        return bool(self.pins or self.edited or self.end_marks or self.word_marks or abs(self.offset) > 1e-6
                    or abs(self.factor - 1.0) > 1e-9)

    def edited_segs(self) -> list:
        return self.new_segs if self.has_edits() else []

    # ================================================================ timing
    def select_line(self, idx) -> None:
        self.sel_line = None if idx is None or not (0 <= int(idx) < len(self.new_lines)) else int(idx)
        self.changed()

    def toggle_interpolate(self) -> str:
        """Interpolate button; returns a message when it can't be switched on."""
        if self._ripple_on():
            return ("Ripple changes is on, so the other lines already follow your pins and edits. Turn Ripple "
                    "changes off to place lines one by one.")
        if not self.interpolate and not self.pins:
            return ("Pin or edit at least one line first (two or more for a stretch between them). Lines you "
                    "haven't touched are then interpolated.")
        self.interpolate = not self.interpolate
        self._recompute()
        return ""

    def clear_pins(self) -> None:
        self.pins = {k: v for k, v in self.pins.items() if k in self.edited}
        if not self.pins:
            self.interpolate = False
        self._recompute()

    def clear_edits(self) -> None:
        for k in list(self.edited):
            self.pins.pop(k, None)
        self.edited = set()
        self.ai_keys = set()
        self.end_marks, self.ai_ends, self.ai_conf = {}, set(), {}
        self.word_marks = {}
        if not self.pins:
            self.interpolate = False
        self._recompute()

    def toggle_pin(self, idx: int) -> None:
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

    def _line_key(self, idx) -> str | None:
        if idx is None:
            return None
        if idx in self._marker_rows:
            return "end:" + self._marker_rows[idx]
        if 0 <= idx < len(self.orig_lines):
            return lrcmod.pin_key(self.orig_lines[idx][0])
        return None

    def set_line_time(self, idx: int, seconds: float | None) -> None:
        """Give line `idx` of the Edited list a fixed time; None resets it to the computed time."""
        key = self._line_key(idx)
        if key is None:
            return
        if key.startswith("end:"):
            k = key[4:]
            self.ai_ends.discard(k)
            if seconds is None:
                self.end_marks.pop(k, None)
            else:
                self.end_marks[k] = max(0.0, float(seconds))
            self._recompute()
            return
        self.ai_keys.discard(key)
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

    def set_line_time_text(self, idx: int, raw: str) -> str:
        """A timestamp typed into the editor: empty resets it. Returns an error message, or ''."""
        raw = (raw or "").strip()
        if not raw:
            self.set_line_time(idx, None)
            return ""
        secs = lrcmod.parse_length(raw)
        if secs is None:
            return f"'{raw}' isn't a time. Use m:ss.xx, e.g. 1:23.45"
        self.set_line_time(idx, secs)
        return ""

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
        for d in (self.pins, self.end_marks, self.ai_conf, self.word_marks):
            d.pop(key, None)
        for st in (self.edited, self.ai_keys, self.ai_ends):
            st.discard(key)
        self.sel_line = None
        self._user_edited()
        self._recompute()

    def insert_line(self, idx: int, after: bool, text: str) -> None:
        """A new lyric line before / after row idx, timed halfway to its neighbour."""
        if self.cand is None or not self.cand.synced or idx in self._marker_rows or \
                not 0 <= idx < len(self.orig_lines) or text is None:
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

    def align_line(self, idx, pos: float) -> str:
        """Line to playhead: the selected line is sung at `pos`. Returns a message when it can't."""
        if self.cand is None or not self.cand.synced:
            return "Choose synced lyrics in the Edited pane first."
        if idx is not None:
            self.sel_line = int(idx)
        if self.sel_line is None or self.sel_line >= len(self.new_lines):
            return "Click a line in the Edited pane, play to where that line is actually sung, then press it again."
        t_new = self.new_lines[self.sel_line][0]
        if t_new is None:
            return "That line has no time."
        orig = (t_new - self.offset) / self.factor
        self.anchors = [a for a in self.anchors if abs(a[0] - orig) > 0.01] + [(orig, float(pos))]
        off, fac = lrcmod.solve_alignment(self.anchors, self.factor)
        self._set_timing(off, fac)
        self._recompute()
        return ""

    def stretch_to_length(self, src_text: str | None = None) -> dict:
        """Scale the lyrics by this song's length ÷ the length they were timed for. When that length isn't
        known, returns {"ask": "..."} and the window asks for it (then calls again with src_text)."""
        if self.cand is None or not self.cand.synced:
            return {"info": "Choose synced lyrics in the Edited pane first."}
        dur = self._duration()
        src = self.cand.src_length
        if not src:
            if src_text is None:
                return {"ask": "How long is the version these lyrics were timed for? (m:ss)\n"
                               f"This song is {fmt_time(dur)}."}
            src = lrcmod.parse_length(src_text or "")
            if not src:
                return {}
            self.cand.src_length = src
        if src <= 0 or dur <= 0:
            return {}
        self.anchors = []
        self._set_timing(0.0, dur / src)
        self.anchors_note = f"stretched {fmt_time(src)} → {fmt_time(dur)}"
        self._recompute()
        return {}

    def _reset_timing(self) -> None:
        self.anchors = []
        self.pins, self.edited, self.interpolate = {}, set(), False
        self.ai_keys = set()
        self.end_marks, self.ai_ends, self.ai_conf = {}, set(), {}
        self.word_marks = {}
        self._set_timing(0.0, 1.0)
        self._recompute()

    # ================================================================ timelines
    def _build_bars(self) -> None:
        dur = self._duration()
        tidy = self.settings.tidy
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

    def transfer(self, lane: str, keys=None) -> int:
        """Bring lyrics down from the Found or Aligned timeline into Edited. keys=None: every lyric."""
        if lane not in ("new", "aligned"):
            return 0
        if keys is None:
            self.transfer_all(lane)
            return len(self.lane_bars.get(lane, []))
        keys = set(keys)
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
            self.ai_note = f"Brought {n} lyric(s) down from {LANE_LABEL[lane]} into Edited."
            self._user_edited()
            self._recompute()
        return n

    def transfer_all(self, lane: str) -> None:
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

    def forget_gpu_timing(self) -> None:
        for k in list(self.ai_keys):
            if k in self.pins:
                del self.pins[k]
            self.edited.discard(k)
        for k in list(self.ai_ends):
            self.end_marks.pop(k, None)
        self.ai_keys, self.ai_ends, self.ai_conf = set(), set(), {}
        self._set_aligned(None)
        if self.cand is not None:
            self.cand.ai_detail = None
        self._recompute()
        self._save_prefs()

    def _user_edited(self) -> None:
        """You changed the Edited timeline: it becomes this song's choice (shortly, once you pause)."""
        self._cancel_auto()
        self._auto_job = self.app.loop.after(900, self._auto_use_edited)

    def _auto_use_edited(self) -> None:
        self._cancel_auto()
        if self.song is None or self.cand is None or not self.cand.synced:
            return
        lv = self._levels()
        e = lv["edited"]
        if not e or any(lv[k] and lrcmod.same_lyrics(lv[k], e) for k in ("found", "aligned")):
            return
        if self.current_level()[0] != "edited":
            self.apply()

    # -- dragging lyrics along Edited, stretching one
    def _snapshot(self):
        return (dict(self.pins), set(self.edited), dict(self.end_marks), set(self.ai_keys), set(self.ai_ends))

    def _restore(self, snap) -> None:
        pins, edited, ends, ai_keys, ai_ends = snap
        self.pins, self.edited, self.end_marks = dict(pins), set(edited), dict(ends)
        self.ai_keys, self.ai_ends = set(ai_keys), set(ai_ends)

    def move_begin(self, keys) -> None:
        """Start dragging the selected lyrics (keys) along the Edited timeline."""
        row_of = {b.key: b.row for b in self.lane_bars.get("edit", []) if b.key and b.row is not None}
        self._drag = {"kind": "move", "snap": self._snapshot(), "rows": [t for t, _ in self.new_lines],
                      "texts": [x for _, x in self.new_lines],
                      "sel": [row_of[k] for k in keys if k in row_of]}

    def move(self, dt: float) -> None:
        d = self._drag
        if not d or d["kind"] != "move":
            return
        self._restore(d["snap"])
        changes = move_lyrics(d["rows"], d["texts"], d["sel"], float(dt), self._duration() or 1e9)
        self._apply_row_times(changes, d["rows"])
        self._recompute()

    def move_end(self, dt: float | None = None) -> None:
        if dt is not None:
            self.move(dt)
        if self._drag is not None:
            self._drag = None
            self._user_edited()
            self._save_prefs_later()

    def move_selected(self, keys, dt: float) -> None:
        """Move lyrics in one go (begin, move, end)."""
        self.move_begin(keys)
        self.move_end(dt)

    def _apply_row_times(self, changes: dict, times: list) -> None:
        moved = sorted(i for i in changes if isinstance(i, int))
        for i in moved:
            key = self._line_key(i)
            if key is None:
                continue
            v = changes[i]
            if key.startswith("end:"):
                k = key[4:]
                self.end_marks[k] = v
                self.ai_ends.discard(k)
            else:
                self.pins[key] = v
                self.edited.add(key)
                self.ai_keys.discard(key)
        if self._warping() and moved:
            edges = set()
            for i in moved:
                for step in (-1, 1):
                    j = i + step
                    while 0 <= j < len(times) and j not in changes:
                        key = self._line_key(j)
                        if key and not key.startswith("end:") and times[j] is not None:
                            edges.add(j)
                            break
                        j += step
            for j in edges:
                key = self._line_key(j)
                if key not in self.pins:
                    self.pins[key] = times[j]

    def edge_handles(self) -> list:
        """Where lyrics in Edited can be stretched: (row, edge, time)."""
        out = []
        rows = self.new_lines
        for b in self.lane_bars.get("edit", []):
            if b.row is None:
                continue
            nxt = b.row + 1
            out.append((b.row, "start", b.start))
            if nxt >= len(rows) or not rows[nxt][1].strip():
                out.append((b.row, "end", b.end))
        return out

    def resize_begin(self, row: int, edge: str) -> None:
        self._drag = {"kind": "resize", "edge": (int(row), edge), "snap": self._snapshot(),
                      "times": [t for t, _ in self.new_lines], "texts": [b for _, b in self.new_lines]}

    def resize(self, t: float) -> None:
        r = self._drag
        if not r or r["kind"] != "resize":
            return
        self._restore(r["snap"])
        idx, edge = r["edge"]
        changes = resize_lyric(r["times"], r["texts"], idx, edge, round(float(t), 2), self._duration() or 1e9)
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
        if new_end is not None:
            k = self._line_key(idx)
            if k and not k.startswith("end:"):
                self.end_marks[k] = new_end
                self.ai_ends.discard(k)
        if self._warping():
            stop = changes.get(("stop", idx))
            if isinstance(stop, int):
                k = self._line_key(stop)
                if k and not k.startswith("end:") and k not in self.pins:
                    self.pins[k] = r["times"][stop]
        self._recompute()

    def resize_end(self, t: float | None = None) -> None:
        if t is not None:
            self.resize(t)
        if self._drag is not None:
            self._drag = None
            self._user_edited()
            self._save_prefs_later()

    def next_line_to_needle(self, pos: float) -> None:
        """Target button: give the next lyric (the first starting after the needle) the needle's time."""
        if self.audio is None:
            return
        nxt = next((b for b in self.lane_bars.get("edit", []) if b.row is not None and b.start > pos + 0.01),
                   None)
        if nxt is None:
            self.ai_note = "No lyric after the needle."
            self.changed()
            return
        self.set_line_time(nxt.row, round(pos, 2))
        self.ai_note = f"“{nxt.text[:40]}” now starts at the needle ({lrcmod.format_ts(pos)})."
        self.changed()

    # -- word times (word-synced lyrics)
    def _edit_bar(self, row):
        return next((b for b in self.lane_bars.get("edit", []) if b.row == row), None)

    def set_word_time(self, row: int, word: int, t: float) -> None:
        """Move word `word` of Edited row `row` to start at `t` (between its neighbours, inside the line)."""
        key = self._line_key(row)
        b = self._edit_bar(row)
        if key is None or key.startswith("end:") or b is None or not (0 < int(word) < len(b.words)):
            return
        word = int(word)
        lo = b.words[word - 1][0] + 0.05
        hi = (b.words[word + 1][0] - 0.05) if word + 1 < len(b.words) else b.end - 0.05
        if hi < lo:
            return
        t = max(lo, min(float(t), hi))
        marks = self.word_marks.setdefault(key, {})
        if not marks:                                  # the first word you move: the others keep their times
            for j, (ws, _we, _w) in enumerate(b.words):
                if j:
                    marks[j] = round(ws - b.start, 3)
        marks[word] = round(t - b.start, 3)
        self._user_edited()
        self._recompute()

    def time_words(self, row: int) -> str:
        """Give a line's words times of their own, shared out by length over the line (to adjust after)."""
        key = self._line_key(row)
        b = self._edit_bar(row)
        if key is None or key.startswith("end:") or b is None:
            return ""
        words = b.text.split()
        if len(words) < 2:
            return "This line has only one word."
        span = max(0.4, min(b.end - b.start, 0.45 * len(words) + 1.5) * 0.92)
        lens = [max(1, len(w)) for w in words]
        total, acc, marks = sum(lens), 0, {}
        for j in range(1, len(words)):
            acc += lens[j - 1]
            marks[j] = round(span * acc / total, 3)
        self.word_marks[key] = marks
        self._user_edited()
        self._recompute()
        return ""

    def clear_word_times(self, row: int) -> None:
        """Forget the word times you set on this line (back to the aligner's, or to none)."""
        key = self._line_key(row)
        if key in self.word_marks:
            del self.word_marks[key]
            self._user_edited()
            self._recompute()

    def next_word_to_needle(self, pos: float) -> None:
        """The next word in the line being sung starts at the needle."""
        pos = float(pos)
        b = next((x for x in self.lane_bars.get("edit", []) if x.row is not None and x.start <= pos < x.end), None)
        if b is None:
            self.note("No lyric at the needle.")
            return
        if not b.words:
            self.note("This line has no word times yet: right-click it in the Edited timeline → Time this line's "
                      "words.")
            return
        j = next((i for i, (ws, _we, _w) in enumerate(b.words) if i and ws > pos + 0.01), None)
        if j is None:
            self.note("No word after the needle in this line.")
            return
        word = b.words[j][2]
        self.set_word_time(b.row, j, pos)
        self.note(f"“{word}” now starts at the needle ({lrcmod.format_ts(pos)}).")

    def note(self, text: str) -> None:
        self.ai_note = text
        self.changed()

    # ================================================================ GPU alignment
    def _ripple_on(self) -> bool:
        return bool(getattr(self.settings, "ripple", False))

    def _mode(self) -> str:
        return "ripple" if self._ripple_on() else self.warp_mode

    def _warping(self) -> bool:
        return bool(self.pins) and (self.interpolate or self._ripple_on())

    def set_ripple(self, on: bool) -> None:
        self.settings.ripple = bool(on)
        if on:
            self.warp_mode = "ripple"
        elif self.warp_mode == "ripple":
            self.warp_mode = "slope"
        try:
            save_settings(self.settings)
        except OSError:
            pass
        self._recompute()

    def _attach_alignment(self, info) -> None:
        try:
            st = State(self.settings.resolved_state_db())
            try:
                det = st.get_aligned_detail(info.path)
            finally:
                st.close()
        except Exception:
            det = None
        if not det or not det["ok"] or not det.get("text"):
            return
        orig = det.get("orig") or ""
        if not orig:
            o = next((c for c in self.cands if c.key.endswith("-original") and c.text), None)
            if o is None:
                return
            orig = o.text
            det = dict(det, **_diff_as_edits(orig, det["text"]))
            det["orig"] = orig
        det["stamp"] = hashlib.sha1((det["text"] or "").encode("utf-8", "replace")).hexdigest()[:16]
        for c in self.cands:
            if c.text and lrcmod.same_lyrics(c.text, det["text"]):
                c.text = orig
                c.ai_detail = det

    def auto_align(self, words: bool | None = None) -> str:
        """Auto-align (GPU): align the shown lyrics to this song's vocals. The window has already made
        sure alignment is switched on. Returns a message when it can't start."""
        from . import ai_align

        if self.song is None or self.cand is None or not self.cand.synced or not self.cand.text:
            return "Choose synced lyrics in the Edited pane first."
        if words is None:
            words = bool(getattr(self.settings, "sync_words", False))
        items = ai_align.lyric_lines(self.cand.text)
        if len(items) < 2:
            return "These lyrics have fewer than 2 timed lines to align."
        n = (self._align_req[2] + 1) if self._align_req else 1
        self._align_req = (self.song.path, self.cand.key, n)
        self._align_words = bool(words)
        self._ai_pct = None
        svc = self.app.align_service_get()
        if svc.paused:
            self.ai_note = "Waiting — aligning is paused (see the message)"
        elif svc.busy:
            self.ai_note = "Waiting for the GPU to finish the song it's on…"
        else:
            self.ai_note = "Starting…"
        self.app.activity.set("autoalign", GPU + " Auto-align", None,
                              detail=os.path.basename(display_path(self.song.path)), stop="autoalign")
        svc.request_raw(self.song.path, [b for _, b in items], items[0][0], self._align_req,
                        duration=self._duration() or None)
        self.app.align_tick()
        self.changed()
        return ""

    def auto_align_progress(self, snap: dict | None, waiting: bool) -> None:
        mine = self._align_req is not None and self.song is not None and self._align_req[0] == self.song.path
        if not mine:
            return
        if snap is None:
            self._ai_pct = None
            if waiting:
                self.ai_note = "Waiting for the GPU to finish the song it's on…"
            self.changed()
            return
        self._ai_pct = round(snap["frac"] * 100, 1)
        self.ai_note = f"{snap['label']} ({snap['stage_frac'] * 100:.0f}%) · about {fmt_left(snap['left'])} left"
        self.changed()

    def cancel_auto_align(self) -> None:
        self._align_req = None
        self._ai_pct = None
        self.ai_note = "Auto-align cancelled."
        self.app.activity.done("autoalign")
        self.changed()

    def align_result(self, e: dict) -> None:
        """The GPU's answer to Auto-align: times applied if enough was heard."""
        from . import ai_align

        if e.get("token") != self._align_req:
            return
        self._align_req = None
        self._ai_pct = None
        self.changed()
        if self.song is None or self.cand is None or e.get("path") != self.song.path:
            self.app.log(f"Auto-align finished for {os.path.basename(display_path(e.get('path') or ''))} after "
                         "you moved to another song: not applied. Open it and press Auto-align again.")
            return
        res = e.get("result") or {}
        self.check_vocals()
        self.app.loop.after(4000, self.check_vocals)
        if not res.get("ok"):
            self.ai_note = f"Auto-align failed: {res.get('error', 'unknown error')}"
            return
        minimum = float(getattr(self.settings, "ai_align_min", 70))
        words = self._align_words
        out = ai_align.evaluate(self.cand.text, res, self._duration() or None, minimum, self._mode(),
                                **({"words": True} if words else {}))
        if not out.ok:
            self.ai_note = f"Not applied — {out.reason}."
            return
        self._set_aligned({"text": out.text, "verify": out.verify, "pins": out.pin_map, "ends": out.ends,
                           "confs": out.line_conf, "mode": self._mode()})
        mine = self.edited - self.ai_keys
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
        if getattr(out, "words", False):
            kept += " · word-synced: review the whole song before saving"
        if out.partial:
            self.ai_note = (f"{GPU} Partly aligned: {out.verify:.0f}% of words heard (below your {minimum:.0f}%), "
                            f"so only the {len(self.ai_keys)} of {out.lines} lines it was sure of were placed "
                            f"(purple); the others {follow}{kept}. Check them.")
        else:
            self.ai_note = (f"{GPU} Aligned: {out.verify:.0f}% of words heard · {len(self.ai_keys)} of {out.lines} "
                            f"lines placed (purple), the others {follow}{kept}.")

    # ================================================================ checkpoints
    def add_checkpoint(self, t: float, slot: int | None = None) -> int:
        """A new checkpoint: in `slot` (key number - 1), or the first free number."""
        cps = self.checkpoints
        if slot is None:
            slot = next((i for i, x in enumerate(cps) if x is None), len(cps))
        slot = int(slot)
        while len(cps) <= slot:
            cps.append(None)
        cps[slot] = round(max(0.0, min(float(t), self._duration() or float(t))), 2)
        self.cp_custom = True
        self._checkpoints_changed()
        return slot

    def delete_checkpoint(self, i: int) -> None:
        if 0 <= i < len(self.checkpoints) and self.checkpoints[i] is not None:
            self.checkpoints[i] = None
            while self.checkpoints and self.checkpoints[-1] is None:
                self.checkpoints.pop()
            self.cp_custom = True
            self._checkpoints_changed()

    def default_checkpoints(self) -> None:
        self.cp_custom = False
        self._recompute()

    def _checkpoints_changed(self) -> None:
        self._save_prefs_later()
        self.changed()

    # ================================================================ prefs
    def _save_prefs_later(self) -> None:
        if self._prefs_job is not None:
            self.app.loop.cancel(self._prefs_job)
        self._prefs_job = self.app.loop.after(600, self._save_prefs)

    def _save_prefs(self) -> None:
        self._prefs_job = None
        if self.song is None:
            return
        prefs = {"cand": self.cand.key if self.cand else "", "offset": self.offset, "factor": self.factor,
                 "pins": dict(self.pins), "edited": sorted(self.edited), "interpolate": self.interpolate,
                 "ai": sorted(self.ai_keys & set(self.pins)), "mode": self.warp_mode,
                 "ends": dict(self.end_marks), "ai_ends": sorted(self.ai_ends & set(self.end_marks)),
                 "ai_conf": dict(self.ai_conf)}
        if self.word_marks:
            prefs["words"] = {k: {str(i): o for i, o in v.items()} for k, v in self.word_marks.items()}
        ad = getattr(self.cand, "ai_detail", None) if self.cand else None
        if ad and ad.get("stamp"):
            prefs["ai_stamp"] = ad["stamp"]
        elif self.aligned_text:
            prefs["aligned"] = {"text": self.aligned_text, "verify": self.aligned_verify,
                                "pins": self.aligned_pins, "ends": self.aligned_ends,
                                "confs": self.aligned_conf, "mode": self.aligned_mode}
        if self.cand is not None and self.found_text is not None and self.cand.text != self.found_text:
            prefs["base"] = self.cand.text
        if self.cp_custom:
            prefs["checkpoints"] = self.checkpoints
        if prefs != (self.song.prefs or {}):
            try:
                review.save_prefs(self.song.path, prefs, self.settings)
                self.song.prefs = prefs
            except Exception:
                pass

    # ================================================================ choices
    def apply(self) -> None:
        """Use edited: record the Edited lyrics for this file; Apply writes them."""
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
        path = self.song.path
        self._run_action(lambda s: review.choose(path, text, label, s, c.path), "chosen",
                         hand=bool(c.synced and mine), level="edited")

    def lock(self) -> None:
        self._cancel_auto()
        if self.song is not None:
            path = self.song.path
            self._run_action(lambda s: review.lock_current(path, s), "locked")

    def clear(self) -> None:
        self._cancel_auto()
        if self.song is not None:
            path = self.song.path
            self._run_action(lambda s: review.clear_choice(path, s), "cleared")

    def _run_action(self, fn, status, hand: bool = False, level: str = "") -> None:
        path, s = self.song.path, self.settings

        def work():
            try:
                msg = fn(s)
                st = State(s.resolved_state_db())
                try:
                    choice = st.current_choice(path)
                finally:
                    st.close()
                e = {"path": path, "status": status, "msg": msg or "", "choice": choice, "hand": hand,
                     "level": level or ("keep" if status == "locked" else "")}
            except Exception as ex:
                e = {"path": path, "err": f"{type(ex).__name__}: {ex}"}
            self.app.loop.post(self._action_done, e)
        threading.Thread(target=work, daemon=True).start()

    def _action_done(self, e: dict) -> None:
        if e.get("err"):
            self.app.dialog("error", e["err"])
        else:
            if e.get("status"):
                self.app.rows.choice_done(e)
            self.app.refresh_apply_bar()
            if e.get("msg"):
                self.app.log(e["msg"])
        if self.song is not None and e["path"] == self.song.path:
            self.refresh_override()

    # ================================================================ LRCLIB
    def search_lrclib(self) -> str:
        if self.song is None:
            return ""
        if not self.song.meta.title:
            return "This file has no title tag to search for."
        token, meta, dur, s = self._token, self.song.meta, self._duration(), self.settings
        self.app.activity.set("search", "Searching LRCLIB", None, detail=meta.title)

        def work():
            try:
                cands = review.search_lrclib(meta, dur, s)
                e = {"token": token, "cands": cands}
            except Exception as ex:
                e = {"token": token, "cands": [], "err": f"{type(ex).__name__}: {ex}"}
            self.app.loop.post(self._lrclib_done, e)
        threading.Thread(target=work, daemon=True).start()
        return ""

    def _lrclib_done(self, e: dict) -> None:
        self.app.activity.done("search")
        if e["token"] != self._token or self.song is None:
            return
        if e.get("err"):
            self.app.dialog("error", f"LRCLIB search failed:\n{e['err']}")
            return
        known = {c.key for c in self.cands}
        new = [c for c in e["cands"] if c.key not in known]
        if not new:
            self.app.dialog("info", "LRCLIB returned no other synced lyrics for this song.")
            return
        self.cands[0:0] = new
        self._fill_candidates()
        self.select_candidate(0)

    def shutdown(self) -> None:
        for job in ("_auto_job", "_emit_job"):
            j = getattr(self, job, None)
            if j is not None:
                self.app.loop.cancel(j)
                setattr(self, job, None)
        if self._prefs_job is not None:
            self.app.loop.cancel(self._prefs_job)
            self._save_prefs()
