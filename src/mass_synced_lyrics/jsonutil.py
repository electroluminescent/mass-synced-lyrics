"""JSON that also takes numpy numbers (float32 and the like): they turn up in scores and certainties."""
from __future__ import annotations

import json


def plain(o):
    """json default=: numpy scalars and other number-likes become plain numbers; sets become lists."""
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    try:
        return float(o)
    except (TypeError, ValueError):
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable") from None


def dumps(obj, **kw) -> str:
    return json.dumps(obj, default=plain, **kw)
