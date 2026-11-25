"""Path helpers shared by the engine, state and UI."""
from __future__ import annotations

import os


def to_long_path(p: str) -> str:
    """Windows: allow paths longer than 260 characters."""
    if os.name != "nt":
        return p
    p = os.path.abspath(p)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def display_path(p: str) -> str:
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def state_key(p: str) -> str:
    """One spelling per file for the state database (no \\\\?\\ prefix, absolute)."""
    return os.path.abspath(display_path(p)) if p else p


def _launch(args, shell_string: str | None = None) -> None:
    import subprocess
    if shell_string is not None:
        subprocess.Popen(shell_string)                      # Windows: explorer needs its own quoting
    else:
        subprocess.Popen(args)


def reveal_in_explorer(path: str) -> None:
    """Show a file in the system file manager, selected where the platform allows it."""
    import sys
    p = display_path(path)
    if os.name == "nt":
        _launch(None, f'explorer /select,"{os.path.normpath(p)}"')
    elif sys.platform == "darwin":
        _launch(["open", "-R", p])
    else:
        _launch(["xdg-open", os.path.dirname(p) or "."])


def open_folder(folder: str) -> None:
    """Open a folder in the system file manager."""
    import sys
    p = display_path(folder)
    if os.name == "nt":
        os.startfile(os.path.normpath(p))                   # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        _launch(["open", p])
    else:
        _launch(["xdg-open", p])
