"""Align low-confidence lyrics: optional, NVIDIA-only add-on.

The app never imports PyTorch. The add-on is a separate Python environment in
%LOCALAPPDATA%\\mass-synced-lyrics\\aligner (PyTorch with CUDA, stable-ts/Whisper, Demucs, PyAV); its
worker (aligner_worker.py) isolates the vocals and force-aligns the known lyric lines to them. This module:

* detects an NVIDIA GPU (nvidia-smi), installs / checks the add-on, runs the worker process;
* turns the worker's word timings into re-timed lyrics: lines heard clearly become pins and the rest are
  interpolated between them (the same as the player's Interpolate);
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
VERIFY_FLOOR = 30.0       # below this % heard the lyrics are probably not these ones: nothing is changed
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


_PROGRESS_RE = re.compile(r"^Progress (\d+) of (\d+)")
_DOWNLOAD_RE = re.compile(r"^\s*Downloading (\S+?)(?:-\d[^ ]*)?(?:\.whl|\.tar\.gz)?(?: \(([^)]+)\))?\s*$")


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def install(log, cancel: threading.Event | None = None, progress=None) -> bool:
    """Build the add-on environment: about 3 GB of downloads (PyTorch with CUDA, Whisper, Demucs).
    The speech and separation models (~1.6 GB more) download on first use. `log(str)` gets progress
    lines; `progress(fraction 0-1, title, detail)` drives a progress bar. Stops (returns False) as soon
    as `cancel` is set."""
    cancel = cancel or threading.Event()
    progress = progress or (lambda f, t, d: None)
    py = find_system_python()
    if py is None:
        log("Align: Python 3.10, 3.11 or 3.12 is needed to install the add-on (python.org). None was found.")
        return False
    d = addon_dir()
    os.makedirs(d, exist_ok=True)
    venv = os.path.join(d, "venv")
    raw = ["--progress-bar", "raw"]
    # (title, command, share of the bar)
    steps = [
        ("Creating the add-on environment", py + ["-m", "venv", venv], 0.02),
        ("Updating pip", [addon_python(), "-m", "pip", "install", "-U", "pip"], 0.03),
        ("Installing PyTorch with CUDA (about 2.5 GB)", [addon_python(), "-m", "pip", "install", "torch",
                                                         "--index-url", TORCH_INDEX] + raw, 0.75),
        ("Installing Whisper, stable-ts and Demucs", [addon_python(), "-m", "pip", "install"] + PACKAGES + raw,
         0.18),
    ]
    done = 0.0
    for title, cmd, share in steps:
        if cancel.is_set():
            log("Align: installation stopped.")
            return False
        log(f"Align: {title}…")
        progress(done, title, "")
        rc, out = _run_step(cmd, log, cancel, lambda f, det, base=done, sh=share, t=title: progress(
            base + sh * max(0.0, min(1.0, f)), t, det))
        if rc != 0 and "--progress-bar" in cmd and "invalid choice" in out:       # older pip: no raw bars
            rc, out = _run_step([c for c in cmd if c not in raw], log, cancel, lambda f, det: None)
        if cancel.is_set():
            log("Align: installation stopped.")
            return False
        if rc != 0:
            log(f"Align: {title} failed (exit code {rc}).")
            return False
        done += share
        progress(done, title, "done")
    progress(0.99, "Checking the GPU", "")
    shutil.copyfile(worker_source(), _installed_worker())
    with open(os.path.join(d, "installed.json"), "w", encoding="utf-8") as fh:
        json.dump({"python": " ".join(py), "torch_index": TORCH_INDEX, "packages": PACKAGES}, fh)
    info = check_addon()
    if not info.get("ok"):
        log(f"Align: the add-on installed, but its check failed: {info.get('error', info)}")
        return False
    progress(1.0, "Ready", info.get("device") or "")
    log(f"Align: ready — {info.get('device')} ({info.get('vram_gb')} GB), PyTorch {info.get('torch')}.")
    return True


def _run_step(cmd, log, cancel, step_progress) -> tuple[int, str]:
    """Run one install command; feed pip's raw progress lines to step_progress(fraction, detail).
    The command is killed as soon as `cancel` is set."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                             creationflags=_NO_WINDOW)
    except OSError as e:
        log(f"    {e}")
        return 1, str(e)

    def watch():                                  # stop promptly even while pip is quiet
        while p.poll() is None:
            if cancel.wait(0.5):
                if p.poll() is None:
                    p.kill()
                return
    threading.Thread(target=watch, daemon=True).start()
    tail: list[str] = []
    current = ""
    for line in p.stdout:                                     # type: ignore[union-attr]
        line = line.rstrip()
        m = _PROGRESS_RE.match(line)
        if m:
            got, total = int(m.group(1)), int(m.group(2))
            if total:
                step_progress(got / total, f"{current}{_fmt_bytes(got)} of {_fmt_bytes(total)}")
            continue
        md = _DOWNLOAD_RE.match(line)
        if md and not line.rstrip().endswith(".metadata"):
            current = f"{md.group(1)}: "
        tail = (tail + [line])[-20:]
        if line and not line.startswith(("  ", "Requirement already")):
            log("    " + line[:200])
    rc = p.wait()
    if cancel.is_set():
        return 1, ""
    return rc, "\n".join(tail)


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
        self.loaded = False             # this worker has finished a song, so its models are in memory

    def running(self) -> bool:
        return self._p is not None and self._p.poll() is None

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
        self.loaded = False
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
              timeout: float = 900, on_progress=None) -> dict:
        """on_progress(stage, frac): called (on this thread) as the worker reports its steps."""
        if not self.running() and on_progress is not None:
            on_progress("start", None)
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
            if msg.get("id") != self._id:
                continue
            if "progress" in msg:
                if on_progress is not None:
                    try:
                        on_progress(str(msg["progress"]), msg.get("frac"))
                    except Exception:
                        pass
                continue
            if msg.get("ok"):
                self.loaded = True
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


