"""Persistent settings and default locations."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields

from . import APP_NAME, DEFAULT_ACOUSTID_KEY
from .jsonutil import plain as _plain


def app_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


@dataclass
class Settings:
    music_folder: str = ""
    collection: list[str] = field(default_factory=list)   # personal .lrc folders/files
    use_collection: bool = True
    use_online: bool = True
    skip_synced_online: bool = True      # no LRCLIB / AcoustID requests for files with synced lyrics embedded
    dry_run: bool = False                # command line only; the app always finds first, then applies
    output_mode: str = "embed"           # embed (overwrite files) | lrc (save .lrc beside each file)
    lrc_action: str = "recycle"          # recycle | delete | keep (local sidecar .lrc after embedding)
    restore_mtime: bool = True
    match_threshold: int = 90
    use_acoustid: bool = True
    acoustid_key: str = ""
    fpcalc_path: str = ""
    retry_misses: bool = False
    requests_per_minute: int = 80
    workers: int = 6                     # songs processed at the same time while finding lyrics
    confident_threshold: int = 95        # "Apply confident" writes proposals at or above this (0-100)
    only_found: bool = True              # song list: show only songs with lyrics found
    hide_saved: bool = False             # song list: hide songs already saved / up to date
    confident_fit: int = 80              # ... and whose lyrics fit the recording at least this well (0-100)
    fit_audio: bool = False              # also decode the audio while finding to check the fit (slower)
    hide_low_confidence: bool = False    # song list: hide songs below either level
    list_min_match: int = 0              # song list: Match column filter (hide known scores below this)
    list_min_fit: int = 0                # song list: Fit column filter
    ai_align: bool = False               # align low-confidence lyrics on an NVIDIA GPU (optional add-on)
    ai_align_min: int = 70               # ... keep the re-timed lyrics only when this % of words is heard
    ai_align_model: str = "medium"       # Whisper model for the alignment
    ignore_cache: bool = False           # look everything up / align again instead of reusing earlier results
    ai_keep_loaded: bool = True          # keep the GPU worker and its models loaded between songs and runs
    ripple: bool = False
    show_current: bool = False           # player: show the Current lyrics pane (collapsed by default)                 # player: an edited / aligned time moves the lines around it too
    list_view: str = "artist"
    list_columns: list = field(default_factory=list)   # song list: visible columns ([] = all)            # song list: "artist" (Artist ▸ Album) or "folder" (folders on disk)
    checkpoint_seconds: float = 1.0      # review player: play ± this around a checkpoint
    loop_checkpoints: bool = False
    playback_volume: int = 80            # review player volume, 0–100
    review_split: int = 0                # review tab: width of the song list (0 = default)
    session_restore: str = "ask"
    session_include_saved: bool = False  # reloading a session also lists songs already saved
    tidy_trim: bool = False              # trim spaces at the start/end of each lyric line
    tidy_quotes: bool = False            # straighten smart quotes & punctuation

    def tidy(self, text):
        from .lrc import tidy
        return tidy(text, self.tidy_trim, self.tidy_quotes)
    theme: str = "light"                 # light | dark         # on start: ask | load | fresh (last review session)
    log_dir: str = ""
    state_db: str = ""

    def resolved_log_dir(self) -> str:
        return self.log_dir or os.path.join(app_dir(), "logs")

    def resolved_state_db(self) -> str:
        return self.state_db or os.path.join(app_dir(), "state.sqlite3")

    def resolved_acoustid_key(self) -> str:
        return (self.acoustid_key or DEFAULT_ACOUSTID_KEY).strip()


def settings_path() -> str:
    return os.path.join(app_dir(), "settings.json")


def snap5(value, lo: int = 0, hi: int = 100) -> int:
    """Round a percentage to the nearest 5 and keep it within lo..hi."""
    try:
        v = int(round(float(value) / 5.0) * 5)
    except (TypeError, ValueError):
        v = lo
    return max(lo, min(hi, v))


def load_settings(path: str | None = None) -> Settings:
    path = path or settings_path()
    s = Settings()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        names = {f.name for f in fields(Settings)}
        for k, v in data.items():
            if k in names:
                setattr(s, k, v)
    except (OSError, ValueError):
        pass
    return s


def save_settings(s: Settings, path: str | None = None) -> None:
    path = path or settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(asdict(s), fh, indent=2, default=_plain)
    os.replace(tmp, path)
