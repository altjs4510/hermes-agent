"""Tests for gateway/session_taint.py (egress lockdown Phase 1).

Covers: read-source classification, mark/flag semantics, task isolation, and
that side-effect tools never taint.
"""

import asyncio

import pytest

from gateway import session_taint as st
from gateway.session_taint import (
    SessionTaint,
    TaintEvent,
    classify_read_source,
    clear_taint,
    current_taint,
    mark_read,
)


@pytest.fixture(autouse=True)
def _fresh_taint():
    clear_taint()
    yield
    clear_taint()


# ── classification ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "tool,trust,source_class",
    [
        ("web_search", st.TRUST_EXTERNAL, "web"),
        ("web_extract", st.TRUST_EXTERNAL, "web"),
        ("x_search", st.TRUST_EXTERNAL, "web"),
        ("browser_snapshot", st.TRUST_EXTERNAL, "browser"),
        ("browser_vision", st.TRUST_EXTERNAL, "browser"),
        ("read_file", st.TRUST_PRIVATE, "file"),
        ("search_files", st.TRUST_PRIVATE, "file"),
        ("mcp__claude_ai_Notion__notion-fetch", st.TRUST_INTERNAL, "mcp"),
        ("mcp__claude_ai_Slack__slack_read_channel", st.TRUST_INTERNAL, "mcp"),
    ],
)
def test_read_tools_classified(tool, trust, source_class):
    ev = classify_read_source(tool, {})
    assert ev is not None
    assert ev.trust_level == trust
    assert ev.source_class == source_class
    assert ev.tool_name == tool


@pytest.mark.parametrize(
    "tool",
    [
        "send_message",
        "write_file",
        "terminal",
        "browser_click",      # browse egress, not a read
        "browser_navigate",   # browse egress, not a read
        "mcp__claude_ai_Notion__notion-update-page",  # mcp_write, not a read
        "image_generate",
    ],
)
def test_side_effect_tools_do_not_taint(tool):
    assert classify_read_source(tool, {}) is None


def test_unknown_tool_returns_none():
    assert classify_read_source("some_made_up_tool", {}) is None


# ── flag semantics ──────────────────────────────────────────────────────

def test_external_read_sets_external_flag_only():
    t = SessionTaint()
    t.add(TaintEvent("web", "u", st.TRUST_EXTERNAL, "web_extract", "t"))
    assert t.external_content_seen is True
    assert t.private_context_seen is False
    assert t.any_taint is True


def test_private_and_internal_read_sets_private_flag():
    t = SessionTaint()
    t.add(TaintEvent("file", "/x", st.TRUST_PRIVATE, "read_file", "t"))
    t.add(TaintEvent("mcp", "m", st.TRUST_INTERNAL, "mcp__x__y_get", "t"))
    assert t.external_content_seen is False
    assert t.private_context_seen is True


def test_empty_taint_is_untainted():
    assert SessionTaint().any_taint is False


# ── mark_read on the live contextvar ────────────────────────────────────

def test_mark_read_accumulates_on_current_taint():
    assert current_taint().any_taint is False
    mark_read("web_extract", {"url": "https://evil.example/x"})
    cur = current_taint()
    assert cur.external_content_seen is True
    assert len(cur.events) == 1
    assert cur.events[0].source_ref == "https://evil.example/x"


def test_mark_read_ignores_side_effect_tool():
    mark_read("send_message", {"target": "#x"})
    assert current_taint().any_taint is False


def test_clear_taint_resets():
    mark_read("web_search", {"query": "q"})
    assert current_taint().any_taint is True
    clear_taint()
    assert current_taint().any_taint is False


# ── task isolation ──────────────────────────────────────────────────────

def test_taint_is_task_isolated():
    """Two concurrent asyncio tasks must not share taint."""

    async def tainted_task():
        mark_read("web_extract", {"url": "x"})
        await asyncio.sleep(0)
        return current_taint().external_content_seen

    async def clean_task():
        await asyncio.sleep(0)
        return current_taint().external_content_seen

    async def main():
        # Each task runs in its own copied context → independent taint.
        return await asyncio.gather(tainted_task(), clean_task())

    tainted, clean = asyncio.run(main())
    assert tainted is True
    assert clean is False