# ------------------------------------------------------------------ progress of one song
# The steps of aligning one song, in order, with what the progress bar says during each.
STAGES = (("start", "Starting the GPU worker"), ("load_separator", "Loading the vocal model"),
          ("decode", "Reading the audio"), ("separate", "Isolating the vocals"),
          ("load_whisper", "Loading the speech model"), ("language", "Detecting the language"),
          ("align", "Placing the lyric words"), ("transcribe", "Listening to what was sung"),
          ("cleanup", "Freeing GPU memory"), ("check", "Checking and saving"))
STAGE_LABEL = dict(STAGES)
STAGE_ORDER = [k for k, _ in STAGES]
ONCE = {"start", "load_separator", "load_whisper"}     # only when the worker (or its models) isn't loaded yet
# Estimated cost of each step: (fixed seconds, seconds per minute of audio). Measured on an RTX 3070
# (medium model, songs of 2.8-5.7 minutes: about 26 s for a 3.6-minute song); replaced by this PC's
# own measurements as songs are aligned (timings.json).
DEFAULT_COST = {"start": (3.0, 0.0), "load_separator": (0.8, 0.0), "decode": (0.1, 0.2),
                "separate": (0.3, 1.6), "load_whisper": (5.3, 0.0), "language": (0.3, 0.0),
                "align": (0.5, 1.0), "transcribe": (1.0, 4.4), "cleanup": (0.1, 0.0), "check": (0.3, 0.3)}
PER_MINUTE = {"decode", "separate", "align", "transcribe", "check"}
_cost_lock = threading.Lock()
_cost_cache: dict | None = None


def _timings_path() -> str:
    return os.path.join(addon_dir(), "timings.json")


def stage_costs() -> dict:
    """{stage: (fixed s, s per minute)}: this PC's measurements where there are some."""
    global _cost_cache
    with _cost_lock:
        if _cost_cache is None:
            _cost_cache = dict(DEFAULT_COST)
            try:
                with open(_timings_path(), encoding="utf-8") as f:
                    for k, v in json.load(f).items():
                        if k in DEFAULT_COST and isinstance(v, list) and len(v) == 2:
                            _cost_cache[k] = (float(v[0]), float(v[1]))
            except (OSError, ValueError, TypeError):
                pass
        return dict(_cost_cache)


def learn_timings(times: dict, duration: float | None) -> None:
    """Fold one song's measured step times into the estimates (moving average, kept on disk)."""
    global _cost_cache
    costs = stage_costs()
    minutes = max(0.25, (duration or 0) / 60.0)
    for k, t in times.items():
        if k not in costs or t is None or t < 0 or t > 1800:
            continue
        fixed, per = costs[k]
        if k in PER_MINUTE:
            per = per * 0.7 + max(0.0, t - fixed) / minutes * 0.3
        else:
            fixed = fixed * 0.7 + t * 0.3
        costs[k] = (round(fixed, 3), round(per, 4))
    with _cost_lock:
        _cost_cache = costs
    try:
        os.makedirs(addon_dir(), exist_ok=True)
        tmp = _timings_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: [float(x) for x in v] for k, v in costs.items()}, f)
        os.replace(tmp, _timings_path())
    except OSError:
        pass


