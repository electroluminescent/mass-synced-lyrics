"""Align low-confidence lyrics: optional, NVIDIA-only add-on.

The app never imports PyTorch. The add-on is a separate Python environment in
%LOCALAPPDATA%\\mass-synced-lyrics\\aligner (PyTorch with CUDA, stable-ts/Whisper, Demucs, PyAV); its
worker (aligner_worker.py) isolates the vocals and force-aligns the known lyric lines to them. This module:

* detects an NVIDIA GPU (nvidia-smi), installs / checks the add-on, runs the worker process;
* turns the worker's word timings into re-timed lyrics: lines heard clearly become pins and the rest are
  interpolated between them (the same as the player's Interpolate from pins);
* scores the result ("verify", 0-100: how many of the lyric's words were clearly heard where they are
  placed). The caller keeps the result only at or above the user's minimum.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field

from . import lrc as lrcmod
from .paths import display_path

TORCH_INDEX = "https://download.pytorch.org/whl/cu126"
PACKAGES = ["stable-ts", "demucs", "julius", "av", "numpy"]
WORD_OK = 0.5             # a word counts as clearly heard at this probability or more
PIN_LINE = 0.6            # a line becomes a pin when its words average this probability or more
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _run(args, timeout=20, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW, **kw)


# ------------------------------------------------------------------ hardware / add-on location
def nvidia_gpu() -> str | None:
    """Name and memory of the first NVIDIA GPU, or None (no card, or no NVIDIA driver)."""
    exe = shutil.which("nvidia-smi")
    if exe is None and os.name == "nt":
        cand = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
        exe = cand if os.path.exists(cand) else None
    if exe is None:
        return None
    try:
        r = _run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader"], timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    first = (r.stdout or "").strip().splitlines()
    return first[0].strip() if r.returncode == 0 and first else None


def addon_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "mass-synced-lyrics", "aligner")


def addon_python() -> str:
    d = os.path.join(addon_dir(), "venv")
    return os.path.join(d, "Scripts", "python.exe") if os.name == "nt" else os.path.join(d, "bin", "python")


def worker_source() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "aligner_worker.py")


def is_installed() -> bool:
    return os.path.exists(addon_python()) and os.path.exists(os.path.join(addon_dir(), "installed.json"))


def find_system_python() -> list[str] | None:
    """A Python 3.10-3.12 to build the add-on with (PyTorch and Demucs wheels exist for these)."""
    cands: list[list[str]] = []
    if os.name == "nt":
        cands += [["py", f"-3.{v}"] for v in (12, 11, 10)]
    cands += [[f"python3.{v}"] for v in (12, 11, 10)] + [["python3"], ["python"]]
    for c in cands:
        if shutil.which(c[0]) is None:
            continue
        try:
            r = _run(c + ["-c", "import sys; print('%d.%d' % sys.version_info[:2])"], timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        v = (r.stdout or "").strip()
        if r.returncode == 0 and v in ("3.10", "3.11", "3.12"):
            return c
    return None


def check_addon() -> dict:
    """Run the worker's self-check in the add-on environment."""
    if not is_installed():
        return {"ok": False, "error": "the add-on isn't installed"}
    try:
        r = _run([addon_python(), "-I", _installed_worker(), "--check"], timeout=180)
        return json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _installed_worker() -> str:
    return os.path.join(addon_dir(), "aligner_worker.py")


def refresh_worker() -> None:
    """Copy this version's worker script into the add-on folder (cheap; done before every start)."""
    src, dst = worker_source(), _installed_worker()
    try:
        if not os.path.exists(dst) or open(src, "rb").read() != open(dst, "rb").read():
            shutil.copyfile(src, dst)
    except OSError:
        pass


