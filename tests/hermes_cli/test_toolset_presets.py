"""Tests for the toolset presets feature.

Covers:
- resolve_preset() unit semantics (whitelist, disabled, preload, edge cases)
- list_presets() / get_active_preset() helpers
- hermes_cli.toolset_cli command flows (use, list, show, clear)
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout, redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli.tools_config import (
    get_active_preset,
    list_presets,
    resolve_preset,
)


# ─── resolve_preset semantics ─────────────────────────────────────────────────


def test_resolve_preset_unknown_returns_empty_defaults():
    cfg = {"toolset_presets": {}}
    enabled, disabled, preload = resolve_preset("missing", cfg)
    assert enabled is None
    assert disabled == []
    assert preload == []


def test_resolve_preset_empty_config_returns_empty_defaults():
    enabled, disabled, preload = resolve_preset("any", {})
    assert enabled is None
    assert disabled == []
    assert preload == []


def test_resolve_preset_whitelist():
    cfg = {
        "toolset_presets": {
            "email": {
                "toolsets": ["messaging", "file", "memory"],
            }
        }
    }
    enabled, disabled, preload = resolve_preset("email", cfg)
    assert enabled == ["messaging", "file", "memory"]
    assert disabled == []
    assert preload == []


def test_resolve_preset_empty_toolsets_means_no_restriction():
    """toolsets: [] should resolve to None (no whitelist)."""
    cfg = {
        "toolset_presets": {
            "writing": {
                "toolsets": [],
                "disabled_toolsets": ["terminal", "browser"],
            }
        }
    }
    enabled, disabled, preload = resolve_preset("writing", cfg)
    assert enabled is None
    assert disabled == ["terminal", "browser"]


def test_resolve_preset_omitted_toolsets_means_no_restriction():
    cfg = {
        "toolset_presets": {
            "writing": {
                "disabled_toolsets": ["terminal"],
            }
        }
    }
    enabled, disabled, preload = resolve_preset("writing", cfg)
    assert enabled is None
    assert disabled == ["terminal"]


def test_resolve_preset_disabled_returned_with_whitelist():
    """disabled_toolsets is always returned, regardless of whitelist state."""
    cfg = {
        "toolset_presets": {
            "dev": {
                "toolsets": ["terminal", "file", "messaging"],
                "disabled_toolsets": ["messaging"],
            }
        }
    }
    enabled, disabled, preload = resolve_preset("dev", cfg)
    assert enabled == ["terminal", "file", "messaging"]
    assert disabled == ["messaging"]


def test_resolve_preset_preload_skills():
    cfg = {
        "toolset_presets": {
            "research": {
                "toolsets": ["web", "browser"],
                "preload_skills": ["arxiv", "bookmem"],
            }
        }
    }
    _, _, preload = resolve_preset("research", cfg)
    assert preload == ["arxiv", "bookmem"]


def test_resolve_preset_strips_whitespace_and_drops_blanks():
    cfg = {
        "toolset_presets": {
            "x": {
                "toolsets": ["  web", "terminal  ", "", "  "],
                "disabled_toolsets": ["", "browser "],
                "preload_skills": [" arxiv", ""],
            }
        }
    }
    enabled, disabled, preload = resolve_preset("x", cfg)
    assert enabled == ["web", "terminal"]
    assert disabled == ["browser"]
    assert preload == ["arxiv"]


def test_resolve_preset_non_dict_value_treated_as_unknown():
    cfg = {"toolset_presets": {"bad": "not a dict"}}
    enabled, disabled, preload = resolve_preset("bad", cfg)
    assert enabled is None
    assert disabled == []
    assert preload == []


# ─── helpers ──────────────────────────────────────────────────────────────────


def test_list_presets_empty():
    assert list_presets({}) == {}
    assert list_presets({"toolset_presets": None}) == {}


def test_list_presets_returns_configured():
    cfg = {
        "toolset_presets": {
            "a": {"toolsets": ["web"]},
            "b": {"toolsets": ["terminal"]},
        }
    }
    presets = list_presets(cfg)
    assert set(presets.keys()) == {"a", "b"}


def test_get_active_preset_default_empty():
    assert get_active_preset({}) == ""
    assert get_active_preset({"active_preset": None}) == ""
    assert get_active_preset({"active_preset": "  "}) == ""


def test_get_active_preset_returns_name():
    assert get_active_preset({"active_preset": "email"}) == "email"


# ─── hermes toolset subcommand ────────────────────────────────────────────────


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point HERMES_HOME at a tmp dir so save_config doesn't touch the real config."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Clear cached config so the new HERMES_HOME takes effect.
    from hermes_cli import config as _cfg_mod
    _cfg_mod._LOAD_CONFIG_CACHE.clear()
    _cfg_mod._RAW_CONFIG_CACHE.clear()
    # Force hermes_constants to recompute the home path.
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_HERMES_HOME_CACHE", None, raising=False)
    yield tmp_path
    _cfg_mod._LOAD_CONFIG_CACHE.clear()
    _cfg_mod._RAW_CONFIG_CACHE.clear()


def _seed_presets(tmp_path):
    """Write a config.yaml with two presets to the isolated home."""
    import yaml
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "toolset_presets": {
                    "email": {
                        "description": "Inbox triage",
                        "toolsets": ["messaging", "file"],
                        "preload_skills": ["himalaya"],
                    },
                    "writing": {
                        "toolsets": [],
                        "disabled_toolsets": ["terminal", "browser"],
                    },
                },
                "active_preset": "",
            }
        )
    )


def test_toolset_cli_list_shows_presets(isolated_config, capsys):
    _seed_presets(isolated_config)
    from hermes_cli.toolset_cli import toolset_command

    toolset_command(SimpleNamespace(toolset_action="list"))
    out = capsys.readouterr().out
    assert "Active preset: (none" in out
    assert "email" in out
    assert "writing" in out
    assert "Inbox triage" in out


def test_toolset_cli_use_sets_active_preset(isolated_config, capsys):
    _seed_presets(isolated_config)
    from hermes_cli.config import load_config
    from hermes_cli.toolset_cli import toolset_command

    toolset_command(SimpleNamespace(toolset_action="use", name="email"))
    out = capsys.readouterr().out
    assert "'email' is now active" in out

    cfg = load_config()
    assert cfg.get("active_preset") == "email"


def test_toolset_cli_use_unknown_exits_nonzero(isolated_config, capsys):
    _seed_presets(isolated_config)
    from hermes_cli.toolset_cli import toolset_command

    with pytest.raises(SystemExit) as exc:
        toolset_command(SimpleNamespace(toolset_action="use", name="nope"))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Unknown preset" in err


def test_toolset_cli_show_renders_details(isolated_config, capsys):
    _seed_presets(isolated_config)
    from hermes_cli.toolset_cli import toolset_command

    toolset_command(SimpleNamespace(toolset_action="show", name="email"))
    out = capsys.readouterr().out
    assert "Preset: email" in out
    assert "Inbox triage" in out
    assert "messaging" in out
    assert "himalaya" in out


def test_toolset_cli_clear_resets_active(isolated_config, capsys):
    _seed_presets(isolated_config)
    from hermes_cli.config import load_config, save_config
    from hermes_cli.toolset_cli import toolset_command

    cfg = load_config()
    cfg["active_preset"] = "email"
    save_config(cfg)

    toolset_command(SimpleNamespace(toolset_action="clear"))
    out = capsys.readouterr().out
    assert "cleared" in out.lower()

    cfg = load_config()
    assert cfg.get("active_preset", "") == ""


def test_toolset_cli_clear_when_none_active_is_noop(isolated_config, capsys):
    _seed_presets(isolated_config)
    from hermes_cli.toolset_cli import toolset_command

    toolset_command(SimpleNamespace(toolset_action="clear"))
    out = capsys.readouterr().out
    assert "No preset active" in out


# ─── Bundle integration ──────────────────────────────────────────────────────
# Presets layer on top of upstream's skill-bundles feature (PR #28373):
# a preset names a bundle via ``bundle:`` and inherits its skill list.
# ``preload_skills:`` is preserved as an extras list - merged on top of
# the bundle's skills, deduped first-seen. Tool-surface gating
# (``toolsets`` / ``disabled_toolsets``) remains preset-only.


def _bundle_dir(tmp_path):
    """Write skill bundles to the isolated HERMES_HOME so get_bundle finds them."""
    d = tmp_path / "skill-bundles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed_bundles_and_presets(tmp_path, *, with_instruction=False):
    """Write a config.yaml referencing a bundle, and the bundle file itself."""
    import yaml

    bundle_yaml = {
        "name": "email-stack",
        "description": "Skills for email triage.",
        "skills": ["himalaya", "inbox-summariser"],
    }
    if with_instruction:
        bundle_yaml["instruction"] = "Triage email systematically.\nPrioritise unread."
    (_bundle_dir(tmp_path) / "email-stack.yaml").write_text(yaml.safe_dump(bundle_yaml))

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "toolset_presets": {
                    "email": {
                        "bundle": "email-stack",
                        "toolsets": ["messaging", "file"],
                    },
                    "email-plus-extras": {
                        "bundle": "email-stack",
                        "preload_skills": ["calendar-helper"],
                    },
                    "extras-only": {
                        "preload_skills": ["himalaya"],
                    },
                    "missing-bundle": {
                        "bundle": "does-not-exist",
                        "preload_skills": ["fallback-skill"],
                    },
                    "overlap": {
                        "bundle": "email-stack",
                        # himalaya is in the bundle too; extras must dedup.
                        "preload_skills": ["himalaya", "calendar-helper"],
                    },
                },
                "active_preset": "",
            }
        ),
        encoding="utf-8",
    )


def _clear_bundle_cache():
    """Reset the bundles mtime cache so each test reads its tmp files fresh."""
    from agent import skill_bundles as _sb
    _sb._bundles_cache.clear()
    _sb._bundles_cache_mtime = 0.0


def test_resolve_preset_pulls_skills_from_bundle(isolated_config):
    """A ``bundle:`` reference expands to the bundle's skills list."""
    _seed_bundles_and_presets(isolated_config)
    _clear_bundle_cache()
    from hermes_cli.config import load_config

    enabled, disabled, skills = resolve_preset("email", load_config())
    # Tool-surface gating from the preset (unchanged by bundle integration).
    assert enabled == ["messaging", "file"]
    assert disabled == []
    # Skills come from the bundle.
    assert skills == ["himalaya", "inbox-summariser"]


