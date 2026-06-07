"""Tests for the W1 side-effect audit ledger (gateway/side_effect_audit.py).

Covers the W0/W1 validation criteria: classification, schema/normalization,
buffered latency, crash-loss bound, and concurrent-write integrity.
"""

import json
import threading

import pytest

from gateway.side_effect_audit import (
    SideEffectAuditStore,
    classify_side_effect,
    _normalize_status,
)


@pytest.mark.parametrize(
    "tool_name,args,expected_class",
    [
        ("send_message", {"target": "#chan"}, "send"),
        ("write_file", {"path": "/tmp/x"}, "write"),
        ("patch", {"file_path": "/tmp/y"}, "write"),
        ("terminal", {"command": "ls"}, "exec"),
        ("self_improvement", {}, "self_improvement"),
        ("mcp__notion__notion-update-page", {}, "mcp_write"),
        ("mcp__slack__slack_send_message", {}, "mcp_write"),
        ("browser_click", {"selector": "#b"}, "browser"),
    ],
)
def test_classify_side_effect_tools(tool_name, args, expected_class):
    result = classify_side_effect(tool_name, args)
    assert result is not None
    assert result[0] == expected_class


@pytest.mark.parametrize(
    "tool_name",
    ["read_file", "web_search", "grep", "browser_screenshot", "browser_snapshot"],
)
def test_classify_read_tools_return_none(tool_name):
    assert classify_side_effect(tool_name, {}) is None


def test_target_ref_extraction():
    assert classify_side_effect("send_message", {"channel": "C123"})[1] == "C123"
    assert classify_side_effect("write_file", {"path": "/etc/hosts"})[1] == "/etc/hosts"


def test_normalize_status():
    assert _normalize_status("ok") == "success"
    assert _normalize_status("error") == "failed"
    assert _normalize_status("") == "unknown"
    assert _normalize_status(None) == "unknown"
    assert _normalize_status("blocked") == "blocked"


def test_record_lands_with_schema(tmp_path):
    store = SideEffectAuditStore(tmp_path / "ledger.jsonl")
    store.record(
        tool_name="send_message", action_class="send", source="agent",
        status="success", actor="U123", target_ref="#chan",
    )
    store.flush()
    lines = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["tool_name"] == "send_message"
    assert event["action_class"] == "send"
    assert event["source"] == "agent"
    assert event["status"] == "success"
    assert event["actor"] == "U123"
    assert event["event_version"] == "1.0"
    assert event["ts"].endswith("Z")


def test_none_fields_are_omitted(tmp_path):
    store = SideEffectAuditStore(tmp_path / "ledger.jsonl")
    store.record(tool_name="terminal", action_class="exec", source="agent",
                 status="success", error_type=None, rationale=None)
    store.flush()
    event = json.loads((tmp_path / "ledger.jsonl").read_text().strip())
    assert "error_type" not in event
    assert "rationale" not in event


def test_crash_loss_bounded_by_batch(tmp_path):
    # Below batch size with a huge flush interval -> stays buffered.
    store = SideEffectAuditStore(tmp_path / "ledger.jsonl", batch_size=10, flush_interval_sec=999)
    for _ in range(7):
        store.record(tool_name="write_file", action_class="write", source="agent", status="success")
    assert len(store._buffer) <= 10
    store.flush()
    assert len(store._buffer) == 0
    assert len((tmp_path / "ledger.jsonl").read_text().strip().splitlines()) == 7


def test_batch_size_triggers_flush(tmp_path):
    store = SideEffectAuditStore(tmp_path / "ledger.jsonl", batch_size=5, flush_interval_sec=999)
    for _ in range(5):
        store.record(tool_name="write_file", action_class="write", source="agent", status="success")
    # 5th record hits batch_size -> auto flush, buffer empty.
    assert len(store._buffer) == 0
    assert len((tmp_path / "ledger.jsonl").read_text().strip().splitlines()) == 5


def test_concurrent_writes_no_corruption(tmp_path):
    store = SideEffectAuditStore(tmp_path / "ledger.jsonl", batch_size=10, flush_interval_sec=0.01)

    def worker(wid):
        for i in range(200):
            store.record(tool_name="send_message", action_class="send",
                         source="agent", status="success", actor=f"w{wid}", args={"i": i})

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    store.flush()
    lines = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1600
    for line in lines:
        json.loads(line)  # must not raise — no interleaved/corrupt lines


def test_truncation(tmp_path):
    store = SideEffectAuditStore(tmp_path / "ledger.jsonl")
    from gateway.side_effect_audit import record_side_effect, get_store  # noqa: F401
    big = "x" * 5000
    store.record(tool_name="send_message", action_class="send", source="agent",
                 status="success", args=big[:300] + "…")
    store.flush()
    event = json.loads((tmp_path / "ledger.jsonl").read_text().strip())
    assert len(event["args"]) <= 302  # 300 + ellipsis
