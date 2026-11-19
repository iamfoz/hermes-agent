"""Behavior contract: the session context exposes the authenticated sender id.

Display names are untrusted metadata; the numeric sender id is what the gateway
authenticated against its allowlist. Agent-side flows (approval grants, identity
checks) need that id even when a display name is present. Before this change the
id was only rendered as a fallback when no name existed, so a named sender's
authenticated identity was invisible to the agent.

These tests use only symbols that exist on base, so they collect on both trees;
the behavior assertions are red on base and green with the fix.
"""

from gateway.config import Platform
from gateway.session import (
    SessionContext,
    SessionSource,
    build_session_context_prompt,
)


def _ctx(
    user_id: str | None = "6088607142",
    user_name: str | None = "Martyn Forryan",
    chat_id: str = "6088607142",
    chat_type: str = "dm",
    platform: Platform = Platform.TELEGRAM,
    shared: bool = False,
):
    source = SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        user_name=user_name,
    )
    return SessionContext(
        source=source,
        connected_platforms=[platform],
        home_channels={},
        shared_multi_user_session=shared,
    )


def _identity_lines(prompt):
    return [
        l
        for l in prompt.splitlines()
        if l.startswith("**User:") or l.startswith("**User ID:")
    ]


class TestSenderIdExposure:
    def test_named_user_exposes_sender_id(self):
        prompt = build_session_context_prompt(_ctx())
        lines = _identity_lines(prompt)
        assert '**User:** "Martyn Forryan"' in lines
        assert '**User ID:** "6088607142"' in lines

    def test_sender_id_hashed_under_redaction(self):
        # Telegram is PII-safe: redaction hashes ids, never drops the identity line.
        prompt = build_session_context_prompt(_ctx(), redact_pii=True)
        lines = _identity_lines(prompt)
        assert '**User:** "Martyn Forryan"' in lines
        assert any(l.startswith("**User ID:") for l in lines)
        assert "6088607142" not in prompt  # raw id nowhere, incl. delivery labels

    def test_unnamed_user_fallback_unchanged(self):
        prompt = build_session_context_prompt(_ctx(user_name=None))
        lines = _identity_lines(prompt)
        assert lines == ['**User ID:** "6088607142"']

    def test_no_identity_when_neither_present(self):
        prompt = build_session_context_prompt(_ctx(user_id=None, user_name=None))
        assert _identity_lines(prompt) == []

    def test_multi_user_session_pins_no_identity(self):
        # Shared sessions prefix sender names per message; the system prompt
        # must not pin one user (cache stability). Unchanged by this fix.
        prompt = build_session_context_prompt(_ctx(shared=True))
        assert _identity_lines(prompt) == []

    def test_group_sender_id_is_not_chat_id(self):
        prompt = build_session_context_prompt(
            _ctx(user_id="111", chat_id="-100999", chat_type="group")
        )
        assert '**User ID:** "111"' in prompt
        assert '**User ID:** "-100999"' not in prompt

    def test_prompt_stable_for_cache(self):
        a = build_session_context_prompt(_ctx())
        b = build_session_context_prompt(_ctx())
        assert a == b
