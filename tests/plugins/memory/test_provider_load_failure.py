"""A memory provider whose module fails to load must name the cause at WARNING, once.

Regression coverage for the misleading "loaded but no provider instance found" /
"NOT installed" pair: a provider directory that exists but whose ``__init__.py`` raises
(missing dependency, register() blowing up) used to log its cause at DEBUG only, then
report "loaded but no provider instance found" (the module never imported) and
``hermes memory status`` said "NOT installed" for a directory that was installed.

Behaviour-change tests (fail on the pre-fix code):
  * test_missing_top_level_import_names_cause_once        (spec A + B)
  * test_register_raising_before_registering_names_cause  (spec A)

Behaviour-preservation guards (pass before and after):
  * test_register_raising_after_registering_keeps_provider (spec E)
  * test_sibling_module_failure_stays_quier_than_the_main_module (spec F decision)
"""

from __future__ import annotations

import logging

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from plugins.memory import find_provider_dir, load_memory_provider

# New in the fix. Imported tolerantly so this file still COLLECTS on the pre-fix tree, where the
# red then shows up as the core assertion failing (the cause is logged at DEBUG, never WARNING)
# rather than as an import error.
try:
    from plugins.memory import _load_errors_for_active_home, clear_load_failure, load_failure_reason
except ImportError:  # pre-fix tree
    _load_errors_for_active_home = None
    clear_load_failure = None
    load_failure_reason = None

# Minimal MemoryProvider subclass body, rendered into each fake plugin's __init__.py.
_PROLOGUE = (
    "from agent.memory_provider import MemoryProvider\n"
    "\n"
    "\n"
    "class P(MemoryProvider):\n"
    '    name = "fakemem"\n'
    "\n"
    "    def is_available(self):\n"
    "        return True\n"
    "\n"
    "    def initialize(self, *a, **kw):\n"
    "        pass\n"
    "\n"
    "    def get_tool_schemas(self):\n"
    "        return []\n"
)

# A provider whose module imports fine and registers: the success path that must clear a stale
# recorded failure (a long-lived process must not keep reporting an old reason).
_REGISTER_OK_PROVIDER = (
    '"""demo memory provider (register_memory_provider)"""\n'
    + _PROLOGUE
    + "def register(ctx):\n"
    "    ctx.register_memory_provider(P())\n"
)

# Each fake plugin body is (docstring, body). The docstring names the memory-provider contract so
# the discovery heuristic (_is_memory_provider_dir) finds it.
_MISSING_IMPORT = (
    '"""demo memory provider (register_memory_provider)"""\n'
    "from definitely_missing_pkg_xyz.provider import register\n"
)

_REGISTER_RAISES_BEFORE = (
    '"""demo memory provider (register_memory_provider)"""\n'
    "def register(ctx):\n"
    '    raise ValueError("config broken")\n'
)

_REGISTER_RAISES_AFTER = (
    '"""demo memory provider (register_memory_provider)"""\n'
    + _PROLOGUE
    + "def register(ctx):\n"
    "    ctx.register_memory_provider(P())\n"
    '    raise RuntimeError("late failure")\n'
)

_REGISTER_OK = (
    '"""demo memory provider (register_memory_provider)"""\n'
    + _PROLOGUE
    + "def register(ctx):\n"
    "    ctx.register_memory_provider(P())\n"
)

_BAD_SIBLING = "from definitely_missing_sibling import helper  # noqa\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    token = set_hermes_home_override(tmp_path)
    # Fresh registry for this process's home (per-file isolation normally guarantees this).
    if _load_errors_for_active_home is not None:
        _load_errors_for_active_home().clear()
    yield plugins
    reset_hermes_home_override(token)


def _write_provider(plugins, name, init_body, siblings=None):
    d = plugins / name
    d.mkdir()
    (d / "plugin.yaml").write_text(f"name: {name}-manifest\nkind: exclusive\n", encoding="utf-8")
    (d / "__init__.py").write_text(init_body, encoding="utf-8")
    for stem, body in (siblings or {}).items():
        (d / f"{stem}.py").write_text(body, encoding="utf-8")
    return d


