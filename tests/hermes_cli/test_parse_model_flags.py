"""Tests for ``parse_model_flags`` - the /model command argument parser.

Particularly important for multi-word provider names (e.g. saved
``custom_providers:`` entries with spaces in their ``name:`` field
like "airouter JMunch" or "deepseek JMunch"). Telegram, Discord, and
the CLI all feed the raw text of the /model arguments into this
parser; none of them apply shell-style quote stripping first.
"""

from __future__ import annotations

import pytest

from hermes_cli.model_switch import parse_model_flags


@pytest.mark.parametrize(
    "raw,expected",
    [
        # ── Baseline single-word cases ─────────────────────────────
        ("sonnet", ("sonnet", "", False)),
        ("sonnet --global", ("sonnet", "", True)),
        ("sonnet --provider anthropic", ("sonnet", "anthropic", False)),
        ("--provider my-ollama", ("", "my-ollama", False)),
        (
            "sonnet --provider anthropic --global",
            ("sonnet", "anthropic", True),
        ),
        ("", ("", "", False)),
        # Multi-word provider names - THE regression case
        (
            "Qwen3.6 --provider airouter JMunch",
            ("Qwen3.6", "airouter JMunch", False),
        ),
        (
            "Qwen3.6 --provider airouter JMunch --global",
            ("Qwen3.6", "airouter JMunch", True),
        ),
        (
            "--provider deepseek JMunch",
            ("", "deepseek JMunch", False),
        ),
        (
            "deepseek-v4-pro --provider deepseek JMunch",
            ("deepseek-v4-pro", "deepseek JMunch", False),
        ),
        (
            "--provider airouter.ch Direct --global",
            ("", "airouter.ch Direct", True),
        ),
        # ── Defensive quote stripping ──────────────────────────────
        # Users may type quotes out of shell-habit; Telegram/Discord
        # pass them through verbatim, so the parser strips a single
        # matched pair so the value matches the saved entry name.
        (
            'Qwen3.6 --provider "airouter JMunch"',
            ("Qwen3.6", "airouter JMunch", False),
        ),
        (
            "qwen --provider 'airouter JMunch'",
            ("qwen", "airouter JMunch", False),
        ),
        # ── --global appearing before --provider ───────────────────
        (
            "Qwen3.6 --global --provider airouter JMunch",
            ("Qwen3.6", "airouter JMunch", True),
        ),
        # ── Extra whitespace collapses ─────────────────────────────
        (
            "Qwen3.6  --provider   airouter   JMunch",
            ("Qwen3.6", "airouter JMunch", False),
        ),
        # ── Unicode dashes (Telegram/iOS auto-replacement) ─────────
        (
            "sonnet —provider anthropic",
            ("sonnet", "anthropic", False),
        ),
        (
            "sonnet –provider anthropic –global",
            ("sonnet", "anthropic", True),
        ),
    ],
)
def test_parse_model_flags(raw, expected):
    # parse_model_flags returns (model, provider, is_global, force_refresh,
    # is_session); these cases pin the first three fields.
    assert parse_model_flags(raw)[:3] == expected


def test_provider_name_with_no_value_after_flag_is_empty():
    """``--provider`` with nothing after it shouldn't crash or eat the
    sentinel; it just yields empty provider + empty model."""
    assert parse_model_flags("--provider")[:3] == ("", "", False)


def test_unmatched_quote_is_left_intact():
    """A lone quote (no matched pair) shouldn't get silently stripped;
    that would mask a typo. The provider name keeps the quote so the
    downstream "unknown provider" error surfaces the real input."""
    raw = 'qwen --provider "airouter JMunch'
    provider = parse_model_flags(raw)[1]
    assert provider.startswith('"')


def test_unicode_dash_does_not_swallow_provider_value():
    """Regression for Telegram auto-converting a double hyphen to a single
    Unicode dash before the flag name: the value following should still be
    captured greedily."""
    out = parse_model_flags("Qwen3.6 —provider airouter JMunch")
    assert out[:3] == ("Qwen3.6", "airouter JMunch", False)