def install(log, cancel: threading.Event | None = None) -> bool:
    """Build the add-on environment: about 3 GB of downloads (PyTorch with CUDA, Whisper, Demucs).
    The speech and separation models (~1.6 GB more) download on first use. `log(str)` gets progress."""
    cancel = cancel or threading.Event()
    py = find_system_python()
    if py is None:
        log("Align: Python 3.10, 3.11 or 3.12 is needed to install the add-on (python.org). None was found.")
        return False
    d = addon_dir()
    os.makedirs(d, exist_ok=True)
    venv = os.path.join(d, "venv")
    steps = [
        ("Creating the add-on environment", py + ["-m", "venv", venv]),
        ("Updating pip", [addon_python(), "-m", "pip", "install", "-U", "pip"]),
        ("Installing PyTorch with CUDA (about 2.5 GB)", [addon_python(), "-m", "pip", "install", "torch",
                                                         "--index-url", TORCH_INDEX]),
        ("Installing Whisper, stable-ts and Demucs", [addon_python(), "-m", "pip", "install"] + PACKAGES),
    ]
    for title, cmd in steps:
        if cancel.is_set():
            log("Align: installation stopped.")
            return False
        log(f"Align: {title}…")
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 creationflags=_NO_WINDOW)
        except OSError as e:
            log(f"Align: {title} failed: {e}")
            return False
        for line in p.stdout:                                     # type: ignore[union-attr]
            line = line.rstrip()
            if line and not line.startswith(("  ", "Requirement already")):
                log("    " + line[:200])
            if cancel.is_set():
                p.kill()
        if p.wait() != 0:
            log(f"Align: {title} failed (exit code {p.returncode}).")
            return False
    shutil.copyfile(worker_source(), _installed_worker())
    with open(os.path.join(d, "installed.json"), "w", encoding="utf-8") as fh:
        json.dump({"python": " ".join(py), "torch_index": TORCH_INDEX, "packages": PACKAGES}, fh)
    info = check_addon()
    if not info.get("ok"):
        log(f"Align: the add-on installed, but its check failed: {info.get('error', info)}")
        return False
    log(f"Align: ready — {info.get('device')} ({info.get('vram_gb')} GB), PyTorch {info.get('torch')}.")
    return True


# ------------------------------------------------------------------ worker process
class AlignerError(RuntimeError):
    pass


class Aligner:
    """One worker process (models stay loaded between songs). Not thread-safe: use from one thread."""

    def __init__(self, model: str = "medium", separate: bool = True, command: list[str] | None = None):
        self.model, self.separate = model, separate
        self._cmd = command
        self._p: subprocess.Popen | None = None
        self._out: queue.Queue = queue.Queue()
        self._id = 0

    def _command(self) -> list[str]:
        if self._cmd:
            return list(self._cmd)
        env_cmd = os.environ.get("MSL_ALIGNER_CMD")              # tests / development
        if env_cmd:
            return json.loads(env_cmd)
        refresh_worker()
        cmd = [addon_python(), "-I", _installed_worker(), "--serve", "--model", self.model]
        return cmd + ([] if self.separate else ["--no-separate"])

    def start(self, timeout: float = 600) -> None:
        if self._p is not None and self._p.poll() is None:
            return
        log_path = os.path.join(addon_dir(), "worker.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self._err = open(log_path, "a", encoding="utf-8", errors="replace")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self._p = subprocess.Popen(self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=self._err, text=True, encoding="utf-8", env=env,
                                   creationflags=_NO_WINDOW)
        self._out = queue.Queue()
        threading.Thread(target=self._reader, args=(self._p, self._out), daemon=True).start()
        msg = self._next(timeout)
        if not msg.get("ready"):
            self.close()
            raise AlignerError(msg.get("error") or "the aligner didn't start")

    @staticmethod
    def _reader(p, out) -> None:
        for line in p.stdout:
            line = line.strip()
            if line.startswith("{"):
                try:
                    out.put(json.loads(line))
                except ValueError:
                    pass
        out.put({"eof": True})

    def _next(self, timeout: float) -> dict:
        try:
            msg = self._out.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise AlignerError("the aligner stopped responding") from None
        if msg.get("eof"):
            self.close()
            raise AlignerError("the aligner closed unexpectedly (see worker.log in the add-on folder)")
        return msg

    def align(self, audio_path: str, lines: list[str], probe: float = 0.0, language: str | None = None,
              timeout: float = 900) -> dict:
        self.start()
        self._id += 1
        req = {"id": self._id, "audio": display_path(audio_path), "lines": lines, "probe": probe,
               "language": language}
        try:
            self._p.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")      # type: ignore[union-attr]
            self._p.stdin.flush()                                              # type: ignore[union-attr]
        except OSError as e:
            self.close()
            raise AlignerError(f"the aligner closed unexpectedly: {e}") from None
        while True:
            msg = self._next(timeout)
            if msg.get("id") == self._id:
                return msg

    def close(self) -> None:
        p, self._p = self._p, None
        if p is not None:
            try:
                if p.poll() is None:
                    p.stdin.write(json.dumps({"quit": True}) + "\n")           # type: ignore[union-attr]
                    p.stdin.flush()                                            # type: ignore[union-attr]
                    p.wait(5)
            except Exception:
                pass
            if p.poll() is None:
                p.kill()
        err = getattr(self, "_err", None)
        if err is not None:
            try:
                err.close()
            except OSError:
                pass


