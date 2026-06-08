"""Egress lockdown Phase 1 — send_message shadow integration.

Proves the dispatch preflight records the right would_* event for a send after
an external read, WITHOUT changing behavior (preflight returns None = proceed).
The W2/W3/W4 send gate in tools/send_message_tool.py is untouched by Phase 1.
"""

import json

import pytest

import gateway.side_effect_audit as sea
from gateway.egress_policy import _cache as _ep_cache
from gateway.session_taint import clear_taint, mark_read
from model_tools import _emit_egress_shadow_preflight


@pytest.fixture(autouse=True)
def _temp_ledger_and_taint(tmp_path):
    clear_taint()
    store = sea.SideEffectAuditStore(tmp_path / "ledger.jsonl", batch_size=1)
    old = sea._STORE
    sea._STORE = store
    _ep_cache["mtime"] = None
    _ep_cache["policy"] = None
    try:
        yield store, tmp_path / "ledger.jsonl"
    finally:
        sea._STORE = old
        clear_taint()
        _ep_cache["mtime"] = None
        _ep_cache["policy"] = None


def _egress_records(path):
    if not path.exists():
        return []
    return [
        json.loads(l)
        for l in path.read_text(encoding="utf-8").strip().splitlines()
        if l and json.loads(l).get("tool_name") == "egress_lockdown"
    ]


def test_external_read_then_send_records_would_confirm(_temp_ledger_and_taint):
    store, path = _temp_ledger_and_taint
    mark_read("web_extract", {"url": "https://evil.example"})
    # Preflight must never block in Phase 1 (shadow): returns None, no raise.
    assert _emit_egress_shadow_preflight("send_message", {"target": "slack:#public"}) is None
    store.flush()
    recs = _egress_records(path)
    assert len(recs) == 1
    assert recs[0]["action_class"] == "egress_shadow"
    assert recs[0]["status"] == "would_confirm"
    assert recs[0]["source"] == "shadow"
    assert "tainted-external-send" in recs[0]["rationale"]
    assert recs[0]["target_ref"] == "slack:#public"


def test_same_thread_reply_records_would_allow(_temp_ledger_and_taint):
    store, path = _temp_ledger_and_taint
    mark_read("web_extract", {"url": "https://evil.example"})
    # No explicit target = same-surface reply → allowlisted.
    _emit_egress_shadow_preflight("send_message", {})
    store.flush()
    recs = _egress_records(path)
    assert len(recs) == 1
    assert recs[0]["status"] == "would_allow"


def test_no_taint_emits_no_shadow_event(_temp_ledger_and_taint):
    store, path = _temp_ledger_and_taint
    # No prior read → no taint → no shadow noise, current behavior preserved.
    _emit_egress_shadow_preflight("send_message", {"target": "slack:#public"})
    store.flush()
    assert _egress_records(path) == []


def test_read_tool_emits_no_shadow_event(_temp_ledger_and_taint):
    store, path = _temp_ledger_and_taint
    mark_read("web_extract", {"url": "https://evil.example"})
    # A subsequent READ tool is not egress → no shadow event.
    _emit_egress_shadow_preflight("read_file", {"path": "/x"})
    store.flush()
    assert _egress_records(path) == []


def test_external_read_then_mcp_write_records_would_confirm(_temp_ledger_and_taint):
    store, path = _temp_ledger_and_taint
    mark_read("web_extract", {"url": "https://evil.example"})
    # Underscore-style verb (slack_send_message) → capability_of = mcp_write.
    # (Hyphenated claude.ai verbs like notion-create-pages are a known
    # capability_of blind spot, tightened in Phase 3 — not here.)
    _emit_egress_shadow_preflight("mcp__slack__slack_send_message", {})
    store.flush()
    recs = _egress_records(path)
    assert len(recs) == 1
    assert recs[0]["status"] == "would_confirm"
    assert "tainted-mcp-write" in recs[0]["rationale"]
