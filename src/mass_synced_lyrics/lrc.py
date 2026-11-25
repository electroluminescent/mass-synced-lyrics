"""LRC parsing, validation and text decoding."""
from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field

TS_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
LINE_TS_RE = re.compile(r"^\s*(?:\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]\s*)+")
HEADER_RE = re.compile(r"^\s*\[([A-Za-z#]+):(.*)\]\s*$")

PREFERRED_ENCODINGS = ("cp1252", "cp1251", "cp932", "gb18030", "big5", "cp949", "cp1250", "cp1253",
                       "cp1254", "cp1257")

# Seconds of slack allowed between the last timestamp and the track length.
END_TOLERANCE = 3.0


@dataclass
class LrcInfo:
    synced: bool
    timed_lines: int
    last_ts: float | None
    headers: dict = field(default_factory=dict)

    @property
    def length_header(self) -> float | None:
        return parse_length(self.headers.get("length", ""))


def decode_bytes(data: bytes) -> str:
    """Decode an .lrc file whose encoding is unknown."""
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        results = list(from_bytes(data))
        if results:
            low = min(m.chaos for m in results)
            tied = [m for m in results if m.chaos <= low + 0.01]
            # Short lyric files are often ambiguous between code pages; prefer
            # the ones most common for legacy .lrc files.
            for enc in PREFERRED_ENCODINGS:
                for m in tied:
                    if m.encoding == enc:
                        return str(m)
            return str(tied[0])
    except Exception:  # pragma: no cover
        pass
    return data.decode("cp1252", errors="replace")


def clean_text(text: str) -> str:
    """Normalise line endings and strip BOM / surrounding blank lines."""
    text = text.replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    return "\n".join(lines).strip()


def _ts_to_seconds(m: re.Match) -> float:
    minutes, seconds, frac = m.group(1), m.group(2), m.group(3)
    value = int(minutes) * 60 + int(seconds)
    if frac:
        value += int(frac) / (10 ** len(frac))
    return float(value)


