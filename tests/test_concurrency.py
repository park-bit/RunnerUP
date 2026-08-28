"""Tests for the concurrency guard (the 'one execution at a time' guarantee)."""

import asyncio

from app.services.concurrency import ConcurrencyGuard


def _run(coro):
    return asyncio.run(coro)


def test_single_slot_acquire_and_release():
    async def scenario():
        guard = ConcurrencyGuard(max_concurrent=1, max_queue=0)
        assert await guard.acquire() is True
        # Only slot is held and the queue is empty -> next acquire is rejected.
        assert await guard.acquire() is False
        guard.release()
        # After release the slot is available again.
        assert await guard.acquire() is True
        guard.release()

    _run(scenario())


def test_queue_allows_one_waiter_then_rejects():
    async def scenario():
        guard = ConcurrencyGuard(max_concurrent=1, max_queue=1)
        assert await guard.acquire() is True  # running slot taken

        # A waiter reserves the single queued slot and blocks on the semaphore.
        waiter = asyncio.create_task(guard.acquire())
        await asyncio.sleep(0.05)  # let the waiter register in the queue

        # Running + queued are both occupied -> immediate rejection.
        assert await guard.acquire() is False

        # Releasing the running slot lets the queued waiter proceed.
        guard.release()
        assert await waiter is True
        guard.release()

    _run(scenario())


def test_capacity_is_running_plus_queue():
    guard = ConcurrencyGuard(max_concurrent=1, max_queue=2)
    assert guard.capacity == 3
    assert guard.in_flight == 0
