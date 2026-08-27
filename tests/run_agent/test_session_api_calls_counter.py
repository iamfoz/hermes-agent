"""Regression test for session_api_calls counting independent of usage data.

Previously ``session_api_calls`` only ticked up inside the
``if response.usage`` block. Providers / proxies that return a
successful response without a usage frame (e.g. streaming SSE shims
that consolidate chunks and drop the final usage frame) left the
counter at zero forever — which short-circuited /usage's detailed
display (CLI + gateway gate on ``session_api_calls > 0``).

The fix moves the increment out of the usage gate so a successful
response always counts, while token totals stay inside the
usage-present gate so they remain accurate when usage is missing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _mock_response(content="Hello", finish_reason="stop", usage=None):
    msg = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning_content=None,
        reasoning=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    resp = SimpleNamespace(choices=[choice], model="test/model")
    resp.usage = SimpleNamespace(**usage) if usage else None
    return resp


@pytest.fixture()
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        a._cached_system_prompt = "You are helpful."
        a._use_prompt_caching = False
        a.tool_delay = 0
        a.compression_enabled = False
        a.save_trajectories = False
        return a


def _run(agent, resp):
    agent.client.chat.completions.create.side_effect = [resp]
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation("hello")


def test_api_calls_increment_when_usage_missing(agent):
    """A successful response with NO usage frame must still tick the counter.

    This is the bug: the proxy/shim dropped the usage frame, so the old
    code (increment inside ``if response.usage``) never counted the call.
    """
    assert agent.session_api_calls == 0
    result = _run(agent, _mock_response(content="done", usage=None))

    assert result["completed"] is True
    assert agent.session_api_calls == 1, (
        "a successful response must count even when usage data is absent"
    )
    # No usage frame → token totals stay at zero (not corrupted).
    assert agent.session_total_tokens == 0


def test_api_calls_increment_once_when_usage_present(agent):
    """A response WITH usage increments the counter exactly once (no double
    count) and still records token totals."""
    assert agent.session_api_calls == 0
    result = _run(
        agent,
        _mock_response(
            content="done",
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        ),
    )

    assert result["completed"] is True
    assert agent.session_api_calls == 1, "usage-present path must count exactly once"
    # Token bookkeeping still runs inside the usage gate.
    assert agent.session_total_tokens == 120
    assert agent.session_prompt_tokens == 100
    assert agent.session_completion_tokens == 20
