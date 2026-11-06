"""``--toolset NAME``: parsed at the top level and under ``chat``, folded into the launch flags."""

import pytest

from hermes_cli._parser import build_top_level_parser, top_level_value_flag_sets
from hermes_cli.toolset_cli import apply_toolset_flag

CFG = {"toolset_presets": {
    "research": {"toolsets": ["web", "browser", "file"], "disabled_toolsets": ["browser"],
                 "preload_skills": ["arxiv"]},
    "writing": {"disabled_toolsets": ["terminal"]},
}}


@pytest.mark.parametrize("argv", [["--toolset", "research", "chat"], ["chat", "--toolset", "research"]])
def test_toolset_flag_parses_before_and_after_chat(argv):
    args = build_top_level_parser()[0].parse_args(argv)
    assert args.toolset == "research"


def test_toolset_flag_takes_a_value_for_the_argv_scanners():
    assert "--toolset" in top_level_value_flag_sets()[0]


def test_preset_whitelist_becomes_the_pick_minus_its_disabled_and_adds_skills():
    assert apply_toolset_flag("research", CFG, None, ["github-auth"]) == (["web", "file"], ["github-auth", "arxiv"])


def test_typed_toolsets_win_over_the_whitelist_but_lose_disabled_names():
    assert apply_toolset_flag("research", CFG, "terminal,browser", None) == (["terminal"], ["arxiv"])


def test_preset_without_whitelist_leaves_default_resolution():
    assert apply_toolset_flag("writing", CFG, None, None) == (None, None)


def test_unknown_or_absent_preset_leaves_launch_unchanged(capsys):
    assert apply_toolset_flag("", CFG, "web", "s") == ("web", "s")
    assert apply_toolset_flag("nope", CFG, "web", "s") == ("web", "s")
    assert "preset 'nope' not found" in capsys.readouterr().err
