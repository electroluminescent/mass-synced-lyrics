"""The review session (songs in the list, their columns, what was selected) kept between runs."""
from __future__ import annotations

import json
import os
import time

from .config import app_dir

VERSION = 1


def session_path() -> str:
    return os.path.join(app_dir(), "session.json")


def save_session(rows: dict, selected: str = "", filter_name: str = "", search: str = "",
                 music_folder: str = "", path: str | None = None) -> None:
    path = path or session_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"version": VERSION, "saved": time.time(), "music_folder": music_folder, "selected": selected,
            "filter": filter_name, "search": search, "rows": rows}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def load_session(path: str | None = None) -> dict | None:
    path = path or session_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != VERSION or not data.get("rows"):
        return None
    return data


def clear_session(path: str | None = None) -> None:
    try:
        os.remove(path or session_path())
    except OSError:
        pass