def test_missing_top_level_import_names_cause_once(home, caplog):
    """A missing dependency gets ONE WARNING naming the exception, the missing module and the
    supported fix - never "loaded but no provider instance found", never pip."""
    _write_provider(home, "demo_missing", _MISSING_IMPORT)
    assert find_provider_dir("demo_missing") == home / "demo_missing"

    with caplog.at_level(logging.DEBUG, logger="plugins.memory"):
        assert load_memory_provider("demo_missing") is None
        # Second load: same failure, no second warning (once per failure).
        assert load_memory_provider("demo_missing") is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name.startswith("plugins.memory")]
    causes = [r for r in warnings if "failed to load" in r.getMessage()]
    assert len(causes) == 1  # once, across two loads
    msg = causes[0].getMessage()
    assert "ModuleNotFoundError" in msg
    assert "definitely_missing_pkg_xyz" in msg
    assert "python_dependencies" in msg          # supported declaration path
    assert "hermes plugins install" in msg       # supported fix
    assert "hermes pm install" in msg            # re-sync after a PM rebuild
    assert " pip " not in f" {msg} " and not msg.rstrip().endswith("pip")  # never suggest pip

    # The cause is readable after the log line has scrolled away (status + agent init use this).
    if load_failure_reason is not None:
        reason = load_failure_reason("demo_missing")
        assert reason is not None and "definitely_missing_pkg_xyz" in reason

    # The old misleading message must not be used for a module that never imported.
    assert not any("loaded but no provider instance found" in r.getMessage() for r in caplog.records)

    # Full tracebacks stay at DEBUG.
    tracebacks = [r for r in caplog.records if r.levelno == logging.DEBUG and r.exc_info is not None]
    assert any("definitely_missing_pkg_xyz" in str(r.exc_info[1]) for r in tracebacks)


def test_register_raising_before_registering_names_cause(home, caplog):
    """register() raising BEFORE registering (and no subclass fallback) names the exception at
    WARNING instead of logging it at DEBUG and falling through to the generic message."""
    _write_provider(home, "demo_regfail", _REGISTER_RAISES_BEFORE)

    with caplog.at_level(logging.DEBUG, logger="plugins.memory"):
        assert load_memory_provider("demo_regfail") is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name.startswith("plugins.memory")]
    causes = [r for r in warnings if "register()" in r.getMessage()]
    assert len(causes) == 1
    msg = causes[0].getMessage()
    assert "ValueError: config broken" in msg
    if load_failure_reason is not None:
        reason = load_failure_reason("demo_regfail")
        assert reason is not None and "ValueError" in reason
    assert not any("loaded but no provider instance found" in r.getMessage() for r in caplog.records)


def test_register_raising_after_registering_keeps_provider(home, caplog):
    """Spec E: a raise AFTER register_memory_provider() still keeps the registered provider
    (existing warning path, unchanged)."""
    _write_provider(home, "demo_reglate", _REGISTER_RAISES_AFTER)

    with caplog.at_level(logging.DEBUG, logger="plugins.memory"):
        provider = load_memory_provider("demo_reglate")

    assert provider is not None and provider.is_available()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("raised after registering" in m and "late failure" in m for m in warnings)
    # The provider loaded; it is not recorded as a load failure.
    assert load_failure_reason is None or load_failure_reason("demo_reglate") is None


def test_sibling_module_failure_stays_quier_than_the_main_module(home, caplog):
    """Spec F decision: a failing SIBLING (optional cli.py) stays at DEBUG - the main module
    may still import fine without it. Only the main module's failure surfaces at WARNING."""
    _write_provider(home, "demo_sibling", _REGISTER_OK, siblings={"cli": _BAD_SIBLING})

    with caplog.at_level(logging.DEBUG, logger="plugins.memory"):
        provider = load_memory_provider("demo_sibling")

    assert provider is not None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("definitely_missing_sibling" in r.getMessage() for r in warnings)
    assert not any("failed to load" in r.getMessage() for r in warnings)
    # The sibling failure is still visible at DEBUG.
    assert any(r.levelno == logging.DEBUG and "definitely_missing_sibling" in r.getMessage()
               for r in caplog.records)


# ---------------------------------------------------------------------------
# Review-point tests (added after first pass): bundled-vs-user hint, falsy
# sentinel, entry-point recording, and stale-reason clearing. New symbols are
# imported inside each test so this file still collects on the pre-fix tree.
# ---------------------------------------------------------------------------

def _missing_module_error(name):
    e = ModuleNotFoundError()
    e.msg = f"No module named {name!r}"
    e.name = name
    return e


def test_module_missing_hint_user_default():
    """No repair passed -> the USER-INSTALLED text (declare deps, hermes plugins install)."""
    from plugins.plugin_loader import module_missing_hint
    hint = module_missing_hint(_missing_module_error("demo_dep"))
    assert hint is not None
    assert "demo_dep" in hint
    assert "python_dependencies" in hint
    assert "hermes plugins install" in hint


def test_module_missing_hint_custom_repair_replaces_user_text():
    """A caller-supplied repair (bundled provider) replaces the user-plugin text entirely."""
    from plugins.plugin_loader import module_missing_hint
    repair = "Run `hermes memory setup demo` to prepare its dependencies."
    hint = module_missing_hint(_missing_module_error("demo_dep"), repair=repair)
    assert hint is not None
    assert "demo_dep" in hint
    assert "hermes memory setup demo" in hint
    assert "hermes plugins install" not in hint  # user-plugin text must NOT leak in
    assert "pip" not in hint


def test_module_missing_hint_non_import_error_is_none():
    from plugins.plugin_loader import module_missing_hint
    assert module_missing_hint(ValueError("x")) is None
    assert module_missing_hint(_missing_module_error("d"), repair=None) is not None


