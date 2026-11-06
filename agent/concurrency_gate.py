"""Process-wide concurrency gate for outbound provider API calls.

Hermes runs many concurrent API callers in a single process: the Telegram
gateway, CLI sessions, cron jobs, delegate-task children.  Upstream providers
impose per-API-key parallel-request limits (e.g. AIRouter caps at 3); when the
in-process callers stampede past that ceiling the provider returns 429 and the
turn fails.

This module provides a per-``base_url`` ``threading.Semaphore`` registry that
callers wrap around their HTTP request.  Limits are configured per provider in
``config.yaml`` (``max_parallel_requests``) and pushed into the gate via
``configure_from_custom_providers``.  Unconfigured base_urls are unbounded -
the context manager becomes a no-op.

Design notes:
  * Keying by ``base_url`` matches one-API-key-per-endpoint deployments, which
    is the common Hermes layout.  Trailing slashes are stripped to canonicalize.
  * One process serves many profiles, and each profile owns its own
    ``config.yaml``, so the registry is scoped by ``hermes_home_key()``: a limit
    one profile configures never gates another profile's calls to the same URL.
  * Bedrock and other transports without a base_url fall back to keying by
    provider name passed to ``acquire``.
  * Acquire is blocking and FIFO (CPython's ``threading.Semaphore`` is FIFO
    in practice via the underlying ``_PySemLock`` waiter queue).  Callers that
    must not block can pass ``timeout`` to ``acquire`` and handle the False
    return.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from hermes_constants import hermes_home_key

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# Keyed by (profile home key, normalized base_url).
_semaphores: Dict[Tuple[str, str], threading.Semaphore] = {}
_limits: Dict[Tuple[str, str], int] = {}


def _normalize_key(key: Optional[str]) -> str:
    if not key:
        return ""
    return key.strip().rstrip("/").lower()


def _scoped(norm: str) -> Tuple[str, str]:
    return (hermes_home_key(), norm)


def set_limit(key: str, limit: Optional[int]) -> None:
    """Register or update the parallel-request ceiling for ``key``.

    ``key`` is typically the provider ``base_url``.  Passing ``None`` or a
    non-positive limit removes the gate for that key (unbounded).  Re-setting
    the same limit is a no-op; changing the limit replaces the semaphore (any
    in-flight callers continue under the old semaphore, which is the simplest
    safe behavior - they release into an orphaned object and the next acquire
    uses the new one).
    """
    norm = _normalize_key(key)
    if not norm:
        return
    scoped = _scoped(norm)
    with _lock:
        if limit is None or limit <= 0:
            _semaphores.pop(scoped, None)
            _limits.pop(scoped, None)
            return
        if _limits.get(scoped) == limit:
            return
        _semaphores[scoped] = threading.Semaphore(limit)
        _limits[scoped] = limit
        logger.info("concurrency_gate: %s limit=%d", norm, limit)


def get_limit(key: str) -> Optional[int]:
    return _limits.get(_scoped(_normalize_key(key)))


def configure_from_custom_providers(custom_providers: Iterable[dict]) -> None:
    """Populate gate limits from a list of normalized custom-provider entries.

    Each entry may carry ``max_parallel_requests`` (int >= 1).  Missing/invalid
    values leave the corresponding endpoint unbounded.
    """
    if not custom_providers:
        return
    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        base_url = entry.get("base_url")
        raw = entry.get("max_parallel_requests")
        if not isinstance(raw, int) or raw <= 0:
            continue
        if isinstance(base_url, str) and base_url.strip():
            set_limit(base_url, raw)


@contextmanager
def acquire(key: str, *, timeout: Optional[float] = None):
    """Context manager that holds a slot on the semaphore for ``key``.

    Yields ``True`` if a slot was acquired (or no gate is configured),
    ``False`` if ``timeout`` elapsed without acquiring.  When unbounded the
    yield is immediate and always ``True``.
    """
    norm = _normalize_key(key)
    if not norm:
        yield True
        return
    sem = _semaphores.get(_scoped(norm))
    if sem is None:
        yield True
        return
    if timeout is None:
        sem.acquire()
        acquired = True
    else:
        acquired = sem.acquire(timeout=timeout)
    try:
        yield acquired
    finally:
        if acquired:
            sem.release()


def gated(agent: Any, callback: Callable[[], None]) -> Callable[[], None]:
    """Wrap a request-thread target so the call runs inside the agent's endpoint gate.

    Limits are read from ``agent._custom_providers`` when the call runs, inside the
    request thread's (propagated) profile scope, so they land in the right profile's
    registry; re-applying an unchanged limit is a no-op.
    """
    def _run() -> None:
        configure_from_custom_providers(getattr(agent, "_custom_providers", None))
        with acquire(agent.base_url or agent.provider):
            callback()
    return _run


def snapshot() -> Dict[str, int]:
    """Return a copy of the current profile's configured limits (for diagnostics/tests)."""
    home = hermes_home_key()
    with _lock:
        return {norm: limit for (scope, norm), limit in _limits.items() if scope == home}


def reset_for_tests() -> None:
    with _lock:
        _semaphores.clear()
        _limits.clear()
