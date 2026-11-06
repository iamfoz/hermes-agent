"""`hermes memory status` must tell "installed, failed to load" apart from "NOT installed".

Regression coverage for the pair: a provider directory that exists but whose module raises
used to show "Plugin: NOT installed ✗" plus an install hint for a directory that was
installed, and the provider was dropped from "Installed plugins". Now the status names the
reason (same text the load path logged) and keeps the provider listed with a failed marker.
"""

from types import SimpleNamespace

from hermes_cli import memory_setup

_FAILURE_REASON = (
    "ModuleNotFoundError: No module named 'definitely_missing_pkg'. The plugin's Python "
    "dependencies are not in Hermes' environment (module 'definitely_missing_pkg' is missing)."
)


def test_status_installed_but_failed_to_load(monkeypatch, capsys):
    """Installed directory + failed module: "installed, failed to load ✗" with the reason,
    NO install hint, and the provider stays in "Installed plugins" with a failed marker."""
    monkeypatch.setattr(
        memory_setup, "_get_available_providers",
        lambda: [("demo_mem", "failed to load", None)],
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"memory": {"provider": "demo_mem"}},
    )
    import plugins.memory as _pm
    # raising=False: a pre-fix tree has no load_failure_reason, the patch is a no-op and the
    # test goes red on the assertion (status still says "NOT installed") rather than in setup.
    monkeypatch.setattr(
        _pm, "load_failure_reason",
        lambda name: _FAILURE_REASON if name == "demo_mem" else None,
        raising=False,
    )

    memory_setup.cmd_status(SimpleNamespace())
    out = capsys.readouterr().out

    assert "installed, failed to load" in out
    assert "NOT installed" not in out
    # The same reason the load path logged, so the user can act on it.
    assert "ModuleNotFoundError" in out
    assert "definitely_missing_pkg" in out
    # No install hint for a directory that is installed.
    assert "Install the 'demo_mem' memory plugin" not in out
    # Kept in the list with a failed marker (and the active flag), not dropped.
    assert "• demo_mem  (installed, failed to load ✗) ← active" in out


def test_status_truly_not_installed_keeps_install_hint(monkeypatch, capsys):
    """No directory at all: the old "NOT installed ✗" + install hint stays as-is (guard)."""
    monkeypatch.setattr(memory_setup, "_get_available_providers", list)  # no providers discovered
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"memory": {"provider": "ghostmem"}},
    )
    import plugins.memory as _pm
    monkeypatch.setattr(_pm, "load_failure_reason", lambda name: None, raising=False)

    memory_setup.cmd_status(SimpleNamespace())
    out = capsys.readouterr().out

    assert "NOT installed" in out
    assert "Install the 'ghostmem' memory plugin to ~/.hermes/plugins/" in out
    # A failed-load marker must not appear for a provider that was never installed.
    assert "installed, failed to load" not in out


def test_status_pip_entry_point_provider_failed_to_load(tmp_path, monkeypatch, capsys):
    """Review point 2, end-to-end: a PIP (entry-point) provider whose module fails to import is
    RECORDED by the real load path, so `hermes memory status` says "installed, failed to load"
    (requirement C held for pip installs, not just directory installs) - no patched registry."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    import plugins.memory as _pm

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    token = set_hermes_home_override(tmp_path)

    class FakeEP:
        name = "demo_pip"
        value = "demo_pip.provider:register"

        def load(self):
            raise ModuleNotFoundError("No module named 'pipdep_xyz'", name="pipdep_xyz")

    # Entry-point-only provider: no directory, one failing entry point.
    monkeypatch.setattr(_pm, "find_provider_dir", lambda name: None)
    monkeypatch.setattr(_pm, "find_provider_entry_point", lambda name: FakeEP())
    monkeypatch.setattr(
        _pm, "discover_memory_providers",
        lambda: [("demo_pip", "a pip provider", False)],
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"memory": {"provider": "demo_pip"}},
    )
    try:
        memory_setup.cmd_status(SimpleNamespace())
    finally:
        reset_hermes_home_override(token)
    out = capsys.readouterr().out

    assert "installed, failed to load" in out
    assert "NOT installed" not in out
    # The recorded reason (via the REAL per-home registry, not a patch) is shown.
    assert "pipdep_xyz" in out
    assert "Install the 'demo_pip' memory plugin" not in out
    assert "• demo_pip  (installed, failed to load ✗) ← active" in out


def test_setup_provider_failed_to_load_says_so(tmp_path, monkeypatch, capsys):
    """Review point 4: setting up a failed-to-load provider must not end in a plain
    "Activation saved" - it prints the recorded reason and points at a restart + status."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    import plugins.memory as _pm

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    token = set_hermes_home_override(tmp_path)
    try:
        # The real registry carries the reason (as a real failed load would).
        _pm._record_load_failure(
            "demo_mem",
            "ModuleNotFoundError: No module named 'demo_dep'. The plugin's Python dependencies "
            "are not in Hermes' environment (module 'demo_dep' is missing).",
        )
        monkeypatch.setattr(
            memory_setup, "_get_available_providers",
            lambda: [("demo_mem", "failed to load", None)],
        )
        monkeypatch.setattr(memory_setup, "_install_dependencies", lambda name: None)
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: {"memory": {}})
        monkeypatch.setattr("hermes_cli.config.save_config", lambda config: None)

        memory_setup.cmd_setup_provider("demo_mem")
    finally:
        reset_hermes_home_override(token)
    out = capsys.readouterr().out

    # The plain "Activation saved" block is REPLACED by the notice, not preceded by it.
    assert "Activation saved to config.yaml" in out
    assert out.index("Activation saved to config.yaml") > out.index("Memory provider: demo_mem")
    assert out.count("Activation saved to config.yaml") == 1
    assert "demo_mem could not be loaded when checked" in out
    assert "demo_dep" in out
    assert "hermes memory status" in out


def test_status_dir_present_but_nothing_registered(tmp_path, monkeypatch, capsys):
    """Review point 1: directory exists but no failure reason is recorded (register() ran and
    registered nothing - the generic 'loaded but no provider instance found' path, which records
    nothing). Status must still say "installed, failed to load ✗" (with a pointer to the log),
    never "NOT installed" + install hint."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    import plugins.memory as _pm

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    token = set_hermes_home_override(tmp_path)
    try:
        (tmp_path / "plugins" / "demo_empty").mkdir(parents=True)
        monkeypatch.setattr(
            _pm, "find_provider_dir",
            lambda name: tmp_path / "plugins" / "demo_empty" if name == "demo_empty" else None,
        )
        monkeypatch.setattr(_pm, "load_failure_reason", lambda name: None)
        monkeypatch.setattr(
            memory_setup, "_get_available_providers",
            lambda: [("demo_empty", "local", None)],
        )
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"memory": {"provider": "demo_empty"}},
        )

        memory_setup.cmd_status(SimpleNamespace())
    finally:
        reset_hermes_home_override(token)
    out = capsys.readouterr().out

    assert "installed, failed to load" in out
    assert "no provider registered (see the log)" in out
    assert "NOT installed" not in out
    assert "Install the 'demo_empty' memory plugin" not in out
    # The list badge agrees with the detail block even without a recorded reason.
    assert "• demo_empty  (installed, failed to load ✗) ← active" in out
