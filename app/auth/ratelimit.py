"""In-process sliding-window rate limits (per principal, and per IP for unauthenticated sign-in endpoints)."""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque

_WINDOW_S = 60.0
_hits: dict[str, deque[float]] = defaultdict(deque)


def _limit(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def per_principal_limit() -> int:
    return _limit("MSP_RATE_LIMIT_PER_MIN", 600)


def anonymous_limit() -> int:
    return _limit("MSP_RATE_LIMIT_ANON_PER_MIN", 60)


def allow(key: str, limit: int) -> bool:
    now = time.monotonic()
    q = _hits[key]
    while q and q[0] <= now - _WINDOW_S:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    if len(_hits) > 50_000:  # keep memory bounded under abuse
        for k in [k for k, v in list(_hits.items()) if not v][:10_000]:
            _hits.pop(k, None)
    return True


def reset() -> None:
    _hits.clear()
