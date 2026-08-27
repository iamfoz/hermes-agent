"""Tests for the per-base_url outbound-API concurrency gate.

The gate is a process-wide ``threading.Semaphore`` registry keyed by
provider ``base_url``. Configured providers block FIFO at their
``max_parallel_requests`` ceiling; unconfigured providers are unbounded
(acquire is a no-op). See agent/concurrency_gate.py.
"""

from __future__ import annotations

import threading
import time

import pytest

from agent import concurrency_gate as cg


@pytest.fixture(autouse=True)
def _reset_gate():
    """Each test starts with an empty registry (process-wide global state)."""
    cg.reset_for_tests()
    yield
    cg.reset_for_tests()


# ── set_limit / get_limit / normalization ────────────────────────────────────


def test_set_and_get_limit():
    cg.set_limit("http://127.0.0.1:7884/v1", 3)
    assert cg.get_limit("http://127.0.0.1:7884/v1") == 3


def test_key_normalization_trailing_slash_and_case():
    cg.set_limit("http://Example.com/v1/", 2)
    # Trailing slash stripped + lowercased - lookups match regardless of form.
    assert cg.get_limit("http://example.com/v1") == 2
    assert cg.get_limit("http://example.com/v1/") == 2
    assert cg.get_limit("HTTP://EXAMPLE.COM/V1") == 2


def test_unconfigured_key_has_no_limit():
    assert cg.get_limit("http://never-configured/v1") is None


def test_set_limit_none_or_nonpositive_removes_gate():
    cg.set_limit("http://x/v1", 4)
    assert cg.get_limit("http://x/v1") == 4
    cg.set_limit("http://x/v1", None)
    assert cg.get_limit("http://x/v1") is None
    cg.set_limit("http://x/v1", 4)
    cg.set_limit("http://x/v1", 0)
    assert cg.get_limit("http://x/v1") is None


def test_empty_key_is_ignored():
    cg.set_limit("", 3)
    cg.set_limit(None, 3)  # type: ignore[arg-type]
    assert cg.snapshot() == {}


# ── configure_from_custom_providers ───────────────────────────────────────────


def test_configure_from_custom_providers_sets_limits():
    cg.configure_from_custom_providers([
        {"base_url": "http://a/v1", "max_parallel_requests": 2},
        {"base_url": "http://b/v1", "max_parallel_requests": 5},
    ])
    assert cg.get_limit("http://a/v1") == 2
    assert cg.get_limit("http://b/v1") == 5


def test_configure_skips_missing_or_invalid_limits():
    cg.configure_from_custom_providers([
        {"base_url": "http://no-limit/v1"},                       # missing key
        {"base_url": "http://zero/v1", "max_parallel_requests": 0},   # non-positive
        {"base_url": "http://neg/v1", "max_parallel_requests": -1},   # negative
        {"base_url": "http://str/v1", "max_parallel_requests": "3"},  # wrong type
        {"max_parallel_requests": 3},                             # no base_url
        "not-a-dict",                                             # junk entry
    ])
    assert cg.snapshot() == {}


def test_configure_empty_iterable_is_noop():
    cg.configure_from_custom_providers([])
    cg.configure_from_custom_providers(None)  # type: ignore[arg-type]
    assert cg.snapshot() == {}


# ── acquire semantics ─────────────────────────────────────────────────────────


def test_acquire_unbounded_is_immediate_noop():
    # No gate configured for this key → yields True immediately.
    with cg.acquire("http://unbounded/v1") as ok:
        assert ok is True


def test_acquire_empty_key_is_immediate_noop():
    with cg.acquire("") as ok:
        assert ok is True


def test_acquire_releases_slot_on_exit():
    cg.set_limit("http://one/v1", 1)
    # First acquire takes the only slot; after the with-block it's released,
    # so a second acquire succeeds immediately.
    with cg.acquire("http://one/v1") as ok:
        assert ok is True
    with cg.acquire("http://one/v1", timeout=0.5) as ok:
        assert ok is True


def test_acquire_timeout_when_slot_unavailable():
    cg.set_limit("http://busy/v1", 1)
    # Hold the only slot in a background thread, then a timed acquire must fail.
    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with cg.acquire("http://busy/v1") as ok:
            assert ok is True
            holding.set()
            release.wait(timeout=5)

    t = threading.Thread(target=_hold)
    t.start()
    try:
        assert holding.wait(timeout=5), "holder never acquired the slot"
        # Slot is taken - a short timed acquire returns False.
        with cg.acquire("http://busy/v1", timeout=0.2) as ok:
            assert ok is False
    finally:
        release.set()
        t.join(timeout=5)

    # Once the holder releases, the slot is free again.
    with cg.acquire("http://busy/v1", timeout=0.5) as ok:
        assert ok is True


def test_acquire_serializes_concurrent_callers_to_the_limit():
    """With limit=2, at most 2 callers run the protected region at once."""
    cg.set_limit("http://gated/v1", 2)
    concurrent = 0
    peak = 0
    peak_lock = threading.Lock()
    start = threading.Event()

    def _worker():
        nonlocal concurrent, peak
        start.wait(timeout=5)
        with cg.acquire("http://gated/v1") as ok:
            assert ok is True
            with peak_lock:
                concurrent += 1
                peak = max(peak, concurrent)
            time.sleep(0.05)
            with peak_lock:
                concurrent -= 1

    threads = [threading.Thread(target=_worker) for _ in range(6)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join(timeout=5)

    # The gate must never have let more than `limit` callers in at once.
    assert peak <= 2, f"concurrency gate exceeded limit: peak={peak}"


def test_snapshot_is_a_copy():
    cg.set_limit("http://a/v1", 1)
    snap = cg.snapshot()
    snap["http://a/v1"] = 999
    # Mutating the snapshot must not affect the live registry.
    assert cg.get_limit("http://a/v1") == 1