# ------------------------------------------------------------------ using the alignment
@dataclass
class Outcome:
    ok: bool                    # verified at or above the minimum and re-timed
    verify: float               # 0-100: share of the lyric's words heard clearly where they're placed
    text: str = ""              # re-timed lyrics (when ok)
    pins: int = 0               # lines placed from the audio; the rest were interpolated
    lines: int = 0
    reason: str = ""
    language: str = ""
    moved: float = 0.0          # median shift of the pinned lines, seconds
    details: dict = field(default_factory=dict)


_WORD_RE = re.compile(r"\w", re.UNICODE)


def lyric_lines(text: str) -> list[tuple[float, str]]:
    """The timed, non-empty lines to align, in time order (repeated lines appear once per time)."""
    return [(t, body.strip()) for t, body in lrcmod.display_lines(text)
            if t is not None and _WORD_RE.search(body or "")]


def _letters(s: str) -> int:
    return len(_WORD_RE.findall(s or ""))


def map_lines(lines: list[str], segments: list[dict]) -> list[list[list]]:
    """Words of the alignment for each lyric line. One segment per line when the counts agree;
    otherwise words are shared out by their position in the text (letters counted)."""
    if len(segments) == len(lines):
        return [list(s.get("words") or []) for s in segments]
    words = [w for s in segments for w in (s.get("words") or [])]
    bounds, total = [], 0
    for line in lines:
        total += _letters(line)
        bounds.append(total)
    out: list[list] = [[] for _ in lines]
    pos, i = 0, 0
    for w in words:
        n = _letters(w[0])
        mid = pos + n / 2
        while i < len(bounds) - 1 and mid > bounds[i]:
            i += 1
        out[i].append(w)
        pos += n
    return out