def parse_length(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    m = re.fullmatch(r"(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?", value)
    if m:
        return _ts_to_seconds(m)
    try:
        return float(value)
    except ValueError:
        return None


def analyze(text: str) -> LrcInfo:
    headers: dict[str, str] = {}
    last = None
    timed = 0
    for line in (text or "").splitlines():
        m = LINE_TS_RE.match(line)
        if m:
            timed += 1
            for t in TS_RE.finditer(m.group(0)):
                s = _ts_to_seconds(t)
                last = s if last is None else max(last, s)
            continue
        h = HEADER_RE.match(line)
        if h:
            headers[h.group(1).lower()] = h.group(2).strip()
    return LrcInfo(synced=timed >= 2, timed_lines=timed, last_ts=last, headers=headers)


def is_synced(text: str | None) -> bool:
    return bool(text) and analyze(text).synced


def check_against_duration(info: LrcInfo, duration: float | None) -> str | None:
    """Return a rejection reason, or None if the LRC plausibly fits the track."""
    if not info.synced:
        return "no timestamps (plain lyrics)"
    if not duration:
        return None
    if info.last_ts is not None and info.last_ts > duration + END_TOLERANCE:
        return f"last timestamp {info.last_ts:.0f}s is past track end {duration:.0f}s"
    length = info.length_header
    if length is not None and abs(length - duration) > END_TOLERANCE:
        return f"[length:] {length:.0f}s differs from track length {duration:.0f}s"
    return None


def same_lyrics(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return clean_text(a) == clean_text(b)


def display_lines(text: str | None) -> list[tuple[float | None, str]]:
    """Lines for display: (time, text) sorted by time for synced lyrics,
    (None, text) for plain lyrics. Header tags are dropped; word-level
    <mm:ss.xx> marks are removed."""
    if not text:
        return []
    out: list[tuple[float | None, str]] = []
    synced = is_synced(text)
    for line in text.splitlines():
        m = LINE_TS_RE.match(line)
        if m:
            body = re.sub(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>", "", line[m.end():]).strip()
            for t in TS_RE.finditer(m.group(0)):
                out.append((_ts_to_seconds(t), body))
        elif HEADER_RE.match(line) and synced:
            continue
        elif not synced:
            out.append((None, line.rstrip()))
    if synced:
        out.sort(key=lambda x: x[0])
    return out


def current_index(lines: list[tuple[float | None, str]], position: float) -> int:
    """Index of the line being sung at `position`, or -1."""
    best = -1
    for i, (t, _) in enumerate(lines):
        if t is None:
            return -1
        if t <= position:
            best = i
        else:
            break
    return best


WORD_TS_RE = re.compile(r"<(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?>")


def format_ts(seconds: float) -> str:
    cs = int(round(max(0.0, seconds) * 100))
    m, rem = divmod(cs, 6000)
    s, c = divmod(rem, 100)
    return f"{m:02d}:{s:02d}.{c:02d}"


def pin_key(t: float) -> str:
    return f"{t:.2f}"


def warp(t: float, anchors: list[tuple[float, float]]) -> float:
    """Piecewise-linear map through (original, new) anchor points; the end segments are
    extended with their own slope, and a single anchor shifts everything by its offset."""
    if not anchors:
        return t
    if len(anchors) == 1:
        o, n = anchors[0]
        return t + (n - o)

    def seg(a, b):
        (o1, n1), (o2, n2) = a, b
        slope = (n2 - n1) / (o2 - o1) if abs(o2 - o1) > 1e-6 else 1.0
        return n1 + (t - o1) * slope
    if t <= anchors[0][0]:
        return seg(anchors[0], anchors[1])
    if t >= anchors[-1][0]:
        return seg(anchors[-2], anchors[-1])
    for a, b in zip(anchors, anchors[1:]):
        if a[0] <= t <= b[0]:
            return seg(a, b)
    return t


def retime(text: str, offset: float = 0.0, factor: float = 1.0, pins: dict | None = None,
           interpolate: bool = False) -> str:
    """Shift and stretch every timestamp: new = old * factor + offset (seconds).
    `pins` maps an original line time (pin_key) to a fixed new time: pinned lines keep that
    time (their word timings move with them). Times are clamped at 0; plain lyrics are unchanged."""
    pins = pins or {}
    if not text or (abs(offset) < 1e-9 and abs(factor - 1.0) < 1e-9 and not pins):
        return text
    anchors = sorted((float(k), v) for k, v in pins.items())
    use_warp = interpolate and bool(anchors)

    def base(t: float) -> float:
        return warp(t, anchors) if use_warp else t * factor + offset
    out = []
    for line in text.splitlines():
        m = LINE_TS_RE.match(line)
        if m:
            stamps_in = list(TS_RE.finditer(m.group(0)))
            first = _ts_to_seconds(stamps_in[0])
            delta = (pins[pin_key(first)] - base(first)) if pin_key(first) in pins else 0.0

            def fix(t, d=delta):
                orig = _ts_to_seconds(t)
                return pins[pin_key(orig)] if pin_key(orig) in pins else base(orig) + d
            stamps = TS_RE.sub(lambda t: f"[{format_ts(fix(t))}]", m.group(0))
            body = WORD_TS_RE.sub(lambda t: f"<{format_ts(base(_ts_to_seconds(t)) + delta)}>", line[m.end():])
            line = stamps + body
        else:
            h = HEADER_RE.match(line)
            if h and h.group(1).lower() == "length" and parse_length(h.group(2)) is not None \
                    and abs(factor - 1.0) > 1e-9:
                secs = parse_length(h.group(2)) * factor
                line = f"[length: {int(secs // 60):02d}:{secs % 60:05.2f}]"
        out.append(line)
    return "\n".join(out)


def solve_alignment(anchors: list[tuple[float, float]], factor: float = 1.0) -> tuple[float, float]:
    """(offset, factor) mapping original line times onto target times.
    One anchor keeps the given factor and solves the offset; two or more fit a
    line through the first and last anchor."""
    if not anchors:
        return 0.0, factor
    if len(anchors) == 1:
        o, t = anchors[0]
        return t - o * factor, factor
    (o1, t1), (o2, t2) = min(anchors), max(anchors)
    if abs(o2 - o1) < 0.05:
        return t1 - o1 * factor, factor
    f = (t2 - t1) / (o2 - o1)
    return t1 - o1 * f, f


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list = field(default_factory=list)   # [(start, end, word)] from <mm:ss.xx> marks


def segments(text: str | None, duration: float | None = None) -> list[Segment]:
    """Timed lines as spans: each line lasts until the next line starts."""
    if not is_synced(text):
        return []
    raw: list[tuple[float, str]] = []
    for line in text.splitlines():
        m = LINE_TS_RE.match(line)
        if not m:
            continue
        body = line[m.end():]
        for t in TS_RE.finditer(m.group(0)):
            raw.append((_ts_to_seconds(t), body))
    raw.sort(key=lambda x: x[0])
    out: list[Segment] = []
    for i, (start, body) in enumerate(raw):
        if i + 1 < len(raw):
            end = raw[i + 1][0]
        else:
            end = min(duration, start + 5.0) if duration and duration > start else start + 5.0
        stamps = list(WORD_TS_RE.finditer(body))
        clean = WORD_TS_RE.sub("", body).strip()
        words = []
        if stamps:
            for j, w in enumerate(stamps):
                ws = _ts_to_seconds(w)
                we = _ts_to_seconds(stamps[j + 1]) if j + 1 < len(stamps) else end
                word = body[w.end(): stamps[j + 1].start() if j + 1 < len(stamps) else len(body)].strip()
                if word:
                    words.append((ws, max(ws, we), word))
        if clean:
            out.append(Segment(start, max(start, end), clean, words))
    return out


def segment_at(segs: list[Segment], t: float) -> int:
    for i, s in enumerate(segs):
        if s.start <= t < s.end:
            return i
        if s.start > t:
            break
    return -1


# ------------------------------------------------------------------ tidying
_STRAIGHTEN = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "´": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "–": "-", "—": "-", "―": "-", "‒": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    "　": " ",
    "​": None, "‌": None, "‍": None, "⁠": None, "﻿": None,
})


def straighten(text: str) -> str:
    """Typographic ("smart") quotes and punctuation → plain ASCII: ‘’ → ', “” → ", … → ...,
    en/em dashes → -, unusual spaces → normal spaces, invisible characters removed."""
    return text.translate(_STRAIGHTEN).replace("…", "...")


def trim_lines(text: str) -> str:
    """Remove spaces at the start and end of each lyric line (after its timestamps)."""
    out = []
    for line in text.splitlines():
        m = LINE_TS_RE.match(line)
        if m:
            stamps = "".join(t.group(0) for t in TS_RE.finditer(m.group(0)))
            line = stamps + line[m.end():].strip()
        else:
            line = line.strip()
        out.append(line)
    return "\n".join(out).strip("\n")


def tidy(text: str | None, trim: bool = False, quotes: bool = False) -> str | None:
    if not text:
        return text
    if quotes:
        text = straighten(text)
    if trim:
        text = trim_lines(text)
    return text