class SongProgress:
    """How far one song's alignment is, as a share of its estimated time. Each step gets a slice of the
    bar sized by its estimate (from the song's length and this PC's measured speed); within a step the
    worker's own progress is used where it reports one (vocal isolation, listening), otherwise the time
    spent against the estimate. Written by the aligner thread, read by the GUI."""

    def __init__(self, path: str, duration: float | None, warm: bool, running: bool, kind: str = "song",
                 clock=None):
        import time
        self._clock = clock or time.monotonic
        self.path, self.kind = path, kind
        self.duration = duration if duration and duration > 0 else None
        self.costs = stage_costs()
        skip = set() if not running else ({"start"} | (ONCE if warm else set()))
        self.plan = [k for k in STAGE_ORDER if k not in skip]
        self.stage, self.frac, self.t0 = self.plan[0], None, self._clock()
        self.started = self.t0
        self.times: dict = {}
        self.retries = 0

    def estimate(self, k: str) -> float:
        fixed, per = self.costs.get(k, (1.0, 0.0))
        return max(0.05, fixed + per * (self.duration or 240.0) / 60.0)

    def set_duration(self, d: float | None) -> None:
        if d and d > 0:
            self.duration = d

    def step(self, stage: str, frac=None) -> None:
        if stage not in STAGE_LABEL:
            return
        now = self._clock()
        if stage != self.stage:
            self.times[self.stage] = self.times.get(self.stage, 0.0) + now - self.t0
            if stage not in self.plan:                 # e.g. the models had to be loaded again
                self.plan.insert(min((i for i, k in enumerate(self.plan)
                                      if STAGE_ORDER.index(k) > STAGE_ORDER.index(stage)), default=len(self.plan)),
                                 stage)
            self.stage, self.t0 = stage, now
        self.frac = None if frac is None else max(0.0, min(1.0, float(frac)))

    def restart(self) -> None:
        """The worker crashed and is being restarted for another try: it has to load everything again."""
        self.retries += 1
        self.plan = list(STAGE_ORDER)
        self.stage, self.frac, self.t0 = "start", None, self._clock()

    def finish(self) -> dict:
        now = self._clock()
        self.times[self.stage] = self.times.get(self.stage, 0.0) + now - self.t0
        self.stage = "done"
        return dict(self.times)

    def snapshot(self) -> dict:
        """{"frac": 0..1 of the whole song, "stage", "label", "stage_frac", "left": seconds}"""
        plan, stage = list(self.plan), self.stage
        if stage == "done":
            return {"frac": 1.0, "stage": stage, "label": "Done", "stage_frac": 1.0, "left": 0.0}
        est = {k: self.estimate(k) for k in plan}
        total = sum(est.values()) or 1.0
        i = plan.index(stage) if stage in plan else 0
        before = sum(est[k] for k in plan[:i])
        cur = est.get(stage, 1.0)
        spent = self._clock() - self.t0
        if self.frac is not None:
            sf = self.frac
        else:                                          # no report: time spent against the estimate,
            sf = 1.0 - 0.5 ** (spent / cur)            # easing off so it never claims to be done
        sf = max(0.0, min(0.99, sf))
        left_cur = max(cur - spent, cur * (1 - sf)) if self.frac is None else \
            (spent / sf * (1 - sf) if sf > 0.05 else cur * (1 - sf))
        left = left_cur + sum(est[k] for k in plan[i + 1:])
        label = STAGE_LABEL.get(stage, stage)
        if self.retries:
            label += " (retrying)"
        return {"frac": max(0.0, min(0.999, (before + cur * sf) / total)), "stage": stage, "label": label,
                "stage_frac": sf, "left": max(0.0, left)}

    def song_estimate(self, duration: float | None = None) -> float:
        """Seconds for another song like this one with everything loaded."""
        d = duration or self.duration or 240.0
        return sum(self.costs[k][0] + self.costs[k][1] * d / 60.0 for k in STAGE_ORDER if k not in ONCE)


# ------------------------------------------------------------------ using the alignment
@dataclass
class Outcome:
    ok: bool                    # re-timed (from the lines it was sure of; see partial)
    verify: float               # 0-100: share of the lyric's words heard clearly where they're placed
    text: str = ""              # re-timed lyrics (when ok)
    pins: int = 0               # lines placed from the audio; the rest were interpolated
    lines: int = 0
    reason: str = ""
    language: str = ""
    moved: float = 0.0          # median shift of the pinned lines, seconds
    details: dict = field(default_factory=dict)
    pin_map: dict = field(default_factory=dict)      # pin_key(original time) -> new time, lines the aligner placed
    heard: list = field(default_factory=list)        # [[word, start, end, probability], ...] as heard
    ends: dict = field(default_factory=dict)         # pin_key(line) -> time the line ends early (a gap follows)
    line_conf: dict = field(default_factory=dict)    # pin_key(line) -> 0-1 how surely it was heard
    partial: bool = False       # ok, but fewer words heard than the minimum: the lines it was sure of are
    minimum: float = 0.0        # placed, the rest interpolated; not counted as verified (left to check)


def _too_little(base: "Outcome", verify: float, minimum: float, what: str) -> bool:
    """Whether to give up. Below the minimum is not a reason by itself: the lines it is sure of are still
    used and the rest interpolated (marked partial). Only too little heard to trust any of it is."""
    base.minimum = minimum
    if verify < min(minimum, VERIFY_FLOOR):
        base.reason = (f"only {verify:.0f}% of the lyric's words were {what} — probably not these lyrics "
                       f"(at least {min(minimum, VERIFY_FLOOR):.0f}% is needed to use any of it)")
        return True
    if base.pins < 2:
        base.reason = "fewer than 2 lines could be placed with confidence"
        return True
    base.partial = verify < minimum
    return False


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


def heard_words(result: dict) -> list:
    """All words the aligner placed, in time order: [[word, start, end, probability], ...]."""
    out = [list(w) for s in (result.get("segments") or []) for w in (s.get("words") or [])
           if len(w) >= 4 and (w[2] - w[1]) >= 0.02]
    out.sort(key=lambda w: w[1])
    return out


