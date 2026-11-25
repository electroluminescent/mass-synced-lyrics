"""Name normalisation and fuzzy scoring for titles, artists and file names."""
from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

FEAT_PAREN_RE = re.compile(r"[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s[^\)\]]*[\)\]]", re.I)
FEAT_TAIL_RE = re.compile(r"\s(?:feat\.?|ft\.?|featuring)\s.*$", re.I)
BRACKET_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
DASH_SUFFIX_RE = re.compile(
    r"\s-\s(?:\d{4}\s)?(?:remaster(?:ed)?|live|mono|stereo|single|radio|album|edit|version|mix|demo)\b.*$",
    re.I,
)
TRACKNO_RE = re.compile(r"^(?:\d{1,2}\s)?\d{1,3}\s")
ARTIST_SPLIT_RE = re.compile(r"\s*(?:;|,|&|/|\sx\s|\sand\s|\sfeat\.?\s|\sft\.?\s|\sfeaturing\s|\swith\s)\s*", re.I)


def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def norm(s: str | None, loose: bool = False) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = FEAT_PAREN_RE.sub(" ", s)
    s = FEAT_TAIL_RE.sub("", s)
    if loose:
        s = BRACKET_RE.sub(" ", s)
        s = DASH_SUFFIX_RE.sub("", s)
    s = _strip_accents(s).casefold().replace("&", " and ")
    s = re.sub(r"[^\w]+", " ", s, flags=re.UNICODE).replace("_", " ")
    return re.sub(r"\s+", " ", s).strip()


def strip_trackno(n: str) -> str:
    return TRACKNO_RE.sub("", n, count=1).strip()


def primary_artist(artist: str | None) -> str:
    if not artist:
        return ""
    return ARTIST_SPLIT_RE.split(artist.strip(), maxsplit=1)[0].strip()


def name_variants(name: str | None, artist: str | None = None) -> set[str]:
    """Normalised forms of a file stem / title used for matching."""
    out: set[str] = set()
    if not name:
        return out
    cands = [name]
    if " - " in name:
        cands += [name.split(" - ", 1)[1], name.rsplit(" - ", 1)[1]]
    artists = {norm(artist), norm(primary_artist(artist))} - {""}
    for c in cands:
        for loose in (False, True):
            n = norm(c, loose)
            if not n:
                continue
            out.add(n)
            s = strip_trackno(n)
            if s:
                out.add(s)
            for a in artists:
                for base in (n, s):
                    if base.startswith(a + " ") and len(base) > len(a) + 1:
                        out.add(base[len(a) + 1:])
    return out


def best_ratio(a: set[str], b: set[str]) -> float:
    best = 0.0
    for x in a:
        for y in b:
            r = fuzz.ratio(x, y)
            if r > best:
                best = r
                if best >= 100:
                    return best
    return best


def similarity(a: str | None, b: str | None) -> float:
    """Loose similarity for comparing tag values with LRCLIB records."""
    na, nb = norm(a, True), norm(b, True)
    if not na or not nb:
        return 0.0
    return max(fuzz.ratio(na, nb), fuzz.token_set_ratio(na, nb))
