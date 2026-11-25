"""Online services: LRCLIB (lyrics), AcoustID + MusicBrainz (identification)."""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests

from . import lrc as lrcmod
from .names import norm, primary_artist, similarity
from .tags import Meta

DURATION_TOLERANCE = 2.0


class Cancelled(Exception):
    pass


class ServiceError(Exception):
    pass


class RateLimiter:
    def __init__(self, per_minute: float):
        self.interval = 60.0 / per_minute if per_minute > 0 else 0.0
        self._next = 0.0
        self._lock = threading.Lock()

    def defer(self, seconds: float) -> None:
        """Push the next allowed request back (a 429 pauses every thread, not just one)."""
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)

    def wait(self, sleep: Callable[[float], None]) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            sleep(delay)


class HttpClient:
    def __init__(self, user_agent: str, per_minute: float, cancel: threading.Event | None = None,
                 session: requests.Session | None = None, max_attempts: int = 6):
        self._fixed_session = session
        if session is not None:
            session.headers["User-Agent"] = user_agent
        self._ua = user_agent
        self._local = threading.local()       # one requests.Session per worker thread
        self.limiter = RateLimiter(per_minute)
        self.cancel = cancel or threading.Event()
        self.max_attempts = max_attempts
        self.requests_made = 0

    @property
    def session(self):
        if self._fixed_session is not None:
            return self._fixed_session
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = self._ua
            self._local.session = s
        return s

    def sleep(self, seconds: float) -> None:
        if self.cancel.wait(seconds):
            raise Cancelled()

    def request(self, method: str, url: str, **kw) -> requests.Response | None:
        """Returns the response for 2xx, None for 404; retries 429/5xx/network errors."""
        last = ""
        for attempt in range(self.max_attempts):
            if self.cancel.is_set():
                raise Cancelled()
            self.limiter.wait(self.sleep)
            try:
                self.requests_made += 1
                r = self.session.request(method, url, timeout=30, **kw)
            except requests.RequestException as e:
                last = f"network error: {e.__class__.__name__}"
                self.sleep(min(60, 2 ** attempt) + random.uniform(0, 1))
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                ra = r.headers.get("Retry-After", "")
                try:
                    wait = float(ra)
                except ValueError:
                    wait = 30.0
                wait += random.uniform(0.5, 3.0)
                self.limiter.defer(wait)
                self.sleep(wait)
                last = "rate limited"
                continue
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                self.sleep(min(60, 2 ** attempt) + random.uniform(0, 1))
                continue
            if r.status_code >= 400:
                raise ServiceError(f"HTTP {r.status_code}: {r.text[:200]}")
            return r
        raise ServiceError(f"gave up after {self.max_attempts} attempts ({last})")


# ---------------------------------------------------------------- LRCLIB ---

@dataclass
class LyricsResult:
    status: str  # found | instrumental | no_synced | not_found
    synced: str = ""
    record_id: int | None = None
    how: str = ""
    duration: float = 0.0      # length of the recording the lyrics were timed for


class LrclibClient:
    BASE = "https://lrclib.net/api"

    def __init__(self, http: HttpClient):
        self.http = http

    def get(self, title: str, artist: str, album: str | None, duration: float | None) -> dict | None:
        params = {"track_name": title, "artist_name": artist}
        if album:
            params["album_name"] = album
        if duration and 1 <= duration <= 3600:
            params["duration"] = int(round(duration))
        r = self.http.request("GET", f"{self.BASE}/get", params=params)
        return r.json() if r is not None else None

    def search(self, title: str, artist: str) -> list[dict]:
        r = self.http.request("GET", f"{self.BASE}/search",
                              params={"track_name": title, "artist_name": artist})
        if r is None:
            return []
        data = r.json()
        return data if isinstance(data, list) else []