_TOKEN_RE = re.compile(r"[\w']+", re.UNICODE)
MATCH_SIM = 70          # % similarity for a transcript word to count as a lyric word
GAP_AFTER = 3.0         # seconds without lyrics after a placed line before it gets an end mark
WORD_DUR = 0.28         # typical sung word, for backing up over unheard words at a line's start
MAX_WORD = 1.5          # longer "words" are the recogniser stretching a word over silence


def _tokens(text: str) -> list[str]:
    return [t.replace("'", "") for t in _TOKEN_RE.findall((text or "").lower()) if t.replace("'", "")]


def match_transcript(lines: list[str], transcript: list) -> list[list[tuple]]:
    """Which lyric words were actually heard, and when. Monotonic alignment of the lyric words to the
    transcript: transcript words that match no lyric (ad-libs, talk, crowd) are skipped cheaply, lyric
    words that weren't heard cost a little more, and similar words (≥ MATCH_SIM %) score.
    Returns, per lyric line, [(word position in the line, start, end, probability, similarity), ...]."""
    from rapidfuzz import fuzz
    from rapidfuzz.process import cdist

    lw: list[tuple[int, int, str]] = []                 # (line, position in line, token)
    for li, line in enumerate(lines):
        for pos, tok in enumerate(_tokens(line)):
            lw.append((li, pos, tok))
    tw: list[tuple[str, float, float, float]] = []
    for w in transcript or []:
        toks = _tokens(w[0])
        if toks and len(w) >= 4:
            tw.append(("".join(toks), float(w[1]), float(w[2]), float(w[3])))
    out: list[list[tuple]] = [[] for _ in lines]
    if not lw or not tw:
        return out
    # plain Python floats: numpy's float32 would end up in the stored per-line certainty (JSON)
    sim = cdist([t for _, _, t in lw], [t for t, _, _, _ in tw], scorer=fuzz.ratio).tolist()
    n, m = len(lw), len(tw)
    SKIP_T, SKIP_L, MISS = -0.15, -0.6, -1.0
    neg = -1e9
    D = [[0.0] * (m + 1) for _ in range(n + 1)]
    B = [[0] * (m + 1) for _ in range(n + 1)]             # 1 = pair, 2 = skip lyric word, 3 = skip transcript
    for i in range(1, n + 1):
        D[i][0] = D[i - 1][0] + SKIP_L
        B[i][0] = 2
        row, prev, srow = D[i], D[i - 1], sim[i - 1]
        brow = B[i]
        for j in range(1, m + 1):
            s = srow[j - 1]
            gain = 2.0 if s >= 85 else (1.0 if s >= MATCH_SIM else MISS)
            best, how = prev[j - 1] + gain, 1
            v = prev[j] + SKIP_L
            if v > best:
                best, how = v, 2
            v = row[j - 1] + SKIP_T
            if v > best:
                best, how = v, 3
            row[j], brow[j] = best, how
    # free end in the transcript (talk after the last lyric costs nothing)
    j = max(range(m + 1), key=lambda k: D[n][k] if k else neg)
    i = n
    while i > 0 and j > 0:
        how = B[i][j]
        if how == 1:
            s = sim[i - 1][j - 1]
            if s >= MATCH_SIM:
                li, pos, _ = lw[i - 1]
                t = tw[j - 1]
                out[li].append((pos, float(t[1]), float(t[2]), float(t[3]), float(s) / 100.0))
            i, j = i - 1, j - 1
        elif how == 2:
            i -= 1
        else:
            j -= 1
    for words in out:
        words.reverse()
    return out


def _onset(words) -> float | None:
    """Where a line starts, from its aligned words: the first word with a believable length (words
    stretched over silence are skipped), backed up over the words before it."""
    for pos, w in enumerate(words):
        d = w[2] - w[1]
        if 0.02 <= d <= MAX_WORD:
            return max(0.0, w[1] - WORD_DUR * pos)
    return None


