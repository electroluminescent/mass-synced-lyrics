"""Command-line interface."""
from __future__ import annotations

import argparse
import sys
import threading

from . import APP_NAME, __version__
from .config import load_settings
from .engine import STATUS_LABELS, Engine, display_path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=APP_NAME, description="Embed synced (LRC) lyrics into a music library.")
    p.add_argument("folder", help="music folder to process (recursive)")
    p.add_argument("--collection", action="append", default=None, metavar="PATH",
                   help="folder or .lrc file of your own lyrics to use before LRCLIB (repeatable)")
    p.add_argument("--offline", action="store_true", help="no online lookups (sidecar .lrc and collection only)")
    p.add_argument("--output", choices=["embed", "lrc"],
                   help="embed = overwrite the files' lyrics tags (default); lrc = save a .lrc beside each file")
    p.add_argument("--dry-run", action="store_true",
                   help="find lyrics without writing (the app's Apply button can write them later)")
    p.add_argument("--lrc-action", choices=["recycle", "delete", "keep"],
                   help="what to do with a sidecar .lrc after embedding it (default: recycle)")
    p.add_argument("--no-restore-mtime", action="store_true", help="let file modified times change")
    p.add_argument("--threshold", type=int, metavar="0-100", help="name match confidence (default 90)")
    p.add_argument("--trim", action="store_true", help="trim spaces at the start/end of each lyric line")
    p.add_argument("--straighten", action="store_true",
                   help="straighten smart quotes and punctuation (’ → ', “” → \", … → ..., dashes → -)")
    p.add_argument("--no-acoustid", action="store_true", help="never fingerprint untagged files")
    p.add_argument("--acoustid-key", help="AcoustID application API key")
    p.add_argument("--fpcalc", help="path to fpcalc")
    p.add_argument("--retry-misses", action="store_true", help="look up again tracks that were not found before")
    p.add_argument("--rpm", type=int, help="LRCLIB requests per minute (default 80)")
    p.add_argument("--log-dir", help="folder for CSV logs")
    p.add_argument("--state-db", help="SQLite state file")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


_PLAIN = str.maketrans({"…": "...", "—": "-", "–": "-"})


def say(*parts, **kw) -> None:
    """print() with console-safe punctuation (legacy Windows code pages)."""
    print(*(str(p).translate(_PLAIN) for p in parts), **kw)


def _safe_console() -> None:
    """Windows consoles often use a legacy code page; never crash on a song name."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    args = build_parser().parse_args(argv)
    s = load_settings()
    s.music_folder = args.folder
    if args.collection is not None:
        s.collection, s.use_collection = args.collection, True
    s.use_online = not args.offline
    s.dry_run = args.dry_run
    if args.output:
        s.output_mode = args.output
    if args.lrc_action:
        s.lrc_action = args.lrc_action
    if args.no_restore_mtime:
        s.restore_mtime = False
    if args.threshold is not None:
        s.match_threshold = max(50, min(100, args.threshold))
    if args.trim:
        s.tidy_trim = True
    if args.straighten:
        s.tidy_quotes = True
    if args.no_acoustid:
        s.use_acoustid = False
    if args.acoustid_key:
        s.acoustid_key = args.acoustid_key
    if args.fpcalc:
        s.fpcalc_path = args.fpcalc
    s.retry_misses = args.retry_misses
    if args.rpm:
        s.requests_per_minute = args.rpm
    if args.log_dir:
        s.log_dir = args.log_dir
    if args.state_db:
        s.state_db = args.state_db

    cancel = threading.Event()
    def emit(e: dict) -> None:
        t = e["type"]
        if t == "log":
            say(e["text"], flush=True)
        elif t == "result":
            r = e["result"]
            if r.status == "cached":
                return
            label = STATUS_LABELS.get(r.status, r.status)
            extra = f" [{r.source}]" if r.source and r.status == "embedded" else ""
            detail = f" — {r.detail}" if r.detail else ""
            say(f"[{e['done']}/{e['total']}] {label}{extra}: {display_path(r.path)}{detail}", flush=True)
        elif t == "done":
            say("\nSummary:")
            for k, v in sorted(e["counts"].items(), key=lambda kv: -kv[1]):
                say(f"  {STATUS_LABELS.get(k, k)}: {v}")
            say(f"CSV log: {e['csv']}")
            if e["cancelled"]:
                say("Stopped before finishing; run again to resume.")

    engine = Engine(s, emit, cancel)
    try:
        engine.run()
    except KeyboardInterrupt:
        cancel.set()
        say("Stopping…", file=sys.stderr)
        return 130
    except ValueError as e:
        say(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
