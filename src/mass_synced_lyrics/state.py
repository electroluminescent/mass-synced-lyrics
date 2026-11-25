"""SQLite state: per-file outcomes (for resuming), LRCLIB lookup cache,
backups of replaced lyrics, dry-run proposals and per-file user choices."""
from __future__ import annotations

import json
import os
import sqlite3
import time

from .paths import state_key as _k

FINAL = {"embedded", "already_embedded", "has_synced", "instrumental", "no_synced", "not_found",
         "no_metadata", "unsupported", "locked", "lrc_written", "unchanged"}
MISSES = {"no_synced", "not_found", "no_metadata"}

# Per-file choices (take precedence over saved settings and the current run's options)
KEEP = "keep"      # never change this file's lyrics
PIN = "pin"        # this file must carry exactly the pinned text ("" = no lyrics)


def _f(v) -> float | None:
    return None if v is None else float(v)


class State:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # Autocommit: every write is its own short transaction, so several worker threads (each with
        # its own connection) never hold a lock while doing anything slow. WAL lets readers run alongside.
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER,
                status TEXT, detail TEXT, updated REAL,
                source TEXT DEFAULT '', artist TEXT DEFAULT '', title TEXT DEFAULT '',
                local TEXT DEFAULT '', online TEXT DEFAULT '', album TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS candidates (
                path TEXT, kind TEXT, label TEXT, text TEXT, lrc_path TEXT DEFAULT '', updated REAL,
                length REAL DEFAULT 0, PRIMARY KEY (path, kind));
            CREATE TABLE IF NOT EXISTS lookups (
                key TEXT PRIMARY KEY, status TEXT, payload TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS backups (
                path TEXT PRIMARY KEY, text TEXT, saved REAL);
            CREATE TABLE IF NOT EXISTS lrc_backups (
                path TEXT PRIMARY KEY, existed INTEGER, text TEXT, saved REAL);
            CREATE TABLE IF NOT EXISTS pending (
                path TEXT PRIMARY KEY, source TEXT, label TEXT, text TEXT, updated REAL,
                lrc_path TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS overrides (
                path TEXT PRIMARY KEY, kind TEXT, text TEXT, label TEXT, updated REAL,
                lrc_path TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS aligned (
                path TEXT PRIMARY KEY, src_hash TEXT, ok INTEGER, verify REAL, reason TEXT, text TEXT,
                confidence REAL, fit REAL, kind TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS song_prefs (
                path TEXT PRIMARY KEY, prefs TEXT, updated REAL);
            """
        )
        for table, col in (("pending", "lrc_path"), ("overrides", "lrc_path"),
                           ("files", "local"), ("files", "online"), ("files", "album"),
                           ("candidates", "length")):  # databases from earlier builds
            try:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
        for col in ("orig", "pins", "heard", "mode", "ends", "confs"):   # aligned: detail for the player
            try:
                self.db.execute(f"ALTER TABLE aligned ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
        for table in ("pending", "candidates"):         # 0-100 scores; NULL = unknown
            for col in ("confidence", "fit"):
                try:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")
                except sqlite3.OperationalError:
                    pass
        self._pending = 0

    # -- files
    def get_file(self, path: str):
        path = _k(path)
        return self.db.execute("SELECT size, mtime_ns, status, detail, source FROM files WHERE path=?",
                               (path,)).fetchone()

    def set_file(self, path: str, size: int, mtime_ns: int, status: str, detail: str = "",
                 source: str = "", artist: str = "", title: str = "", local: str = "", online: str = "",
                 album: str = "") -> None:
        path = _k(path)
        self.db.execute("REPLACE INTO files (path, size, mtime_ns, status, detail, updated, source, artist, "
                        "title, local, online, album) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (path, size, mtime_ns, status, detail, time.time(), source, artist, title,
                         local or "", online or "", album or ""))
        self._tick()

    def get_file_info(self, path: str) -> dict:
        row = self.db.execute("SELECT status, source, artist, title, album, local, online FROM files WHERE path=?",
                              (_k(path),)).fetchone()
        keys = ("status", "source", "artist", "title", "album", "local", "online")
        return dict(zip(keys, [v or "" for v in row])) if row else {}

    def iter_files(self, statuses: set[str] | None = None):
        q = "SELECT path, status, source, artist, title, detail, local, online, album FROM files"
        rows = self.db.execute(q + " ORDER BY path").fetchall()
        return [r for r in rows if statuses is None or r[1] in statuses]

    # -- lookups
    def get_lookup(self, key: str):
        row = self.db.execute("SELECT status, payload FROM lookups WHERE key=?", (key,)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def set_lookup(self, key: str, status: str, payload: dict) -> None:
        self.db.execute("REPLACE INTO lookups VALUES (?,?,?,?)",
                        (key, status, json.dumps(payload), time.time()))
        self._tick()

    # -- backups of lyrics that were replaced (first original only)
    def save_backup(self, path: str, text: str) -> None:
        path = _k(path)
        self.db.execute("INSERT OR IGNORE INTO backups VALUES (?,?,?)", (path, text, time.time()))
        self._tick()

    def save_lrc_backup(self, path: str, previous: str | None) -> None:
        """Remember what <song>.lrc held before the first write (None = there was no .lrc)."""
        self.db.execute("INSERT OR IGNORE INTO lrc_backups VALUES (?,?,?,?)",
                        (_k(path), 0 if previous is None else 1, previous or "", time.time()))
        self._tick()

    def get_lrc_backup(self, path: str):
        """(existed, text) or None if nothing was recorded."""
        row = self.db.execute("SELECT existed, text FROM lrc_backups WHERE path=?", (_k(path),)).fetchone()
        return (bool(row[0]), row[1]) if row else None

    def clear_backups(self, path: str) -> None:
        self.db.execute("DELETE FROM backups WHERE path=?", (_k(path),))
        self.db.execute("DELETE FROM lrc_backups WHERE path=?", (_k(path),))
        self._tick()

    def record_original(self, path: str, mode: str, embedded: list[str], lrc_text: str | None) -> None:
        """Before the first write: keep what the chosen output held, so it can be restored."""
        if mode == "lrc":
            self.save_lrc_backup(path, lrc_text)
        else:
            self.save_backup(path, next((t for t in embedded if t), ""))

    def get_backup(self, path: str) -> str | None:
        path = _k(path)
        row = self.db.execute("SELECT text FROM backups WHERE path=?", (path,)).fetchone()
        return row[0] if row else None

    # -- dry-run proposals
    def set_pending(self, path: str, source: str, label: str, text: str, lrc_path: str = "",
                    confidence: float | None = None, fit: float | None = None) -> None:
        path = _k(path)
        self.db.execute("REPLACE INTO pending (path, source, label, text, updated, lrc_path, confidence, fit) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (path, source, label, text, time.time(), lrc_path or "", _f(confidence), _f(fit)))
        self._tick()

    def pending_scores(self) -> dict[str, tuple[float | None, float | None]]:
        """{path: (match confidence, fit) of its proposal (None = unknown)}."""
        return {p: (c, f) for p, c, f in self.db.execute("SELECT path, confidence, fit FROM pending")}

    def set_pending_fit(self, path: str, text: str, fit: float | None) -> None:
        """Update the fit of a proposal (when the audio has been checked), if it's still these lyrics."""
        from .lrc import same_lyrics
        row = self.get_pending(path)
        if row is not None and same_lyrics(row[2], text):
            self.db.execute("UPDATE pending SET fit=? WHERE path=?", (_f(fit), _k(path)))
            self._tick()

    def get_pending(self, path: str):
        """(source, label, text, lrc_path) or None."""
        path = _k(path)
        return self.db.execute("SELECT source, label, text, lrc_path FROM pending WHERE path=?",
                               (path,)).fetchone()

    def pending_paths(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT path FROM pending")}

    def override_kinds(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT path, kind FROM overrides").fetchall())

    def clear_pending(self, path: str) -> None:
        path = _k(path)
        self.db.execute("DELETE FROM pending WHERE path=?", (path,))
        self._tick()

    # -- per-file choices
    def get_override(self, path: str):
        """(kind, text, label, lrc_path) or None."""
        path = _k(path)
        return self.db.execute("SELECT kind, text, label, lrc_path FROM overrides WHERE path=?",
                               (path,)).fetchone()

    def set_override(self, path: str, kind: str, text: str = "", label: str = "", lrc_path: str = "") -> None:
        path = _k(path)
        self.db.execute("REPLACE INTO overrides (path, kind, text, label, updated, lrc_path) VALUES (?,?,?,?,?,?)",
                        (path, kind, text, label, time.time(), lrc_path or ""))
        self.commit()

    def clear_override(self, path: str) -> None:
        path = _k(path)
        self.db.execute("DELETE FROM overrides WHERE path=?", (path,))
        self.commit()

    # -- per-song review preferences (checkpoints, timing draft)
    def get_song_prefs(self, path: str) -> dict:
        path = _k(path)
        row = self.db.execute("SELECT prefs FROM song_prefs WHERE path=?", (path,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_song_prefs(self, path: str, prefs: dict) -> None:
        path = _k(path)
        self.db.execute("REPLACE INTO song_prefs VALUES (?,?,?)", (path, json.dumps(prefs), time.time()))
        self.commit()

    def delete_file(self, path: str) -> None:
        self.db.execute("DELETE FROM files WHERE path=?", (_k(path),))
        self.commit()

    # -- every source found for a file (for the Choice column)
    def clear_candidates(self, path: str) -> None:
        self.db.execute("DELETE FROM candidates WHERE path=?", (_k(path),))

    def set_candidate(self, path: str, kind: str, label: str, text: str, lrc_path: str = "",
                      length: float = 0.0, confidence: float | None = None, fit: float | None = None) -> None:
        self.db.execute("REPLACE INTO candidates (path, kind, label, text, lrc_path, updated, length, confidence, "
                        "fit) VALUES (?,?,?,?,?,?,?,?,?)",
                        (_k(path), kind, label, text, lrc_path or "", time.time(), float(length or 0),
                         _f(confidence), _f(fit)))
        self._tick()

    def set_candidate_fit(self, path: str, kind: str, fit: float | None) -> None:
        self.db.execute("UPDATE candidates SET fit=? WHERE path=? AND kind=?", (_f(fit), _k(path), kind))
        self._tick()

    def all_candidate_scores(self) -> dict[str, dict[str, tuple[float | None, float | None]]]:
        """{path: {kind: (match confidence, fit)}}."""
        out: dict[str, dict[str, tuple]] = {}
        for p, k, c, f in self.db.execute("SELECT path, kind, confidence, fit FROM candidates"):
            out.setdefault(p, {})[k] = (c, f)
        return out

    def candidate_scores(self, path: str) -> dict[str, tuple[float | None, float | None]]:
        """{kind: (match confidence, fit)} for the local / online candidates."""
        return {k: (c, f) for k, c, f in
                self.db.execute("SELECT kind, confidence, fit FROM candidates WHERE path=?", (_k(path),))}

    def get_candidates(self, path: str) -> dict[str, tuple[str, str, str, float]]:
        """{kind: (label, text, lrc_path, source length in s or 0)} for kind in local / online."""
        rows = self.db.execute("SELECT kind, label, text, lrc_path, length FROM candidates WHERE path=?",
                               (_k(path),)).fetchall()
        return {k: (l, t, p, float(n or 0)) for k, l, t, p, n in rows}

    def choice_kind_for_text(self, path: str, text: str | None) -> str:
        from .lrc import same_lyrics

        for kind, (_, t, _, _) in self.get_candidates(path).items():
            if text and kind in ("local", "online") and same_lyrics(t, text):
                return kind
        return "custom" if text else "remove"

    # -- GPU alignment (ai_align)
    def get_aligned(self, path: str):
        """(src_hash, ok, verify, reason, text, confidence, fit, kind) or None."""
        return self.db.execute("SELECT src_hash, ok, verify, reason, text, confidence, fit, kind FROM aligned "
                               "WHERE path=?", (_k(path),)).fetchone()

    def set_aligned(self, path: str, src_hash: str, ok: bool, verify: float, reason: str, text: str = "",
                    confidence: float | None = None, fit: float | None = None, kind: str = "",
                    orig: str = "", pins: dict | None = None, heard: list | None = None, mode: str = "",
                    ends: dict | None = None, confs: dict | None = None) -> None:
        import json
        self.db.execute("REPLACE INTO aligned (path, src_hash, ok, verify, reason, text, confidence, fit, kind, "
                        "updated, orig, pins, heard, mode, ends, confs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (_k(path), src_hash, 1 if ok else 0, float(verify), reason, text, _f(confidence), _f(fit),
                         kind, time.time(), orig or "", json.dumps(pins or {}), json.dumps(heard or []), mode or "",
                         json.dumps(ends or {}), json.dumps(confs or {})))
        self._tick()

    def get_aligned_detail(self, path: str) -> dict | None:
        """Everything the player needs to show an alignment: the original timing, the aligner's
        pins, the words it heard (with probabilities) and the verify score."""
        import json
        row = self.db.execute("SELECT ok, verify, reason, text, orig, pins, heard, mode, ends, confs FROM aligned "
                              "WHERE path=?", (_k(path),)).fetchone()
        if row is None:
            return None

        def js(v, empty):
            try:
                return json.loads(v) if v else empty
            except ValueError:
                return empty
        pins, heard, ends, confs = js(row[5], {}), js(row[6], []), js(row[8], {}), js(row[9], {})
        return {"ok": bool(row[0]), "verify": row[1], "reason": row[2], "text": row[3] or "", "orig": row[4] or "",
                "pins": {k: float(v) for k, v in pins.items()}, "heard": heard, "mode": row[7] or "slope",
                "ends": {k: float(v) for k, v in ends.items()}, "confs": {k: float(v) for k, v in confs.items()}}

    def use_aligned(self, path: str, kind: str, old_text: str, new_text: str, label: str,
                    confidence: float | None, fit: float | None, base: str | None = None) -> bool:
        """Swap the found lyrics of `kind` (and the proposal, if it is still those lyrics) for the
        re-timed version; the original timing is kept as candidate '<kind>-original'. base: the timing
        the aligner started from when it isn't old_text (aligning again from the original)."""
        from .lrc import same_lyrics
        path = _k(path)
        cands = self.get_candidates(path)
        pend = self.get_pending(path)
        if pend is None or not same_lyrics(pend[2], old_text):
            return False                                  # saved or changed meanwhile: leave it
        if kind in cands and same_lyrics(cands[kind][1], old_text):
            olabel, otext, olrc, olen = cands[kind]
            oc = self.candidate_scores(path).get(kind, (None, None))
            orig = cands.get(kind + "-original")
            if base is None or same_lyrics(base, old_text) or orig is None or not same_lyrics(orig[1], base):
                self.set_candidate(path, kind + "-original", olabel, otext, olrc, olen, oc[0], oc[1])
            self.set_candidate(path, kind, label, new_text, olrc, olen, confidence, fit)
        self.db.execute("UPDATE pending SET text=?, label=?, confidence=?, fit=?, updated=? WHERE path=?",
                        (new_text, label, _f(confidence), _f(fit), time.time(), path))
        self._tick()
        return True

    def current_choice(self, path: str) -> str:
        """What Apply will do for this file: local | online | keep | custom | remove."""
        ov = self.get_override(path)
        if ov is not None:
            return "keep" if ov[0] == KEEP else self.choice_kind_for_text(path, ov[1])
        pend = self.get_pending(path)
        if pend is None:
            return "keep"
        if pend[0] == "lrclib":
            return "online"
        if pend[0] in ("local .lrc", "collection", "embedded"):
            return "local"
        return self.choice_kind_for_text(path, pend[2])

    # -- misc
    def _tick(self) -> None:
        self._pending += 1
        if self._pending >= 50:
            self.commit()

    def commit(self) -> None:
        self.db.commit()
        self._pending = 0

    def close(self) -> None:
        self.commit()
        self.db.close()