def _evaluate_heard(text: str, items, result: dict, duration, minimum: float, mode: str) -> "Outcome":
    """Live-safe evaluation: place lines from the words actually heard (transcript), using the forced
    alignment's more precise onset only where both agree."""
    lines = [b for _, b in items]
    heard = match_transcript(lines, result.get("transcript") or [])
    forced = map_lines(lines, result.get("segments") or [])
    n_words = sum(len(_tokens(b)) for b in lines)
    n_ok = sum(len(h) for h in heard)
    verify = round(100.0 * n_ok / n_words, 1) if n_words else 0.0
    anchors, conf, last_end = [], {}, {}
    for (orig, body), words, fw in zip(items, heard, forced):
        total = max(1, len(_tokens(body)))
        if not words:
            continue
        cover = len(words) / total
        quality = sum(w[3] * w[4] for w in words) / len(words)
        c = round(cover * (0.5 + 0.5 * quality), 3)
        conf[lrcmod.pin_key(orig)] = c
        last_end[lrcmod.pin_key(orig)] = min(words[-1][2], words[-1][1] + MAX_WORD)
        if cover < 0.5 or (len(words) < 2 and total > 2):
            continue
        # line start: each heard word suggests one (its start, backed up over the words before it in
        # the line); words stretched over silence by the recogniser are ignored; median of the first few
        sugg = [w[1] - WORD_DUR * w[0] for w in words if (w[2] - w[1]) <= MAX_WORD][:3]
        if not sugg:
            continue
        sugg.sort()
        est = max(0.0, sugg[len(sugg) // 2])
        fon = _onset(fw)
        start = fon if fon is not None and abs(fon - est) <= 1.0 else est
        if duration is None or 0 <= start <= duration:
            anchors.append((orig, start, c))
    pins: dict[str, float] = {}
    last_new = -1.0
    for orig, new, _c in sorted(anchors):
        if new > last_new + 0.05:
            pins[lrcmod.pin_key(orig)] = round(new, 2)
            last_new = new
    base = Outcome(False, verify, pins=len(pins), lines=len(items), language=result.get("language") or "",
                   details={"words": n_words, "clear": n_ok, "method": "heard"}, pin_map=dict(pins),
                   heard=[list(w) for w in result.get("transcript") or []])
    base.line_conf = {k: v for k, v in conf.items()}
    if _too_little(base, verify, minimum, "heard being sung"):
        return base
    moves = sorted(abs(v - float(k)) for k, v in pins.items())
    base.moved = round(moves[len(moves) // 2], 2)
    timed = lrcmod.retime(text, pins=pins, interpolate=True, mode=mode, duration=duration)
    # end placed lines early when nothing from the lyrics follows for a while (talk, a break)
    new_lines = lrcmod.display_lines(timed)
    orig_lines = lrcmod.display_lines(text)
    ends: dict[str, float] = {}
    for i, ((ot, ob), (nt, nb)) in enumerate(zip(orig_lines, new_lines)):
        k = lrcmod.pin_key(ot) if ot is not None else None
        if k not in pins or k not in last_end or i + 1 >= len(new_lines):
            continue
        nxt_t, nxt_b = new_lines[i + 1]
        if not (nxt_b or "").strip():
            continue                                       # already followed by a gap
        end = last_end[k] + 0.4
        if nxt_t is not None and nxt_t - end >= GAP_AFTER and end > nt:
            ends[k] = round(end, 2)
    base.ends = ends
    base.text = lrcmod.insert_blank_lines(timed, ends.values())
    base.ok = True
    return base


def evaluate(text: str, result: dict, duration: float | None, minimum: float, mode: str = "slope") -> Outcome:
    """Turn the worker's result into re-timed lyrics and a verify score; ok only at or above `minimum`.
    mode: how lines the aligner couldn't place follow the placed ones ("slope" = Interpolate,
    "ripple" = Ripple changes)."""
    items = lyric_lines(text)
    if len(items) < 2:
        return Outcome(False, 0.0, reason="fewer than 2 timed lines")
    if not result.get("ok"):
        return Outcome(False, 0.0, reason=result.get("error") or "alignment failed", lines=len(items))
    if result.get("transcript"):
        return _evaluate_heard(text, items, result, duration, minimum, mode)
    per_line = map_lines([b for _, b in items], result.get("segments") or [])
    line_conf: dict[str, float] = {}
    n_words = n_ok = 0
    anchors: list[tuple[float, float, float]] = []          # (original time, new time, line probability)
    for (orig, _body), words in zip(items, per_line):
        good = [w for w in words if (w[2] - w[1]) >= 0.02]
        n_words += len(words)
        clear = [w for w in good if w[3] >= WORD_OK]
        n_ok += len(clear)
        start = _onset(words)
        if good and start is not None:
            prob = sum(w[3] for w in good) / len(words)
            line_conf[lrcmod.pin_key(orig)] = round(prob, 3)
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
                   details={"words": n_words, "clear": n_ok}, pin_map=dict(pins), heard=heard_words(result),
                   line_conf=line_conf)
    if _too_little(base, verify, minimum, "heard clearly"):
        return base
    moves = sorted(abs(v - float(k)) for k, v in pins.items())
    base.moved = round(moves[len(moves) // 2], 2)
    base.text = lrcmod.retime(text, pins=pins, interpolate=True, mode=mode, duration=duration)
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


def align_one(settings, path: str, al: "Aligner", st, minimum: float, force: bool = False,
              progress: "SongProgress | None" = None) -> dict:
    """Align one song's proposal. Returns an align_progress-style dict (status aligned / rejected /
    error / skipped). Raises AlignerError when the worker itself fails (not this song).
    force=True (asked for this song): align again even if it was aligned or judged before; a song
    aligned before is aligned again from its original timing."""
    from . import fit as fitmod
    from .paths import to_long_path
    from .tags import TagFile

    ev = {"type": "align_progress", "path": path, "status": "skipped", "verify": None, "reason": "",
          "kind": "", "confidence": None, "fit": None}
    pend = st.get_pending(path)
    if pend is None:
        return dict(ev, reason="no found lyrics waiting to be saved")
    if st.get_override(path) is not None:
        return dict(ev, reason="it has your own choice — Reset to default first, or use Auto-align in the player")
    source, label, text = pend[0], pend[1], pend[2]
    kind = "online" if source == "lrclib" else "local"
    ev["kind"] = kind
    h = _hash(text)
    prev = st.get_aligned(path)
    base = text                                                  # the timing the aligner starts from
    if force:
        if prev is not None and prev[1] and prev[4] and lrcmod.same_lyrics(prev[4], text):
            det = st.get_aligned_detail(path) or {}
            if det.get("orig"):
                base = det["orig"]
                label = re.split(r" · (?:partly )?aligned \(", label)[0]
        prev = None
    if (prev is not None and not prev[1] and "(needs " in (prev[3] or "")
            and (prev[2] or 0) >= min(minimum, VERIFY_FLOOR)):
        prev = None        # turned down by an older version only for being under the minimum: use it now
    if prev is not None and prev[0] == h:                        # done before for these exact lyrics
        if prev[1] and prev[4]:
            st.use_aligned(path, kind, text, prev[4], label, prev[5], prev[6])
            st.commit()
            return dict(ev, status="aligned", verify=prev[2], confidence=prev[5], fit=prev[6],
                        reason="aligned before", partial=(prev[3] or "").startswith("partly"))
        return dict(ev, status="rejected", verify=prev[2], reason=prev[3])
    if prev is not None and prev[1] and prev[4] and lrcmod.same_lyrics(prev[4], text):
        return dict(ev, status="aligned", verify=prev[2], confidence=prev[5], fit=prev[6],
                    reason="already aligned", partial=(prev[3] or "").startswith("partly"))
    items = lyric_lines(base)
    if len(items) < 2:
        return dict(ev, reason="fewer than 2 timed lines")
    try:
        dur = TagFile(to_long_path(path)).duration
    except Exception:
        dur = None
    if progress is not None:
        progress.set_duration(dur)
        result = al.align(path, [b for _, b in items], probe=items[0][0], on_progress=progress.step)
        progress.step("check")
    else:
        result = al.align(path, [b for _, b in items], probe=items[0][0])   # AlignerError propagates
    if dur is None and result.get("duration"):
        dur = float(result["duration"])
    mode = "ripple" if getattr(settings, "ripple", False) else "slope"
    out = evaluate(base, result, dur, minimum, mode)
    if not out.ok:
        if result.get("ok"):                     # a judgement about these lyrics: remember it
            st.set_aligned(path, h, False, out.verify, out.reason, kind=kind, orig=base, heard=out.heard,
                           mode=mode)
            st.commit()
            return dict(ev, status="rejected", verify=out.verify, reason=out.reason)
        return dict(ev, status="error", reason=out.reason)       # this song failed (bad file, …): retry later
    peaks = fitmod.peaks_for(display_path(path))
    new_fit = fitmod.fit_score(out.text, dur, dur, peaks)[0]
    old_conf = st.pending_scores().get(path, (None, None))[0]
    if out.partial:            # not verified: its Match isn't raised, so it stays low confidence for you to check
        conf = old_conf
        new_label = f"{label} · partly aligned ({out.verify:.0f}% heard)"
        note = (f"partly: {out.verify:.0f}% of words heard (below {minimum:.0f}%), so only the lines it was sure "
                "of were placed")
    else:
        conf = round(max(old_conf or 0.0, out.verify), 1)
        new_label = f"{label} · aligned ({out.verify:.0f}% heard)"
        note = ""
    if not st.use_aligned(path, kind, text, out.text, new_label, conf, new_fit, base=base):
        return dict(ev, reason="changed or saved meanwhile")
    st.set_aligned(path, h, True, out.verify, note, out.text, conf, new_fit, kind, orig=base, pins=out.pin_map,
                   heard=out.heard, mode=mode, ends=out.ends, confs=out.line_conf)
    st.commit()
    return dict(ev, status="aligned", verify=out.verify, confidence=conf, fit=new_fit, partial=out.partial,
                reason=f"{out.pins} of {out.lines} lines placed from the audio, the rest interpolated; "
                       f"typical shift {out.moved:.2f} s")


def run(settings, paths, emit, cancel: threading.Event | None = None, aligner: Aligner | None = None,
        everything: bool = False) -> dict:
    """Align the low-confidence proposals among `paths` (all of them with everything=True), one after
    another, and keep each re-timed result only when its verify score reaches settings.ai_align_min.
    Stops at the first worker failure. emit() gets align_start / align_progress / align_done events."""
    from .paths import state_key
    from .state import State

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
            try:
                ev = align_one(settings, path, al, st, minimum)
            except AlignerError as e:
                counts["error"] += 1
                emit({"type": "align_progress", "path": path, "done": i, "total": len(todo), "status": "error",
                      "verify": None, "reason": str(e), "kind": "", "confidence": None, "fit": None})
                break
            counts[ev["status"]] += 1
            ev.update(done=i, total=len(todo))
            emit(ev)
    finally:
        st.close()
        if own:
            al.close()
    summary = {"type": "align_done", "counts": counts, "cancelled": cancel.is_set(), "total": len(todo)}
    emit(summary)
    return summary


def is_low_confidence(settings, confidence: float | None, fit: float | None) -> bool:
    return (confidence is not None and confidence < float(settings.confident_threshold)) or \
        (fit is not None and fit < float(settings.confident_fit))


class _Retrying:
    """What align_one sees as the aligner: the service's worker, restarted once if it fails."""

    def __init__(self, svc: "AlignService", settings, prog: SongProgress):
        self.svc, self.settings, self.prog = svc, settings, prog

    def align(self, path, lines, probe=0.0, on_progress=None):
        return self.svc._align_retrying(self.settings, self.prog, path, lines, probe)


class AlignService:
    """Aligns songs in the background while Find keeps running. Songs are queued as they are found.
    When the aligner fails it pauses (the queue keeps filling) and emits align_error; the app then
    calls resume() (ignore the error and go on), disable() (stop using the feature) or stop()."""

    PER_SONG_ERRORS_BEFORE_PAUSE = 3

    def __init__(self, settings_fn, emit, aligner_factory=None, handoff=None):
        """handoff(aligner): when the service ends with a healthy worker and Keep models loaded is on,
        the worker is handed over (the app keeps it for the next service) instead of being shut down."""
        from collections import deque
        self._settings_fn = settings_fn
        self._handoff = handoff
        self._emit = emit
        self._factory = aligner_factory or (lambda s: Aligner(getattr(s, "ai_align_model", "medium")))
        self._q: "deque[tuple]" = deque()      # ("song", path, force) or ("raw", path, lines, probe, token)
        self._queued: set[str] = set()
        self._cv = threading.Condition()
        self.paused = False
        self.stopped = False
        self.busy = False
        self.counts = {"aligned": 0, "rejected": 0, "error": 0, "skipped": 0}
        self.done = 0
        self._thread: threading.Thread | None = None
        self._aligner: Aligner | None = None
        self.current: SongProgress | None = None     # the song being aligned now (read by the GUI)
        self.current_token = None                    # its Auto-align token, for a player request

    # -- control (any thread)
    def submit(self, paths, force: bool = False) -> int:
        from .paths import state_key
        n = 0
        with self._cv:
            if self.stopped:
                return 0
            for p in paths:
                k = state_key(p)
                if k in self._queued:
                    continue
                self._queued.add(k)
                self._q.append(("song", k, force))
                n += 1
            self._cv.notify_all()
        if n:
            self._ensure_thread()
        return n

    def request_raw(self, path: str, lines: list[str], probe: float, token, duration: float | None = None) -> None:
        """Align these lines for the player, ahead of the queued songs. Nothing is stored: the result
        comes back as an align_raw event (with `token`) for the player to apply as edits."""
        with self._cv:
            if self.stopped:
                return
            self._q.appendleft(("raw", path, list(lines), float(probe or 0), token, duration))
            self._cv.notify_all()
        self._ensure_thread()

    def pending(self) -> int:
        with self._cv:
            return sum(1 for it in self._q if it[0] == "song")

    def raw_waiting(self, token) -> bool:
        with self._cv:
            return any(it[0] == "raw" and it[4] == token for it in self._q)

    def idle(self) -> bool:
        with self._cv:
            return not self._q and not self.busy

    def resume(self) -> None:
        with self._cv:
            self.paused = False
            self._cv.notify_all()

    def stop(self) -> None:
        """Finish the current song, drop the queue and shut the worker down."""
        with self._cv:
            self.stopped = True
            dropped = [it for it in self._q if it[0] == "raw"]
            self._q.clear()
            self._queued.clear()
            self._cv.notify_all()
        for it in dropped:                      # the player is waiting for these: tell it they won't come
            self._emit({"type": "align_raw", "token": it[4], "path": it[1],
                        "result": {"ok": False, "error": "aligning was stopped"}})

    def reset_counts(self) -> None:
        self.counts = {k: 0 for k in self.counts}
        self.done = 0

    def join(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    # -- worker thread
    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True, name="msl-align")
            self._thread.start()

    IDLE_RELEASE = 120.0       # seconds with nothing to do before the worker (and its GPU memory) is let go

    def _loop(self) -> None:
        import time

        from .state import State
        st = None
        streak = 0
        idle_since = time.monotonic()
        try:
            while True:
                with self._cv:
                    while not self.stopped and (self.paused or not self._q):
                        if self.busy and not self.paused:
                            self.busy = False
                            idle_since = time.monotonic()
                            self._emit({"type": "align_idle", "counts": dict(self.counts), "done": self.done})
                        if (self._aligner is not None and not self.paused and not self._keep()
                                and time.monotonic() - idle_since > self.IDLE_RELEASE):
                            self._aligner.close()
                            self._aligner = None
                        self._cv.wait(1.0)
                    if self.stopped:
                        return
                    item = self._q.popleft()
                    self.busy = True
                    if item[0] == "song":
                        self._queued.discard(item[1])
                try:
                    s = self._settings_fn()
                    if item[0] == "raw":
                        _, path, lines, probe, token, duration = item
                        prog = self._progress(path, duration, "raw")
                        self.current_token = token
                        try:
                            if nvidia_gpu() is None:
                                raise AlignerError("no NVIDIA GPU was found (nvidia-smi isn't available)")
                            result = self._align_retrying(s, prog, path, lines, probe)
                        except Exception as e:
                            msg = str(e) if isinstance(e, AlignerError) else f"{type(e).__name__}: {e}"
                            self._done_progress(prog, learn=False)
                            self._emit({"type": "align_raw", "token": token, "path": path,
                                        "result": {"ok": False, "error": msg}})
                            self._pause(msg, path)
                            continue
                        prog.step("check")
                        self._done_progress(prog)
                        self._emit({"type": "align_raw", "token": token, "path": path, "result": result})
                        continue
                    _, path, force = item
                    if st is None:
                        st = State(s.resolved_state_db())
                    if not force:
                        c, f = st.pending_scores().get(path, (None, None))
                        if not is_low_confidence(s, c, f):
                            continue
                    self._emit({"type": "align_song_start", "path": path})
                    prog = self._progress(path, None, "song")
                    try:
                        if nvidia_gpu() is None:
                            raise AlignerError("no NVIDIA GPU was found (nvidia-smi isn't available)")
                        ev = align_one(s, path, _Retrying(self, s, prog), st, float(getattr(s, "ai_align_min", 70)),
                                       force=force, progress=prog)
                    except Exception as e:                  # the aligner itself failed (or something unexpected)
                        msg = str(e) if isinstance(e, AlignerError) else f"{type(e).__name__}: {e}"
                        self._done_progress(prog, learn=False)
                        self._fail(path, msg)
                        streak = 0
                        continue
                    self._done_progress(prog, learn=ev["status"] in ("aligned", "rejected"))
                    self.done += 1
                    self.counts[ev["status"]] += 1
                    ev["queued"] = self.pending()
                    self._emit(ev)
                    streak = streak + 1 if ev["status"] == "error" else 0
                    if streak >= self.PER_SONG_ERRORS_BEFORE_PAUSE:
                        streak = 0
                        self._pause(f"{self.PER_SONG_ERRORS_BEFORE_PAUSE} songs in a row could not be aligned. "
                                    f"Last: {ev['reason']}", path)
                except Exception as e:                 # never let the aligner's thread die quietly
                    if st is not None:
                        try:
                            st.close()
                        except Exception:
                            pass
                        st = None
                    self._pause(f"Unexpected error while aligning: {type(e).__name__}: {e}", item[1])
        finally:
            al, self._aligner = self._aligner, None
            if al is not None:
                if self._handoff is not None and self._keep() and al.running() and not self.paused:
                    try:
                        self._handoff(al)
                    except Exception:
                        al.close()
                else:
                    al.close()
            if st is not None:
                st.close()
            self.busy = False

    def _keep(self) -> bool:
        try:
            return bool(getattr(self._settings_fn(), "ai_keep_loaded", True))
        except Exception:
            return False

    def release_idle(self) -> None:
        """Keep models loaded was switched off: let the worker go now if nothing is being aligned."""
        with self._cv:
            if not self.busy and not self._q and self._aligner is not None:
                self._aligner.close()
                self._aligner = None

    def _progress(self, path: str, duration, kind: str) -> SongProgress:
        al = self._aligner
        running = al is not None and al.running()
        self.current = SongProgress(path, duration, warm=running and al.loaded, running=running, kind=kind)
        return self.current

    def _done_progress(self, prog: SongProgress, learn: bool = True) -> None:
        times = prog.finish()
        if learn:
            learn_timings(times, prog.duration)
        if self.current is prog:
            self.current = None
            self.current_token = None

    def _align_retrying(self, s, prog: SongProgress, path: str, lines, probe: float) -> dict:
        """Run the worker; if it crashes or stops answering, start a fresh one and try once more
        (most failures are one-off: a driver hiccup, memory left over from the previous song)."""
        for attempt in (1, 2):
            if self._aligner is None:
                self._aligner = self._factory(s)
            try:
                return self._aligner.align(path, lines, probe=probe, on_progress=prog.step)
            except AlignerError:
                if self._aligner is not None:
                    self._aligner.close()
                    self._aligner = None
                if attempt == 2 or self.stopped:
                    raise
                prog.restart()
        raise AlignerError("unreachable")

    def _fail(self, path: str, error: str) -> None:
        """The aligner failed on this song: it keeps its found lyrics, the worker is restarted for the
        next one, and alignment pauses until the user decides."""
        if self._aligner is not None:
            self._aligner.close()
            self._aligner = None
        self.done += 1
        self.counts["error"] += 1
        self._emit({"type": "align_progress", "path": path, "status": "error", "verify": None, "reason": error,
                    "kind": "", "confidence": None, "fit": None, "queued": self.pending()})
        self._pause(error, path)

    def _pause(self, error: str, path: str) -> None:
        with self._cv:
            if self.stopped:
                return
            self.paused = True
        self._emit({"type": "align_error", "error": error, "path": path, "queued": self.pending()})
