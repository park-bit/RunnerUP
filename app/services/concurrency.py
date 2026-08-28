"""In-memory concurrency control for executions.

The free instance has ~0.1 CPU, so we run **one** execution at a time by
default and allow a tiny wait-queue. There is intentionally no pool, no worker,
and no external queue - just an :class:`asyncio.Semaphore` and a counter.

Usage::

    if not await guard.acquire():
        # queue full -> tell the user we're busy
        ...
    else:
        try:
            result = await executor.execute(code)
        finally:
            guard.release()
"""

from __future__ import annotations

import asyncio


class ConcurrencyGuard:
    def __init__(self, max_concurrent: int = 1, max_queue: int = 2) -> None:
        self.max_concurrent = max(1, int(max_concurrent))
        self.max_queue = max(0, int(max_queue))
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._in_flight = 0  # running + waiting

    @property
    def capacity(self) -> int:
        return self.max_concurrent + self.max_queue

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def is_full(self) -> bool:
        return self._in_flight >= self.capacity

    async def acquire(self) -> bool:
        """Reserve an execution slot.

        Returns True once a slot is held (possibly after waiting in the small
        queue), or False immediately if the queue is already full. There is no
        ``await`` between the capacity check and the counter bump, so under
        asyncio's single-threaded model this is race-free.
        """
        if self._in_flight >= self.capacity:
            return False
        self._in_flight += 1
        try:
            await self._sem.acquire()
        except BaseException:
            self._in_flight -= 1
            raise
        return True

    def release(self) -> None:
        self._sem.release()
        self._in_flight -= 1
        if self._in_flight < 0:
            self._in_flight = 0
