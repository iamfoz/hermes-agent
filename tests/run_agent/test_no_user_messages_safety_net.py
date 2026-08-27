"""Regression test for the no-user-message safety net.

If context compression or session resume strips every user-role
message from the request, OpenAI-compatible providers (Airrouter /
litellm and others) reject the call with 400 "No user query found in
messages", which the jmunch gateway surfaces as a 502. The agent
detects a user-less ``api_messages`` just before the request and
injects a continuation prompt so the turn can proceed. See
agent/conversation_loop.py (run_conversation).

The strip happens upstream in the message pipeline; this test
simulates it by making ``_drop_thinking_only_and_merge_users`` (the
last transform before the guard) remove user-role messages, which is
exactly the post-compression / post-resume shape the feature guards
against.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _resp_text(content):
    msg = SimpleNamespace(content=content, reasoning=None, reasoning_content=None,
                          tool_calls=[])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=None,
        model="test/model",
    )


class _ScriptedCompletions:
    def __init__(self, response):
        self._response = response
        self.seen_messages = []

    def create(self, **kwargs):
        self.seen_messages.append(list(kwargs.get("messages") or []))
        return self._response


class _Client:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


def _make_agent(monkeypatch):
    from run_agent import AIAgent

    comp = _ScriptedCompletions(_resp_text("ok"))
    monkeypatch.setattr("run_agent.OpenAI", lambda **kwargs: _Client(comp))
    monkeypatch.setattr(
        "run_agent.get_tool_definitions", lambda *a, **k: [{"function": {"name": "web_search"}}]
    )
    agent = AIAgent(
        model="test-model",
        api_key="test-key",
        base_url="http://localhost:8080/v1",
        platform="cli",
        max_iterations=3,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )
    agent._disable_streaming = True
    agent.tool_delay = 0
    return agent, comp


_CONTINUATION_MARKER = "please continue the conversation from your last response"


def test_injects_continuation_when_user_messages_stripped(monkeypatch):
    """When the pipeline yields a user-less api_messages, a continuation
    user prompt must be injected before the request is sent."""
    agent, comp = _make_agent(monkeypatch)

    # Simulate compression/resume stripping every user-role message right
    # before the guard runs.
    def _strip_users(messages):
        return [m for m in messages if m.get("role") != "user"]

    monkeypatch.setattr(agent, "_drop_thinking_only_and_merge_users", _strip_users)

    agent.run_conversation("hello")

    sent = comp.seen_messages[0]
    user_msgs = [m for m in sent if m.get("role") == "user"]
    # Exactly the injected continuation prompt, since the original user turn
    # was stripped by the simulated pipeline.
    assert len(user_msgs) == 1, f"expected one injected user msg, got {user_msgs}"
    assert _CONTINUATION_MARKER in user_msgs[0]["content"]


def test_no_injection_when_user_message_present(monkeypatch):
    """Normal case: a user message is present, so the safety net stays
    out of the way and injects nothing."""
    agent, comp = _make_agent(monkeypatch)
    # Leave the pipeline transform as the real (identity-ish) implementation.
    agent.run_conversation("hello there")

    sent = comp.seen_messages[0]
    contents = " ".join(
        (m.get("content") or "") for m in sent if m.get("role") == "user"
    )
    # The real user turn survives and no continuation marker is injected.
    assert "hello there" in contents
    assert _CONTINUATION_MARKER not in contents
