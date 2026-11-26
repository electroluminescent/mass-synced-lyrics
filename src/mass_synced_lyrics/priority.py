"""Songs wanted first: the song you select in Review & player jumps the queue of every running operation
(Find, Align, Save) so it can be dealt with straight away."""
from __future__ import annotations

import threading
from collections import deque

from .paths import state_key

_lock = threading.Lock()
_wanted: dict[str, set[str]] = {"find": set(), "save": set()}


def want_first(paths, ops=("find", "save")) -> None:
    keys = {state_key(p) for p in paths}
    with _lock:
        for op in ops:
            _wanted.setdefault(op, set()).clear()
            _wanted[op].update(keys)


def pick(op: str, queue: deque, key=lambda item: item):
    """Take the next item from `queue`: a wanted one if it is waiting there, else the first."""
    with _lock:
        wanted = _wanted.get(op)
        if wanted:
            for i, item in enumerate(queue):
                k = state_key(key(item))
                if k in wanted:
                    wanted.discard(k)
                    del queue[i]
                    return item
            wanted.clear()                    # not waiting in this queue (done, or not reached yet)
    return queue.popleft()


def clear() -> None:
    with _lock:
        for s in _wanted.values():
            s.clear()