def evaluate(text: str, result: dict, duration: float | None, minimum: float) -> Outcome:
    """Turn the worker's result into re-timed lyrics and a verify score; ok only at or above `minimum`."""
    items = lyric_lines(text)
    if len(items) < 2:
        return Outcome(False, 0.0, reason="fewer than 2 timed lines")
    if not result.get("ok"):
        return Outcome(False, 0.0, reason=result.get("error") or "alignment failed", lines=len(items))
    per_line = map_lines([b for _, b in items], result.get("segments") or [])
    n_words = n_ok = 0
    anchors: list[tuple[float, float, float]] = []          # (original time, new time, line probability)
    for (orig, _body), words in zip(items, per_line):
        good = [w for w in words if (w[2] - w[1]) >= 0.02]
        n_words += len(words)
        clear = [w for w in good if w[3] >= WORD_OK]
        n_ok += len(clear)
        if good:
            prob = sum(w[3] for w in good) / len(words)
            start = good[0][1]
            if prob >= PIN_LINE and (duration is None or 0 <= start <= duration):
                anchors.append((orig, start, prob))
    verify = round(100.0 * n_ok / n_words, 1) if n_words else 0.0
    lang = result.get("language") or ""
    # keep the pins in time order: drop any that would make the lyrics run backwards
    pins: dict[str, float] = {}
    last_new = -1.0
    for orig, new, _p in sorted(anchors):
        if new > last_new + 0.05:
            pins[lrcmod.pin_key(orig)] = round(new, 2)
            last_new = new
    base = Outcome(False, verify, pins=len(pins), lines=len(items), language=lang,
                   details={"words": n_words, "clear": n_ok})
    if verify < minimum:
        base.reason = f"only {verify:.0f}% of the words were heard clearly (needs {minimum:.0f}%)"
        return base
    if len(pins) < 2:
        base.reason = "fewer than 2 lines could be placed with confidence"
        return base
    moves = sorted(abs(v - float(k)) for k, v in pins.items())
    base.moved = round(moves[len(moves) // 2], 2)
    base.text = lrcmod.retime(text, pins=pins, interpolate=True)
    base.ok = True
    return base


# ------------------------------------------------------------------ the batch step
def _hash(text: str) -> str:
    import hashlib
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()


def pick_low_confidence(settings, paths) -> list[str]:
    """Proposals (no per-file choice) below the High confidence or Good fit level."""
    from .paths import state_key
    from .state import State

    thr, fthr = float(settings.confident_threshold), float(settings.confident_fit)
    st = State(settings.resolved_state_db())
    try:
        scores = st.pending_scores()
        kinds = st.override_kinds()
    finally:
        st.close()
    out = []
    for p in paths:
        k = state_key(p)
        if k in kinds or k not in scores:
            continue
        c, f = scores[k]
        if (c is not None and c < thr) or (f is not None and f < fthr):
            out.append(k)
    return out


def run(settings, paths, emit, cancel: threading.Event | None = None, aligner: Aligner | None = None,
        everything: bool = False) -> dict:
    """Align the low-confidence proposals among `paths` (all of them with everything=True) and keep
    each re-timed result only when its verify score reaches settings.ai_align_min.
    emit() gets align_start / align_progress / align_done events."""
    from . import fit as fitmod
    from .paths import state_key, to_long_path
    from .state import State
    from .tags import TagFile

    cancel = cancel or threading.Event()
    minimum = float(getattr(settings, "ai_align_min", 70))
    todo = [state_key(p) for p in paths] if everything else pick_low_confidence(settings, paths)
    counts = {"aligned": 0, "rejected": 0, "error": 0, "skipped": 0}
    emit({"type": "align_start", "total": len(todo)})
    own = aligner is None
    al = aligner or Aligner(getattr(settings, "ai_align_model", "medium"))
    st = State(settings.resolved_state_db())
    try:
        for i, path in enumerate(todo, 1):
            if cancel.is_set():
                break
            ev = {"type": "align_progress", "path": path, "done": i, "total": len(todo), "status": "skipped",
                  "verify": None, "reason": "", "kind": "", "confidence": None, "fit": None}
            pend = st.get_pending(path)
            if pend is None or st.get_override(path) is not None:
                counts["skipped"] += 1
                emit(dict(ev, reason="nothing to align"))
                continue
            source, label, text = pend[0], pend[1], pend[2]
            kind = "online" if source == "lrclib" else "local"
            ev["kind"] = kind
            h = _hash(text)
            prev = st.get_aligned(path)
            items = lyric_lines(text)
            try:
                dur = TagFile(to_long_path(path)).duration
            except Exception:
                dur = None
            if prev is not None and prev[0] == h:            # done before for these exact lyrics
                if prev[1] and prev[4]:
                    st.use_aligned(path, kind, text, prev[4], label, prev[5], prev[6])
                    counts["aligned"] += 1
                    emit(dict(ev, status="aligned", verify=prev[2], confidence=prev[5], fit=prev[6],
                              reason="aligned before"))
                else:
                    counts["rejected"] += 1
                    emit(dict(ev, status="rejected", verify=prev[2], reason=prev[3]))
                st.commit()
                continue
            if len(items) < 2:
                counts["skipped"] += 1
                emit(dict(ev, reason="fewer than 2 timed lines"))
                continue
            try:
                result = al.align(path, [b for _, b in items], probe=items[0][0])
            except AlignerError as e:
                counts["error"] += 1
                emit(dict(ev, status="error", reason=str(e)))
                break                                        # the worker is gone: stop the batch
            out = evaluate(text, result, dur, minimum)
            if not out.ok:
                st.set_aligned(path, h, False, out.verify, out.reason, kind=kind)
                counts["rejected" if result.get("ok") else "error"] += 1
                emit(dict(ev, status="rejected" if result.get("ok") else "error", verify=out.verify,
                          reason=out.reason))
                st.commit()
                continue
            peaks = fitmod.peaks_for(display_path(path))
            new_fit = fitmod.fit_score(out.text, dur, dur, peaks)[0]
            old_conf = st.pending_scores().get(path, (None, None))[0]
            conf = round(max(old_conf or 0.0, out.verify), 1)
            new_label = f"{label} · aligned ({out.verify:.0f}% heard)"
            if st.use_aligned(path, kind, text, out.text, new_label, conf, new_fit):
                st.set_aligned(path, h, True, out.verify, "", out.text, conf, new_fit, kind)
                counts["aligned"] += 1
                emit(dict(ev, status="aligned", verify=out.verify, confidence=conf, fit=new_fit,
                          reason=f"{out.pins} of {out.lines} lines placed from the audio, the rest "
                                 f"interpolated; typical shift {out.moved:.2f} s"))
            else:
                counts["skipped"] += 1
                emit(dict(ev, reason="changed or saved meanwhile"))
            st.commit()
    finally:
        st.close()
        if own:
            al.close()
    summary = {"type": "align_done", "counts": counts, "cancelled": cancel.is_set(), "total": len(todo)}
    emit(summary)
    return summary
