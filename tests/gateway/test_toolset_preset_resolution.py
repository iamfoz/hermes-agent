"""Toolset presets on the gateway: the sticky ``active_preset`` shapes every agent build.

Messaging and background turns go through ``_resolve_turn_toolsets``; CLI / TUI / Desktop sessions
the gateway owns decide their toolsets at creation in ``session_policy._resolve_toolsets``. Both must
apply the preset whitelist and merge its ``disabled_toolsets``; ``/toolset`` flips the sticky key.
"""

import asyncio

import pytest

from gateway.run import GatewayRunner
from gateway.session_policy import _resolve_toolsets

PRESETS = {
    "email": {"toolsets": ["messaging", "file"], "disabled_toolsets": ["browser"]},
    "writing": {"disabled_toolsets": ["terminal"]},
}


def _runner():
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda source: None
    return runner


def _cfg(active, **extra):
    return {"toolset_presets": PRESETS, "active_preset": active,
            "platform_toolsets": {"telegram": ["web", "terminal"]}, **extra}


def test_turn_toolsets_take_preset_whitelist_and_merge_disabled():
    cfg = _cfg("email", agent={"disabled_toolsets": ["cronjob"]})
    enabled, disabled = _runner()._resolve_turn_toolsets(cfg, object(), "telegram")
    assert enabled == ["messaging", "file"]
    assert disabled == ["cronjob", "browser"]


def test_turn_toolsets_preset_without_whitelist_keeps_platform_surface():
    base, _ = _runner()._resolve_turn_toolsets(_cfg(""), object(), "telegram")
    enabled, disabled = _runner()._resolve_turn_toolsets(_cfg("writing"), object(), "telegram")
    assert enabled == base
    assert disabled == ["terminal"]


@pytest.mark.parametrize("active", ["", "missing"])
def test_turn_toolsets_untouched_without_a_known_active_preset(active):
    plain = _runner()._resolve_turn_toolsets(_cfg(""), object(), "telegram")
    assert _runner()._resolve_turn_toolsets(_cfg(active), object(), "telegram") == plain
    assert plain[1] is None


def test_local_session_creation_uses_preset_whitelist_over_coding_posture(monkeypatch):
    from agent import coding_context
    monkeypatch.setattr(coding_context, "focus_toolsets", lambda config: pytest.fail("posture consulted"))
    assert _resolve_toolsets({}, _cfg("email"), "cli", False, coding_posture=True) == {"messaging", "file"}


class _Event:
    def __init__(self, args):
        self._args = args

    def get_command_args(self):
        return self._args


def _command_runner(monkeypatch, cfg):
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda *a, **k: cfg)
    runner = object.__new__(GatewayRunner)
    runner.saved, runner.resets = [], 0

    def _save(key, value):
        runner.saved.append((key, value))
        return True

    async def _reset(event):
        runner.resets += 1

    runner._save_gateway_config_key = _save
    runner._handle_reset_command = _reset
    return runner


def test_toolset_command_activates_preset_and_rotates_session(monkeypatch):
    runner = _command_runner(monkeypatch, _cfg(""))
    reply = asyncio.run(runner._handle_toolset_command(_Event("email")))
    assert runner.saved == [("active_preset", "email")]
    assert runner.resets == 1
    assert "activated" in reply and "Toolsets: messaging, file" in reply


def test_toolset_command_no_new_defers_the_reset(monkeypatch):
    runner = _command_runner(monkeypatch, _cfg(""))
    reply = asyncio.run(runner._handle_toolset_command(_Event("writing --no-new")))
    assert runner.saved == [("active_preset", "writing")] and runner.resets == 0
    assert "Run /new" in reply


def test_toolset_command_unknown_name_changes_nothing(monkeypatch):
    runner = _command_runner(monkeypatch, _cfg(""))
    reply = asyncio.run(runner._handle_toolset_command(_Event("nope")))
    assert runner.saved == [] and runner.resets == 0
    assert "Unknown preset `nope`" in reply


def test_toolset_command_clear_resets_sticky_preset(monkeypatch):
    runner = _command_runner(monkeypatch, _cfg("email"))
    reply = asyncio.run(runner._handle_toolset_command(_Event("clear")))
    assert runner.saved == [("active_preset", "")] and runner.resets == 1
    assert "cleared" in reply


def test_toolset_is_a_mid_run_command():
    from hermes_cli.commands import resolve_command
    cmd = resolve_command("preset")
    assert cmd.name == "toolset" and cmd.busy_policy == "interrupt_then_dispatch"
    kind, handler = object.__new__(GatewayRunner)._busy_slash_route(cmd)
    assert kind == "plain" and handler.__name__ == "_handle_toolset_command"
