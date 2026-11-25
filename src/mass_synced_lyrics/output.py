"""Where lyrics go: embedded in the audio file ("embed") or a .lrc file beside it ("lrc")."""
from __future__ import annotations

import os

from . import lrc as lrcmod
from .paths import display_path
from .tags import TagFile

EMBED, LRC = "embed", "lrc"
MODES = (EMBED, LRC)


class VerifyError(Exception):
    pass


def lrc_path_for(audio_path: str) -> str:
    return os.path.splitext(audio_path)[0] + ".lrc"


def same_file(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(display_path(a))) == os.path.normcase(os.path.abspath(display_path(b)))


def read_lrc_file(audio_path: str) -> str | None:
    p = lrc_path_for(audio_path)
    if not os.path.isfile(p):
        return None
    with open(p, "rb") as fh:
        return lrcmod.clean_text(lrcmod.decode_bytes(fh.read()))


def current_texts(tf: TagFile, mode: str, embedded: list[str] | None = None) -> list[str]:
    """The lyrics that the chosen output currently holds for this file."""
    if mode == LRC:
        t = read_lrc_file(tf.path)
        return [t] if t else []
    return list(embedded if embedded is not None else tf.read().lyrics)


def write_lyrics(tf: TagFile, text: str | None, restore_mtime: bool, st: os.stat_result | None = None) -> None:
    """Embed (or with text=None/"" remove) lyrics, restore mtime, verify by re-reading."""
    path = tf.path
    st = st or os.stat(path)
    if text:
        tf.set_lyrics(text)
    else:
        tf.remove_lyrics()
    tf.save()
    if restore_mtime:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = TagFile(path).read().lyrics
    ok = any(lrcmod.same_lyrics(t, text) for t in after) if text else not after
    if not ok:
        raise VerifyError("verification failed: lyrics not as expected after writing")


def write_lrc_file(audio_path: str, text: str | None, lrc_action: str = "recycle") -> str:
    """Write <audio stem>.lrc (UTF-8). text=None/"" removes it (per lrc_action). Returns the path."""
    p = lrc_path_for(audio_path)
    if not text:
        if os.path.exists(p):
            dispose(p, "delete" if lrc_action == "delete" else "recycle")
        return p
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(lrcmod.clean_text(text) + "\n")
    os.replace(tmp, p)
    if not lrcmod.same_lyrics(read_lrc_file(audio_path), text):
        raise VerifyError("verification failed: .lrc file content differs after writing")
    return p


def write_output(tf: TagFile, text: str | None, mode: str, restore_mtime: bool, lrc_action: str,
                 st: os.stat_result | None = None) -> None:
    if mode == LRC:
        write_lrc_file(tf.path, text, lrc_action)
    else:
        write_lyrics(tf, text, restore_mtime, st)


def dispose(lrc_path: str, action: str) -> str:
    """Remove a used sidecar .lrc per the setting. Returns a short note for the log."""
    if action == "keep" or not lrc_path or not os.path.exists(lrc_path):
        return ""
    try:
        if action == "recycle":
            from send2trash import send2trash

            send2trash(display_path(lrc_path))
            return ".lrc moved to Recycle Bin"
        os.remove(lrc_path)
        return ".lrc deleted"
    except Exception as e:
        return f".lrc kept (could not remove: {e})"