def test_describe_load_failure_threads_repair():
    from plugins.plugin_loader import describe_load_failure
    out = describe_load_failure(
        _missing_module_error("demo_dep"), repair="Run `hermes pm install` to re-sync its dependencies.")
    assert "ModuleNotFoundError" in out
    assert "demo_dep" in out
    assert "hermes pm install" in out
    assert "hermes plugins install" not in out
    # No repair -> user-plugin text.
    assert "hermes plugins install" in describe_load_failure(_missing_module_error("demo_dep"))


def test_load_failed_sentinel_is_falsy_with_repr():
    """Review point 3: the sentinel must be FALSY so any ``if provider:`` check degrades it to
    'no provider', and must have a readable repr (no default '<_LoadFailedSentinel object>')."""
    from plugins.plugin_loader import LOAD_FAILED
    assert bool(LOAD_FAILED) is False
    assert not LOAD_FAILED
    assert repr(LOAD_FAILED) == "LOAD_FAILED"


def test_load_named_degrades_load_failed_to_none(caplog):
    """A loader that returns the sentinel yields None from load_named - not the sentinel itself,
    and without the generic 'loaded but no instance found' (which is for imported-but-empty)."""
    from pathlib import Path
    from plugins import plugin_loader as pl
    test_logger = logging.getLogger("test.load_named")
    with caplog.at_level(logging.WARNING):
        result = pl.load_named(
            "x", Path("/nonexistent"), lambda plugin_dir: pl.LOAD_FAILED,
            kind="Memory provider", noun="provider", logger=test_logger)
    assert result is None
    assert not any("no provider instance found" in r.getMessage() for r in caplog.records)


def test_successful_load_clears_stale_failure(home):
    """Review point 5: a long-lived process must not keep a stale reason. After a recorded
    failure, a successful load of the same provider clears it (and a later re-failure would
    record a fresh reason, since dedupe is per-reason, not per-name)."""
    if clear_load_failure is None or load_failure_reason is None:
        pytest.skip("pre-fix tree: no failure registry")
    from plugins.memory import _record_load_failure
    _write_provider(home, "demo_ok", _REGISTER_OK)
    _record_load_failure("demo_ok", "stale reason from an earlier failed attempt")
    assert load_failure_reason("demo_ok") == "stale reason from an earlier failed attempt"

    provider = load_memory_provider("demo_ok")
    assert provider is not None
    assert load_failure_reason("demo_ok") is None  # cleared by the successful load


def test_entry_point_failure_is_recorded_for_status(home, monkeypatch, caplog):
    """Review point 2: a pip-installed (entry-point) provider whose module fails to import is
    RECORDED in the per-home registry - so `hermes memory status` says 'installed, failed to
    load' instead of 'NOT installed' (requirement C held for pip installs, not just dirs)."""
    if load_failure_reason is None:
        pytest.skip("pre-fix tree: no failure registry")
    from plugins import memory as pm

    class FakeEP:
        name = "demo_pip"
        value = "demo_pip.provider:register"

        def load(self):
            raise ModuleNotFoundError("No module named 'pipdep_xyz'", name="pipdep_xyz")

    monkeypatch.setattr(pm, "find_provider_dir", lambda name: None)
    monkeypatch.setattr(pm, "find_provider_entry_point", lambda name: FakeEP())

    with caplog.at_level(logging.DEBUG):
        assert pm.load_memory_provider("demo_pip") is None

    reason = load_failure_reason("demo_pip")
    assert reason is not None
    assert "pipdep_xyz" in reason
    # The generic 'no instance found' must NOT have been logged (sentinel, not None).
    assert not any("no provider instance found" in r.getMessage() for r in caplog.records)


def test_bundled_cron_provider_failure_says_pm_install(tmp_path, monkeypatch, caplog):
    """Review point 1: a BUNDLED provider's missing dependency points at `hermes pm install`,
    never the user-plugin 'declare deps + hermes plugins install' text."""
    from plugins import cron_providers
    bundled_dir = tmp_path / "bundled_cron"
    bundled_dir.mkdir()
    monkeypatch.setattr(cron_providers, "_CRON_PLUGINS_DIR", bundled_dir)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    d = bundled_dir / "demo_cron"
    d.mkdir()
    (d / "plugin.yaml").write_text("name: demo_cron\n", encoding="utf-8")
    (d / "__init__.py").write_text(
        '"""demo cron scheduler provider (register_cron_scheduler)"""\n'
        "from definitely_missing_cron_dep import register\n",
        encoding="utf-8")

    with caplog.at_level(logging.DEBUG):
        assert cron_providers.load_cron_scheduler("demo_cron") is None

    warnings = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "definitely_missing_cron_dep" in warnings
    assert "hermes pm install" in warnings
    assert "hermes plugins install" not in warnings  # bundled: not the user-plugin text
