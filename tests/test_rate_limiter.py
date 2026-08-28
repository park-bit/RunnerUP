"""Tests for the in-memory rate limiter.

A deterministic ``now`` is passed to :meth:`RateLimiter.try_acquire` so the
sliding window can be exercised without real time passing.
"""

from app.services.rate_limiter import RateLimiter


def test_first_request_is_allowed():
    rl = RateLimiter(per_user=5, global_limit=100, window=60)
    assert rl.try_acquire(1, now=0.0).allowed is True


def test_per_user_limit_is_enforced():
    rl = RateLimiter(per_user=3, global_limit=100, window=60)
    for i in range(3):
        assert rl.try_acquire(42, now=float(i)).allowed is True
    blocked = rl.try_acquire(42, now=3.0)
    assert blocked.allowed is False
    assert blocked.scope == "user"


def test_global_limit_is_enforced():
    rl = RateLimiter(per_user=100, global_limit=2, window=60)
    assert rl.try_acquire(1, now=0.0).allowed is True
    assert rl.try_acquire(2, now=0.0).allowed is True
    blocked = rl.try_acquire(3, now=0.0)
    assert blocked.allowed is False
    assert blocked.scope == "global"


def test_window_expiry_frees_capacity():
    rl = RateLimiter(per_user=1, global_limit=100, window=60)
    assert rl.try_acquire(7, now=0.0).allowed is True
    # Still inside the window -> blocked.
    assert rl.try_acquire(7, now=30.0).allowed is False
    # After the full window has elapsed -> allowed again.
    assert rl.try_acquire(7, now=61.0).allowed is True


def test_users_are_independent():
    rl = RateLimiter(per_user=1, global_limit=100, window=60)
    assert rl.try_acquire(1, now=0.0).allowed is True
    assert rl.try_acquire(2, now=0.0).allowed is True  # different user, fine
    assert rl.try_acquire(1, now=1.0).allowed is False  # first user still capped


def test_blocked_request_is_not_recorded():
    # A denied attempt must not consume capacity, so once the window frees up
    # the user gets exactly their allotment.
    rl = RateLimiter(per_user=1, global_limit=100, window=60)
    assert rl.try_acquire(9, now=0.0).allowed is True
    assert rl.try_acquire(9, now=10.0).allowed is False  # denied
    assert rl.try_acquire(9, now=20.0).allowed is False  # still denied
    assert rl.try_acquire(9, now=61.0).allowed is True   # window cleared