def test_resolve_preset_merges_bundle_skills_with_extras(isolated_config):
    """Bundle skills come first; extras are appended after, deduped."""
    _seed_bundles_and_presets(isolated_config)
    _clear_bundle_cache()
    from hermes_cli.config import load_config

    _, _, skills = resolve_preset("email-plus-extras", load_config())
    assert skills == ["himalaya", "inbox-summariser", "calendar-helper"]


def test_resolve_preset_dedupes_overlap_between_bundle_and_extras(isolated_config):
    """A skill listed in both the bundle and extras appears once (bundle wins position)."""
    _seed_bundles_and_presets(isolated_config)
    _clear_bundle_cache()
    from hermes_cli.config import load_config

    _, _, skills = resolve_preset("overlap", load_config())
    # himalaya is in both - must appear exactly once, in its bundle position.
    assert skills == ["himalaya", "inbox-summariser", "calendar-helper"]


def test_resolve_preset_without_bundle_uses_extras_only(isolated_config):
    """Presets that only set ``preload_skills:`` still work (no bundle layer)."""
    _seed_bundles_and_presets(isolated_config)
    _clear_bundle_cache()
    from hermes_cli.config import load_config

    enabled, disabled, skills = resolve_preset("extras-only", load_config())
    assert enabled is None
    assert disabled == []
    assert skills == ["himalaya"]


