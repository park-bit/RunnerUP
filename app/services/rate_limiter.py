"""In-memory, per-user and global rate limiting.

Uses a fixed-size sliding window of timestamps per user plus a global window.
State lives entirely in process memory - there is no Redis or database - which
is exactly what the Render free tier calls for. A restart simply resets all
counters (documented behavior).

Memory is bounded: each user keeps at most ``per_user`` timestamps, and a
periodic sweep discards users whose windows have fully expired.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional


@dataclass
class RateLimitResult:
    allowed: bool
    scope: Optional[str] = None  # "user" | "global" | None
    retry_after: float = 0.0     # seconds until the caller may retry


class RateLimiter:
    def __init__(
        self,
        per_user: int,
        global_limit: int,
        window: float = 60.0,
    ) -> None:
        self.per_user = max(1, int(per_user))
        self.global_limit = max(1, int(global_limit))
        self.window = float(window)
        self._user_hits: Dict[int, Deque[float]] = defaultdict(deque)
        self._global_hits: Deque[float] = deque()
        self._last_sweep = 0.0

    def _prune(self, hits: Deque[float], now: float) -> None:
        cutoff = now - self.window
        while hits and hits[0] <= cutoff:
            hits.popleft()

    def _maybe_sweep(self, now: float) -> None:
        # Periodically drop users whose windows have fully expired so the dict
        # does not grow unbounded with one-off users.
        if now - self._last_sweep < self.window:
            return
        self._last_sweep = now
        stale = []
        for user_id, hits in self._user_hits.items():
            self._prune(hits, now)
            if not hits:
                stale.append(user_id)
        for user_id in stale:
            del self._user_hits[user_id]

    def try_acquire(self, user_id: int, now: Optional[float] = None) -> RateLimitResult:
        """Check limits and, if allowed, record one execution.

        Global limit is checked first (cheapest way to shed load), then the
        per-user limit. The window uses a monotonic clock so it is immune to
        system clock changes. ``now`` may be supplied for deterministic tests.
        """
        if now is None:
            now = time.monotonic()
        self._maybe_sweep(now)

        # Global window
        self._prune(self._global_hits, now)
        if len(self._global_hits) >= self.global_limit:
            retry = self.window - (now - self._global_hits[0])
            return RateLimitResult(False, "global", max(0.0, retry))

        # Per-user window
        hits = self._user_hits[user_id]
        self._prune(hits, now)
        if len(hits) >= self.per_user:
            retry = self.window - (now - hits[0])
            # Do not leave an empty deque lying around if it became empty.
            if not hits:
                del self._user_hits[user_id]
            return RateLimitResult(False, "user", max(0.0, retry))

        # Allowed -> record.
        hits.append(now)
        self._global_hits.append(now)
        return RateLimitResult(True, None, 0.0)

    # Introspection helpers (handy for /health or debugging; never required).
    def active_users(self) -> int:
        return len(self._user_hits)

    def global_count(self, now: Optional[float] = None) -> int:
        if now is None:
            now = time.monotonic()
        self._prune(self._global_hits, now)
        return len(self._global_hits)
