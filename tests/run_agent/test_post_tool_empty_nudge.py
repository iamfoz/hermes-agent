"""Tests for the multi-attempt empty-response nudge after tool calls.

Some weaker models (mimo-v2-pro, GLM-5, ...) return an empty response
after a tool result instead of continuing. Rather than giving up, the
agent nudges the model to continue - up to two attempts. The second
(strong) nudge reinjects truncated tool-result summaries so the model
has something concrete to work with. See agent/conversation_loop.py
(``_post_tool_empty_retries``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def _tool_call(name, arguments, call_id="call_1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _resp_tool(name="web_search", arguments='{"query": "x"}'):
    msg = SimpleNamespace(content=None, reasoning=None, reasoning_content=None,
                          tool_calls=[_tool_call(name, arguments)])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="tool_calls")],
        usage=None,
        model="test/model",
    )


def _resp_text(content):
    msg = SimpleNamespace(content=content, reasoning=None, reasoning_content=None,
                          tool_calls=[])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=None,
        model="test/model",
    )


class _ScriptedCompletions:
    """Returns a pre-scripted sequence of responses, one per create() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.seen_messages = []  # snapshot of messages passed on each call

    def create(self, **kwargs):
        # Record the message list the agent sent so the test can inspect
        # the nudges that were injected before each turn.
        self.seen_messages.append(list(kwargs.get("messages") or []))
        i = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[i]


class _Client:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


def _make_agent(monkeypatch, responses):
    from run_agent import AIAgent

    comp = _ScriptedCompletions(responses)
    monkeypatch.setattr("run_agent.OpenAI", lambda **kwargs: _Client(comp))
    monkeypatch.setattr(
        "run_agent.get_tool_definitions",
        lambda *a, **k: [{"function": {"name": "web_search"}}],
    )
    # Tool returns a sizeable result so the strong nudge has something to
    # summarise (and so we can assert truncation behaviour indirectly).
    monkeypatch.setattr(
        "run_agent.handle_function_call",
        lambda name, args, task_id=None, **kw: json.dumps({"results": "R" * 50}),
    )

    agent = AIAgent(
        model="test-model",
        api_key="test-key",
        base_url="http://localhost:8080/v1",
        platform="cli",
        max_iterations=8,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )
    agent._disable_streaming = True
    agent.tool_delay = 0
    return agent, comp


def test_empty_after_tool_triggers_nudge_then_recovers(monkeypatch):
    """tool-call → empty → (nudge) → text recovers in one nudge."""
    agent, comp = _make_agent(monkeypatch, [
        _resp_tool(),          # 1: model calls a tool
        _resp_text(""),        # 2: empty after tool result → nudge #1
        _resp_text("recovered after nudge"),  # 3: model continues
    ])

    result = agent.run_conversation("do the thing")

    assert result["final_response"].startswith("recovered after nudge")
    # Exactly one nudge was needed.
    assert agent._post_tool_empty_retries == 1
    # A synthetic user nudge was injected before the recovery turn.
    last_sent = comp.seen_messages[-1]
    assert any(
        m.get("role") == "user"
        and "empty response" in (m.get("content") or "").lower()
        for m in last_sent
    )


def test_second_empty_triggers_strong_nudge_with_tool_summaries(monkeypatch):
    """tool → empty → nudge#1 → empty → strong nudge (with tool-result
    summaries reinjected) → text."""
    agent, comp = _make_agent(monkeypatch, [
        _resp_tool(),          # 1: tool call
        _resp_text(""),        # 2: empty → nudge #1
        _resp_text(""),        # 3: empty again → nudge #2 (strong)
        _resp_text("finally done"),  # 4: recovers
    ])

    result = agent.run_conversation("do the thing")

    assert result["final_response"].startswith("finally done")
    assert agent._post_tool_empty_retries == 2
    # The strong (2nd) nudge reinjects tool results as a summary.
    last_sent = comp.seen_messages[-1]
    strong = [
        m for m in last_sent
        if m.get("role") == "user" and "Tool results:" in (m.get("content") or "")
    ]
    assert strong, "strong nudge should reinject a 'Tool results:' summary"
    assert "web_search" in strong[-1]["content"]


def test_nudge_caps_at_two_attempts(monkeypatch):
    """Three consecutive empties must not produce a third nudge - the
    counter caps at 2 and the loop moves on (no infinite nudge loop)."""
    agent, comp = _make_agent(monkeypatch, [
        _resp_tool(),       # 1: tool call
        _resp_text(""),     # 2: empty → nudge #1
        _resp_text(""),     # 3: empty → nudge #2
        _resp_text(""),     # 4: empty → NO nudge #3
        _resp_text(""),     # padding (loop falls through to other recovery)
    ])

    agent.run_conversation("do the thing")

    # Counter must never exceed 2 regardless of how many empties arrive.
    assert agent._post_tool_empty_retries == 2