def find_synced(client: LrclibClient, meta: Meta, duration: float | None,
                name_threshold: float = 85.0) -> LyricsResult:
    title, artist, album = meta.title, meta.artist, meta.album
    loose_title = _loose_title(title)
    seen_instrumental = seen_plain = False

    def judge(rec: dict | None, how: str) -> LyricsResult | None:
        nonlocal seen_instrumental, seen_plain
        if not rec:
            return None
        if rec.get("instrumental"):
            seen_instrumental = True
            return None
        synced = rec.get("syncedLyrics") or ""
        if synced and lrcmod.is_synced(synced):
            reason = lrcmod.check_against_duration(lrcmod.analyze(synced), duration)
            if reason is None:
                return LyricsResult("found", lrcmod.clean_text(synced), rec.get("id"), how,
                                    float(rec.get("duration") or 0))
        elif rec.get("plainLyrics"):
            seen_plain = True
        return None

    attempts = [(title, artist, album, "get")]
    attempts.append((title, artist, None, "get (no album)"))
    pa = primary_artist(artist)
    if pa and norm(pa) != norm(artist):
        attempts.append((title, pa, None, "get (primary artist)"))
    if loose_title and norm(loose_title) != norm(title):
        attempts.append((loose_title, pa or artist, None, "get (simplified title)"))
    done = set()
    for t, a, al, how in attempts:
        k = (norm(t), norm(a), norm(al))
        if k in done:
            continue
        done.add(k)
        res = judge(client.get(t, a, al, duration), how)
        if res:
            return res
        if seen_instrumental:
            return LyricsResult("instrumental", how=how)

    # Search fallback with scoring.
    for t, a in {(title, artist), (loose_title or title, pa or artist)}:
        best: tuple[float, dict] | None = None
        for rec in client.search(t, a):
            d = rec.get("duration")
            if duration and d and abs(float(d) - duration) > DURATION_TOLERANCE:
                continue
            ts = similarity(title, rec.get("trackName") or rec.get("name"))
            ars = max(similarity(artist, rec.get("artistName")),
                      similarity(pa, primary_artist(rec.get("artistName"))))
            if ts < name_threshold or ars < name_threshold:
                continue
            if rec.get("instrumental"):
                seen_instrumental = True
                continue
            if not rec.get("syncedLyrics"):
                if rec.get("plainLyrics"):
                    seen_plain = True
                continue
            score = ts + ars + (5 if album and similarity(album, rec.get("albumName")) >= 90 else 0)
            if best is None or score > best[0]:
                best = (score, rec)
        if best:
            res = judge(best[1], "search")
            if res:
                return res
    if seen_instrumental:
        return LyricsResult("instrumental")
    if seen_plain:
        return LyricsResult("no_synced")
    return LyricsResult("not_found")


def _loose_title(title: str) -> str:
    import re
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title or "")
    t = re.sub(r"\s-\s.*(remaster|live|version|edit|mix|mono|stereo).*$", "", t, flags=re.I)
    return t.strip()


# ------------------------------------------------- MusicBrainz / AcoustID ---

class MusicBrainzClient:
    BASE = "https://musicbrainz.org/ws/2"

    def __init__(self, http: HttpClient):
        self.http = http

    def recording(self, mbid: str) -> Meta | None:
        r = self.http.request("GET", f"{self.BASE}/recording/{mbid}",
                              params={"inc": "artist-credits+releases", "fmt": "json"})
        if r is None:
            return None
        d = r.json()
        artist = "".join(c.get("name", "") + c.get("joinphrase", "") for c in d.get("artist-credit", []))
        album = (d.get("releases") or [{}])[0].get("title", "")
        return Meta(title=d.get("title", ""), artist=artist.strip(), album=album, mbid=mbid)


def find_fpcalc(override: str = "") -> str:
    cands = []
    if override:
        cands.append(override)
    exe = "fpcalc.exe" if os.name == "nt" else "fpcalc"
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(os.path.join(base, exe))
    cands.append(os.path.join(os.path.dirname(sys.executable), exe))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return shutil.which("fpcalc") or ""


class AcoustIdClient:
    URL = "https://api.acoustid.org/v2/lookup"

    def __init__(self, http: HttpClient, api_key: str, fpcalc: str):
        self.http, self.api_key, self.fpcalc = http, api_key, fpcalc

    def fingerprint(self, path: str) -> tuple[float, str]:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.run([self.fpcalc, "-json", path], capture_output=True, timeout=180,
                           creationflags=flags)
        if p.returncode != 0:
            raise ServiceError(f"fpcalc failed: {p.stderr.decode(errors='replace')[:200].strip()}")
        d = json.loads(p.stdout)
        return float(d["duration"]), d["fingerprint"]

    def identify(self, path: str) -> Meta | None:
        duration, fp = self.fingerprint(path)
        r = self.http.request("POST", self.URL, data={
            "client": self.api_key, "duration": int(duration), "fingerprint": fp,
            "meta": "recordings releasegroups", "format": "json"})
        if r is None:
            return None
        d = r.json()
        if d.get("status") != "ok":
            raise ServiceError(f"AcoustID: {d.get('error', {}).get('message', 'error')}")
        for res in sorted(d.get("results", []), key=lambda x: -x.get("score", 0)):
            if res.get("score", 0) < 0.8:
                break
            for rec in res.get("recordings", []) or []:
                if rec.get("title") and rec.get("artists"):
                    artist = "".join(a.get("name", "") + a.get("joinphrase", "") for a in rec["artists"])
                    album = ((rec.get("releasegroups") or [{}])[0]).get("title", "")
                    return Meta(title=rec["title"], artist=artist.strip(), album=album,
                                mbid=rec.get("id", ""), acoustid=res.get("id", ""))
        return None
