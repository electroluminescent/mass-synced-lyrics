"""Persistent settings and default locations."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields

from . import APP_NAME, DEFAULT_ACOUSTID_KEY


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
        json.dump(asdict(s), fh, indent=2)
    os.replace(tmp, path)
