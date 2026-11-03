"""Tests for the lancedb fork-safety warning filter installed when a memory
provider is loaded.

``hermes-memory-lancedb-pro`` (and any other lance-backed provider) makes
lancedb register an at-fork handler that re-emits a ``UserWarning`` every time
the hermes process forks (e.g. for each ``subprocess.Popen`` the terminal tool
spawns), flooding normal command output. ``plugins.memory`` installs a filter
for that one specific message when a provider is loaded.  These tests assert it
is specific (no global swallow) and idempotent.
"""

import warnings

import plugins.memory as pm


def test_only_lance_fork_warning_is_suppressed():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        pm._LANCE_FORK_WARNING_FILTERED = False  # allow (re)install in this context
        pm._suppress_lance_fork_safety_warning()
        assert pm._LANCE_FORK_WARNING_FILTERED is True

        warnings.warn(
            "lance is not fork-safe. If you are using multiprocessing, use spawn instead.",
            UserWarning,
        )
        warnings.warn("some other unrelated warning", UserWarning)
        warnings.warn("a deprecation here", DeprecationWarning)

    msgs = [str(w.message) for w in caught]
    # The targeted warning is gone...
    assert not any("lance is not fork-safe" in m for m in msgs)
    # ...but unrelated warnings (same and other categories) are NOT swallowed.
    assert any("some other unrelated warning" in m for m in msgs)
    assert any("deprecation here" in m for m in msgs)


def test_filter_install_is_idempotent():
    pm._LANCE_FORK_WARNING_FILTERED = False
    pm._suppress_lance_fork_safety_warning()
    count = len(warnings.filters)
    # Subsequent calls must not pile up duplicate filters.
    pm._suppress_lance_fork_safety_warning()
    pm._suppress_lance_fork_safety_warning()
    assert len(warnings.filters) == count


def test_load_memory_provider_installs_filter():
    """The public entry point installs the filter even for a missing provider
    (it is set before the lookup so a lance-backed plugin's import is covered)."""
    pm._LANCE_FORK_WARNING_FILTERED = False
    # A name that does not exist returns None but must still arm the filter.
    assert pm.load_memory_provider("definitely-not-a-real-provider-xyz") is None
    assert pm._LANCE_FORK_WARNING_FILTERED is True
