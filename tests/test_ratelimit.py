from __future__ import annotations

import time

import pytest

from github_actions_ingester.ratelimit import RateLimiter


def test_rejects_non_positive_rps() -> None:
    with pytest.raises(ValueError, match="rps"):
        RateLimiter(0)


def test_paces_consecutive_calls() -> None:
    limiter = RateLimiter(50.0)  # 20 ms between slots
    t0 = time.monotonic()
    for _ in range(4):
        limiter.acquire()
    elapsed = time.monotonic() - t0
    # first call is immediate, three more slots of 20 ms each
    assert elapsed >= 0.055


def test_first_call_is_immediate() -> None:
    limiter = RateLimiter(1.0)
    t0 = time.monotonic()
    limiter.acquire()
    assert time.monotonic() - t0 < 0.1
