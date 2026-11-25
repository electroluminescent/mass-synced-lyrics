import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mass_synced_lyrics.config import Settings  # noqa: E402
from mass_synced_lyrics.tags import TagFile  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
FORMATS = ["flac", "mp3", "ogg", "opus", "wav", "m4a", "aiff", "wma", "wv"]

LRC = "[ti:Some Song]\n[00:01.00]First line\n[00:04.50]Second line\n[00:08.20]Third line"
LRC_B = "[00:00.50]Other first\n[00:05.00]Other second"
PLAIN = "Just words\nno timing"


def make_track(dest: str, fmt: str, title="Some Song", artist="Some Artist", album="Some Album",
               lyrics: str | None = None, id3v23: bool = False) -> str:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy(os.path.join(FIXTURES, f"silence.{fmt}"), dest)
    tf = TagFile(dest)
    t = tf.audio.tags
    if tf.kind == "id3":
        from mutagen.id3 import TALB, TIT2, TPE1
        if title:
            t.add(TIT2(encoding=3, text=title))
        if artist:
            t.add(TPE1(encoding=3, text=artist))
        if album:
            t.add(TALB(encoding=3, text=album))
    elif tf.kind == "vorbis":
        for k, v in (("title", title), ("artist", artist), ("album", album)):
            if v:
                t[k] = [v]
    elif tf.kind == "mp4":
        for k, v in (("\xa9nam", title), ("\xa9ART", artist), ("\xa9alb", album)):
            if v:
                t[k] = [v]
    elif tf.kind == "asf":
        for k, v in (("Title", title), ("Author", artist), ("WM/AlbumTitle", album)):
            if v:
                t[k] = [v]
    elif tf.kind == "ape":
        for k, v in (("Title", title), ("Artist", artist), ("Album", album)):
            if v:
                t[k] = v
    if lyrics:
        tf.set_lyrics(lyrics)
    if tf.kind == "id3" and id3v23:
        tf.audio.save(v2_version=3)
    else:
        tf.save()
    os.utime(dest, (1_600_000_000, 1_600_000_000))
    return dest


def write_lrc(path: str, text: str = LRC, encoding: str = "utf-8") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding=encoding, newline="\r\n") as fh:
        fh.write(text)
    return path


class FakeLrclib:
    """Stands in for LrclibClient; records are dicts like the real API."""

    def __init__(self, records=None):
        self.records = records or []
        self.calls = []

    def get(self, title, artist, album, duration):
        self.calls.append(("get", title, artist, album, duration))
        for r in self.records:
            if r["trackName"].lower() == title.lower() and r["artistName"].lower() == artist.lower():
                if album and r.get("albumName", "").lower() != album.lower():
                    continue
                if duration and abs(r["duration"] - duration) > 2:
                    continue
                return r
        return None

    def search(self, title, artist):
        self.calls.append(("search", title, artist))
        return [r for r in self.records if title.lower().split()[0] in r["trackName"].lower()]


def record(title="Some Song", artist="Some Artist", album="Some Album", duration=10.0,
           synced=LRC_B, plain="x", instrumental=False, rid=1):
    return {"id": rid, "trackName": title, "artistName": artist, "albumName": album,
            "duration": duration, "instrumental": instrumental,
            "syncedLyrics": None if instrumental else synced,
            "plainLyrics": None if instrumental else plain}


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.music_folder = str(tmp_path / "music")
    s.log_dir = str(tmp_path / "logs")
    s.state_db = str(tmp_path / "state.sqlite3")
    s.lrc_action = "delete"
    s.use_acoustid = False
    os.makedirs(s.music_folder, exist_ok=True)
    return s


def new_app(settings=None):
    """gui.App(), retrying Tk start-up: on Windows, creating many Tk interpreters in one test
    process occasionally fails to read Tk's own library files (not an app problem)."""
    import time
    import tkinter

    from mass_synced_lyrics import gui
    for attempt in range(4):
        try:
            return gui.App(settings)
        except tkinter.TclError:
            if attempt == 3:
                raise
            time.sleep(0.5)
