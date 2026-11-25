"""Read identity tags and read/write embedded lyrics across formats (via mutagen)."""
from __future__ import annotations

from dataclasses import dataclass, field

import mutagen
from mutagen.apev2 import APEv2, APEv2File
from mutagen.asf import ASF, ASFTags
from mutagen.id3 import ID3, USLT
from mutagen.mp4 import MP4, MP4Tags

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".ogg", ".oga", ".opus", ".spx", ".m4a", ".wav", ".aif", ".aiff",
    ".aifc", ".wma", ".ape", ".wv", ".mpc", ".mp+", ".ofr", ".ofs", ".tak", ".tta", ".dsf", ".dff",
}


class UnsupportedFile(Exception):
    pass


@dataclass
class Meta:
    title: str = ""
    artist: str = ""
    album: str = ""
    mbid: str = ""          # MusicBrainz recording ID
    acoustid: str = ""
    lyrics: list[str] = field(default_factory=list)

    @property
    def identified(self) -> bool:
        return bool(self.title and self.artist)


def _first(values, joiner: str = ", ") -> str:
    if values is None:
        return ""
    if isinstance(values, (list, tuple)):
        parts = [str(v).strip() for v in values if str(v).strip()]
        return joiner.join(parts)
    return str(values).strip()


class TagFile:
    """Thin wrapper around a mutagen file exposing a uniform lyrics API."""

    def __init__(self, path: str):
        self.path = path
        try:
            audio = mutagen.File(path)
        except Exception as e:  # mutagen raises many types for corrupt files
            raise UnsupportedFile(f"unreadable: {e}") from e
        if audio is None:
            raise UnsupportedFile("not a recognised audio format")
        self.audio = audio
        self.kind = self._detect_kind()
        self.duration = float(getattr(audio.info, "length", 0.0) or 0.0)

    # ---- format detection -------------------------------------------------
    def _detect_kind(self) -> str:
        a, t = self.audio, self.audio.tags
        if isinstance(t, ID3):
            return "id3"
        if isinstance(t, MP4Tags) or isinstance(a, MP4):
            return "mp4"
        if isinstance(t, ASFTags) or isinstance(a, ASF):
            return "asf"
        if isinstance(t, APEv2) or isinstance(a, APEv2File):
            return "ape"
        if t is not None and hasattr(t, "vendor"):  # VComment (FLAC/Ogg)
            return "vorbis"
        if t is None:
            # Decide by the tag type the container would create.
            try:
                a.add_tags()
            except Exception as e:
                raise UnsupportedFile(f"format cannot hold tags: {type(a).__name__}") from e
            return self._detect_kind()
        raise UnsupportedFile(f"unsupported tag type {type(t).__name__}")

    # ---- reading ----------------------------------------------------------
    def read(self) -> Meta:
        t = self.audio.tags
        m = Meta()
        if self.kind == "id3":
            m.title = _first(t["TIT2"].text) if "TIT2" in t else ""
            m.artist = _first(t["TPE1"].text) if "TPE1" in t else ""
            m.album = _first(t["TALB"].text) if "TALB" in t else ""
            for f in t.getall("UFID"):
                if f.owner == "http://musicbrainz.org":
                    m.mbid = f.data.decode("ascii", "ignore")
            for f in t.getall("TXXX"):
                if f.desc.lower() == "acoustid id":
                    m.acoustid = _first(f.text)
            m.lyrics = [f.text for f in t.getall("USLT") if f.text]
        elif self.kind == "vorbis":
            m.title = _first(t.get("title"))
            m.artist = _first(t.get("artist"))
            m.album = _first(t.get("album"))
            m.mbid = _first(t.get("musicbrainz_trackid"))
            m.acoustid = _first(t.get("acoustid_id"))
            for k in ("lyrics", "unsyncedlyrics", "syncedlyrics"):
                m.lyrics += [v for v in (t.get(k) or []) if v]
        elif self.kind == "mp4":
            m.title = _first(t.get("\xa9nam"))
            m.artist = _first(t.get("\xa9ART"))
            m.album = _first(t.get("\xa9alb"))
            m.mbid = _ff(t.get("----:com.apple.iTunes:MusicBrainz Track Id"))
            m.acoustid = _ff(t.get("----:com.apple.iTunes:Acoustid Id"))
            m.lyrics = [v for v in (t.get("\xa9lyr") or []) if v]
        elif self.kind == "asf":
            m.title = _first(t.get("Title"))
            m.artist = _first(t.get("Author"))
            m.album = _first(t.get("WM/AlbumTitle"))
            m.mbid = _first(t.get("MusicBrainz/Track Id"))
            m.acoustid = _first(t.get("Acoustid/Id"))
            m.lyrics = [str(v) for v in (t.get("WM/Lyrics") or []) if str(v)]
        elif self.kind == "ape":
            def g(k):
                v = t.get(k)
                return str(v).replace("\x00", ", ").strip() if v is not None else ""
            m.title, m.artist, m.album = g("Title"), g("Artist"), g("Album")
            m.mbid, m.acoustid = g("MUSICBRAINZ_TRACKID"), g("ACOUSTID_ID")
            ly = g("Lyrics")
            m.lyrics = [ly] if ly else []
        return m

    # ---- writing ----------------------------------------------------------
    def set_lyrics(self, text: str) -> None:
        t = self.audio.tags
        if self.kind == "id3":
            old = t.getall("USLT")
            lang = old[0].lang if old else "XXX"
            v24 = t.version >= (2, 4, 0)
            t.delall("USLT")
            t.add(USLT(encoding=3 if v24 else 1, lang=lang, desc="", text=text))
        elif self.kind == "vorbis":
            t["LYRICS"] = [text]
        elif self.kind == "mp4":
            t["\xa9lyr"] = [text]
        elif self.kind == "asf":
            t["WM/Lyrics"] = [text]
        elif self.kind == "ape":
            t["Lyrics"] = text

    def remove_lyrics(self) -> None:
        t = self.audio.tags
        if self.kind == "id3":
            t.delall("USLT")
        elif self.kind == "vorbis":
            for k in ("lyrics", "unsyncedlyrics", "syncedlyrics"):
                if k in t:
                    del t[k]
        else:
            key = {"mp4": "\xa9lyr", "asf": "WM/Lyrics", "ape": "Lyrics"}[self.kind]
            if key in t:
                del t[key]

    def save(self) -> None:
        if self.kind == "id3":
            v = 3 if self.audio.tags.version < (2, 4, 0) and self.audio.tags.version >= (2, 3, 0) else 4
            try:
                self.audio.save(v2_version=v)
            except TypeError:
                self.audio.save()
        else:
            self.audio.save()


def _ff(values) -> str:
    if not values:
        return ""
    v = values[0]
    return bytes(v).decode("utf-8", "ignore").strip() if isinstance(v, (bytes, bytearray)) else str(v)
