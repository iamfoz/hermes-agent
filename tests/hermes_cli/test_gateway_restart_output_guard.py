"""``hermes gateway restart`` must always leave a visible confirmation on the terminal.

Driven through the real subcommand dispatch table so the guard is proven to be what runs for
``restart``; ``_cmd_restart`` is replaced to pin each outcome.
"""
from types import SimpleNamespace

import pytest

from hermes_cli import gateway as gw


def _restart():
    gw._gateway_command_inner(SimpleNamespace(gateway_command="restart"))


class TestRestartOutputGuard:
    def test_silent_dispatch_falls_back_to_service_restarted(self, monkeypatch, capsys):
        monkeypatch.setattr(gw, "_cmd_restart", lambda args: None)
        _restart()
        assert capsys.readouterr().out == "✓ Service restarted\n"

    def test_dispatch_output_passes_through_without_fallback(self, monkeypatch, capsys):
        monkeypatch.setattr(gw, "_cmd_restart", lambda args: print("✓ User service restarted (PID 12345)"))
        _restart()
        assert capsys.readouterr().out == "✓ User service restarted (PID 12345)\n"

    def test_whitespace_only_output_still_triggers_fallback(self, monkeypatch, capsys):
        def fake(_args):
            print()
            print("   ")

        monkeypatch.setattr(gw, "_cmd_restart", fake)
        _restart()
        assert "✓ Service restarted" in capsys.readouterr().out

    def test_dispatch_exception_propagates_and_skips_fallback(self, monkeypatch, capsys):
        class _BoomError(RuntimeError):
            pass

        def fake(_args):
            raise _BoomError("upstream is wedged")

        monkeypatch.setattr(gw, "_cmd_restart", fake)
        with pytest.raises(_BoomError, match="upstream is wedged"):
            _restart()
        assert "✓ Service restarted" not in capsys.readouterr().out

    def test_dispatch_sys_exit_propagates(self, monkeypatch, capsys):
        def fake(_args):
            print("✗ Gateway service restart failed.")
            raise SystemExit(1)

        monkeypatch.setattr(gw, "_cmd_restart", fake)
        with pytest.raises(SystemExit) as excinfo:
            _restart()
        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "✗ Gateway service restart failed." in out
        assert "✓ Service restarted" not in out
