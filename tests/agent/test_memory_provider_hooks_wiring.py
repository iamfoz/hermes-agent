"""Turn wiring of the before_prompt_build / on_tool_call_observed / on_recall_used provider hooks.

Prompt-caching invariant: the per-turn ``before_prompt_build`` block rides the current user turn
(the ``api_content`` sidecar), never the byte-stable system prompt."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.memory_context import build_memory_context_block
from agent.memory_manager import MemoryManager
from tests.agent.test_api_content_sidecar import _FakeAgent, _build
from tests.agent.test_memory_provider import FakeMemoryProvider
from tests.agent.test_tool_call_incremental_persistence import _make_agent, _mock_tool_call


class _HookProvider(FakeMemoryProvider):
    def __init__(self, block: str = ""):
        super().__init__(name="hooks")
        self._block = block
        self.turn_states: list[dict] = []
        self.observed: list[tuple] = []
        self.recalls: list[tuple] = []

    def before_prompt_build(self, turn_state):
        self.turn_states.append(turn_state)
        return self._block

    def on_tool_call_observed(self, tool_name, args, result, *, session_id="", success=True):
        self.observed.append((tool_name, args, result, session_id, success))

    def on_recall_used(self, response_text, *, session_id=""):
        self.recalls.append((response_text, session_id))


def _manager(provider) -> MemoryManager:
    mgr = MemoryManager()
    mgr._providers.append(provider)
    return mgr


@pytest.fixture(autouse=True)
def _stub_runtime_main():
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield


def test_dynamic_block_rides_user_turn_not_system_prompt():
    provider = _HookProvider(block="DYNAMIC-BLOCK")
    provider._prefetch_result = "never-prefetched"
    agent = _FakeAgent()
    agent._memory_manager = _manager(provider)
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        ctx = _build(agent, user_message="what did we decide about the cache?")

    assert agent._cached_system_prompt == "SYSTEM"
    assert "DYNAMIC-BLOCK" not in (ctx.active_system_prompt or "")
    assert ctx.ext_system_prompt_cache == "DYNAMIC-BLOCK"
    msg = ctx.messages[ctx.current_turn_user_idx]
    assert msg["content"] == "what did we decide about the cache?"
    assert msg["api_content"] == msg["content"] + "\n\n" + build_memory_context_block("DYNAMIC-BLOCK")
    assert provider.prefetch_queries == []  # one injection point, never both
    (state,) = provider.turn_states
    assert state["query"] == "what did we decide about the cache?"
    assert state["session_id"] == "sess-1"
    assert set(state) == {"query", "session_id", "parent_session_id", "turn_number", "model", "platform", "agent_id"}


@pytest.mark.parametrize("executor_mode", ["sequential", "concurrent"])
def test_tool_call_observed_on_both_executor_paths(executor_mode):
    agent = _make_agent()
    provider = _HookProvider()
    agent._memory_manager = _manager(provider)
    agent._flush_messages_to_session_db = MagicMock()
    dispatch = (
        patch("model_tools.handle_function_call", return_value="tool output")
        if executor_mode == "sequential"
        else patch.object(agent, "_invoke_tool", return_value="tool output")
    )
    calls = [_mock_tool_call(arguments='{"query": "x"}', call_id="c1"),
             _mock_tool_call(arguments='{"query": "y"}', call_id="c2")]
    with dispatch, patch("agent.tool_executor.maybe_persist_tool_result", side_effect=lambda **kw: kw["content"]):
        getattr(agent, f"_execute_tool_calls_{executor_mode}")(
            SimpleNamespace(content="", tool_calls=calls), [], "task-1")

    assert [(o[0], o[1], o[2], o[4]) for o in provider.observed] == [
        ("web_search", {"query": "x"}, "tool output", True),
        ("web_search", {"query": "y"}, "tool output", True),
    ]


def test_recall_used_fires_before_sync_on_completed_turn():
    agent = _make_agent()
    order: list[str] = []

    class _Recaller(_HookProvider):
        def on_recall_used(self, response_text, *, session_id=""):
            order.append(f"recall:{response_text}")

    mgr = _manager(_Recaller())
    mgr.sync_all = lambda *a, **k: order.append("sync")
    mgr.queue_prefetch_all = lambda *a, **k: None
    agent._memory_manager = mgr
    agent._sync_external_memory_for_turn(original_user_message="hello there", final_response="the answer",
                                         interrupted=False)
    assert order == ["recall:the answer", "sync"]


def test_subagent_fork_suppresses_all_three_hooks():
    """A fork keeps memory recall and writes off on the host, so none of the new hooks reach a provider."""
    provider = _HookProvider(block="fork must not see this")
    mgr = _manager(provider)
    mgr._agent_context = "subagent"

    assert mgr.build_dynamic_system_prompt({"query": "hi"}) == ""
    mgr.notify_recall_used("answer", session_id="s")
    mgr.notify_tool_call("terminal", {"cmd": "ls"}, "ok", session_id="s")

    assert provider.turn_states == []
    assert provider.recalls == []
    assert provider.observed == []