def test_resolve_preset_missing_bundle_warns_and_falls_back_to_extras(isolated_config, caplog):
    """An unknown bundle must NOT fail the preset - log a warning, keep extras + tool gating."""
    import logging
    _seed_bundles_and_presets(isolated_config)
    _clear_bundle_cache()
    from hermes_cli.config import load_config

    with caplog.at_level(logging.WARNING):
        enabled, disabled, skills = resolve_preset("missing-bundle", load_config())

    # The preset's extras and (absent) tool gating still apply.
    assert skills == ["fallback-skill"]
    # And a warning was emitted naming both the preset and the bundle so
    # operators can act on it.
    msgs = " ".join(r.message for r in caplog.records)
    assert "missing-bundle" in msgs
    assert "does-not-exist" in msgs


def test_preset_bundle_instruction_returns_bundle_text(isolated_config):
    """The instruction helper surfaces the bundle's ``instruction:`` field."""
    _seed_bundles_and_presets(isolated_config, with_instruction=True)
    _clear_bundle_cache()
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import preset_bundle_instruction

    text = preset_bundle_instruction("email", load_config())
    assert "Triage email systematically." in text
    assert "Prioritise unread." in text


def test_preset_bundle_instruction_empty_when_preset_has_no_bundle(isolated_config):
    """No ``bundle:`` field → empty instruction (not None, not exception)."""
    _seed_bundles_and_presets(isolated_config)
    _clear_bundle_cache()
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import preset_bundle_instruction

    assert preset_bundle_instruction("extras-only", load_config()) == ""


def test_preset_bundle_instruction_empty_when_bundle_missing(isolated_config, caplog):
    """Reference to nonexistent bundle returns "" (and resolve_preset has already
    logged the warning - instruction helper stays quiet)."""
    _seed_bundles_and_presets(isolated_config)
    _clear_bundle_cache()
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import preset_bundle_instruction

    assert preset_bundle_instruction("missing-bundle", load_config()) == ""


def test_toolset_cli_show_lists_bundle_extras_and_effective_skills(isolated_config, capsys):
    """``hermes toolset show <name>`` must surface Bundle + Extras + Effective
    skills separately so users understand the composition at a glance."""
    _seed_bundles_and_presets(isolated_config, with_instruction=True)
    _clear_bundle_cache()
    from hermes_cli.toolset_cli import toolset_command

    toolset_command(SimpleNamespace(toolset_action="show", name="email-plus-extras"))
    out = capsys.readouterr().out

    assert "Bundle: email-stack" in out
    assert "Extra skills: calendar-helper" in out
    # Effective = bundle + extras
    assert "Effective skills: himalaya, inbox-summariser, calendar-helper" in out
    # Bundle's instruction first line is previewed
    assert "Bundle instruction: Triage email systematically." in out


def test_toolset_cli_show_reports_no_bundle_when_unset(isolated_config, capsys):
    """Extras-only presets render with ``Bundle: (none)``."""
    _seed_bundles_and_presets(isolated_config)
    _clear_bundle_cache()
    from hermes_cli.toolset_cli import toolset_command

    toolset_command(SimpleNamespace(toolset_action="show", name="extras-only"))
    out = capsys.readouterr().out

    assert "Bundle: (none)" in out
    assert "Effective skills: himalaya" in out
