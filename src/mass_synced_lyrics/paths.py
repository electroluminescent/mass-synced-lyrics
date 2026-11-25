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
