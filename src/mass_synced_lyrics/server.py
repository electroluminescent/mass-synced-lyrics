"""Backend process for the desktop app (Electron).

The window (Electron) starts this process and talks to it over stdin/stdout, one JSON object per line:
  → {"id": 7, "method": "find.start", "params": {...}}
  ← {"id": 7, "result": ...}            or {"id": 7, "error": "message"}
  ← {"event": "rows", "data": {...}}    pushed whenever something changes

Everything that changes state runs on one thread (the Loop), like a GUI main loop: requests, events
from worker threads (Find, Align, Apply) and timers are queued and handled in order. Work that takes
a while runs on worker threads and posts its results back to the loop.
"""
from __future__ import annotations

import heapq
import itertools
import json
import os
import queue
import sys
import threading
import time
import traceback

from .jsonutil import plain as _plain


class Loop:
    """A single-threaded event loop: post() from any thread, after() for timers."""

    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._timers: list = []
        self._seq = itertools.count()
        self._cancelled: set[int] = set()
        self._lock = threading.Lock()
        self.stopped = False
        self.thread_id: int | None = None
        self.on_error = None

    def post(self, fn, *args) -> None:
        self._q.put((fn, args))

    def after(self, ms: float, fn, *args) -> int:
        tid = next(self._seq)
        with self._lock:
            heapq.heappush(self._timers, (time.monotonic() + ms / 1000.0, tid, fn, args))
        self._q.put(None)                                  # wake the loop to re-check its timers
        return tid

    def cancel(self, tid: int | None) -> None:
        if tid is not None:
            with self._lock:
                self._cancelled.add(tid)

    def stop(self) -> None:
        self.stopped = True
        self._q.put(None)

    def _due(self):
        now = time.monotonic()
        out = []
        with self._lock:
            while self._timers and self._timers[0][0] <= now:
                _, tid, fn, args = heapq.heappop(self._timers)
                if tid in self._cancelled:
                    self._cancelled.discard(tid)
                    continue
                out.append((fn, args))
            wait = (self._timers[0][0] - now) if self._timers else 0.5
        return out, max(0.0, min(0.5, wait))

    def _call(self, fn, args) -> None:
        try:
            fn(*args)
        except Exception:
            if self.on_error is not None:
                self.on_error(traceback.format_exc())
            else:
                traceback.print_exc(file=sys.stderr)

    def run_once(self, timeout: float = 0.0) -> bool:
        """Handle what is due now (and wait up to `timeout` for more). False once stopped."""
        due, wait = self._due()
        for fn, args in due:
            self._call(fn, args)
        try:
            item = self._q.get(timeout=min(wait, timeout) if timeout else 0.0) if timeout else \
                self._q.get_nowait()
        except queue.Empty:
            return not self.stopped
        if item is not None:
            self._call(*item)
        for _ in range(500):                               # drain a burst without re-checking timers each time
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                self._call(*item)
        return not self.stopped

    def run(self) -> None:
        self.thread_id = threading.get_ident()
        while not self.stopped:
            due, wait = self._due()
            for fn, args in due:
                self._call(fn, args)
            try:
                item = self._q.get(timeout=wait)
            except queue.Empty:
                continue
            if item is not None:
                self._call(*item)


class Server:
    """JSON-lines transport: reads requests, writes responses and events."""

    def __init__(self, out=None):
        self._out = out
        self._wlock = threading.Lock()
        self.loop = Loop()
        self.loop.on_error = lambda tb: self.event("log", {"lines": [time.strftime("%H:%M:%S  ") +
                                                                     "Internal error: " + tb.strip().splitlines()[-1]]},
                                                   ) or sys.stderr.write(tb)
        self.backend = None

    def send(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=True, default=_plain, separators=(",", ":"))
        with self._wlock:
            try:
                self._out.write(line + "\n")
                self._out.flush()
            except (OSError, ValueError):
                pass

    def event(self, name: str, data=None) -> None:
        self.send({"event": name, "data": data})

    def handle_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            return
        self.loop.post(self._dispatch, msg)

    def _dispatch(self, msg: dict) -> None:
        mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
        try:
            fn = self.backend.rpc(method)
            if fn is None:
                raise ValueError(f"unknown method {method}")
            result = fn(**params) if isinstance(params, dict) else fn(*params)
            if mid is not None:
                self.send({"id": mid, "result": result})
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            if mid is not None:
                self.send({"id": mid, "error": f"{type(e).__name__}: {e}"})

    def read_stdin(self, inp) -> None:
        for line in inp:
            line = line.strip()
            if line:
                self.handle_line(line)
        self.loop.post(self.backend.shutdown)               # the window went away: tidy up and leave


def main() -> None:
    # stdout carries the protocol only: anything printed by libraries goes to stderr instead
    out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
    sys.stdout = sys.stderr
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    server = Server(out)
    from .controller import Backend

    server.backend = Backend(server)
    threading.Thread(target=server.read_stdin, args=(sys.stdin,), daemon=True).start()
    server.loop.run()


if __name__ == "__main__":
    main()
