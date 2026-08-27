"""Tests for surfacing real upstream errors from jmunch-mcp SSE 502 frames.

jmunch-mcp's gateway returns upstream errors as SSE-streamed JSON that
Hermes sees as an HTTP 502 whose body is ``data: {...}`` lines. The
default stringifier truncated the raw payload into useless messages
like ``HTTP 502: data: {"id": "jmunch-gw"...}``. ``_summarize_api_error``
now detects the SSE shape, parses the embedded error object, and
surfaces the upstream's real status / message / code. See
run_agent.py (AIAgent._summarize_api_error).
"""

from __future__ import annotations

import json

from run_agent import AIAgent


class _Err(Exception):
    """Exception whose str() is the raw error body, with optional status_code."""

    def __init__(self, body, status_code=None):
        super().__init__(body)
        if status_code is not None:
            self.status_code = status_code


def _sse(error_obj):
    """Build a jmunch-style SSE error payload string."""
    return (
        'data: {"id": "jmunch-gw", "object": "error"}\n'
        f"data: {json.dumps(error_obj)}\n"
        "data: [DONE]\n"
    )


def test_extracts_upstream_status_message_and_code():
    body = _sse({
        "error": {
            "message": "upstream airouter returned 429: rate limited",
            "code": "UPSTREAM_ERROR",
            "detail": {"status": 429},
        }
    })
    out = AIAgent._summarize_api_error(_Err(body, status_code=502))

    # Hermes's own 502 prefix is preserved...
    assert "HTTP 502:" in out
    # ...the upstream status is surfaced...
    assert "upstream returned HTTP 429" in out
    # ...the real message is included...
    assert "rate limited" in out
    # ...and the error code is appended.
    assert "UPSTREAM_ERROR" in out
    # The opaque raw SSE envelope must NOT leak through.
    assert "jmunch-gw" not in out
    assert "data:" not in out


def test_message_and_code_without_status():
    """A frame carrying a message + code but no detail.status is parsed
    (the ``"code"`` field is enough to trigger the SSE branch)."""
    body = _sse({"error": {
        "message": "upstream deepseek timed out",
        "code": "UPSTREAM_ERROR",
    }})
    out = AIAgent._summarize_api_error(_Err(body, status_code=502))
    assert "upstream deepseek timed out" in out
    assert "UPSTREAM_ERROR" in out
    assert "data:" not in out


def test_non_jmunch_error_falls_through_to_truncation():
    """A plain error string (no SSE shape) uses the existing fallback."""
    out = AIAgent._summarize_api_error(_Err("Connection reset by peer", status_code=500))
    assert "Connection reset by peer" in out
    # No spurious upstream-status decoration.
    assert "upstream returned HTTP" not in out


def test_sse_marker_without_valid_json_falls_through():
    """A payload that mentions data:/UPSTREAM_ERROR but isn't parseable JSON
    must not crash; it falls through to truncation."""
    body = 'data: not-json UPSTREAM_ERROR garbage\n'
    out = AIAgent._summarize_api_error(_Err(body, status_code=502))
    # Doesn't raise; returns some string containing the raw-ish content.
    assert isinstance(out, str) and out
    assert "upstream returned HTTP" not in out
