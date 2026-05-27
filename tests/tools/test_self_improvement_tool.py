"""Unit tests for the self-improvement feedback-loop tool.

Covers the Phase 1 contract (docs/plans/2026-05-27-self-improvement-feedback-loop.md):
- a proposal is recorded to the owner-confirm store (action_class/verb/refs),
- delivery degrades gracefully (no owner id / no token → owner_notified False,
  but the proposal is still persisted — no silent fail),
- owner approval queues the plan,
- priority tiers map to the right card badge.
"""

import json

import pytest


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Point the owner-confirm store and the SI queue at temp files."""
    store_path = tmp_path / "owner-confirm.jsonl"
    queue_path = tmp_path / "self-improvement-queue.jsonl"
    monkeypatch.setenv("HERMES_OWNER_CONFIRM_AUDIT_PATH", str(store_path))

    import tools.self_improvement_tool as sit

    monkeypatch.setattr(sit, "_QUEUE_PATH", queue_path)
    return store_path, queue_path


def _set_session(monkeypatch, **kw):
    from gateway import session_context

    session_context.set_session_vars(
        platform=kw.get("platform", "slack"),
        chat_id=kw.get("chat_id", "C123"),
        thread_id=kw.get("thread_id", "1700.0001"),
        user_id=kw.get("user_id", "UGUEST"),
        user_name=kw.get("user_name", "guest"),
    )


def test_records_proposal_even_without_owner_or_token(isolated_paths, monkeypatch):
    store_path, _ = isolated_paths
    monkeypatch.delenv("HERMES_OWNER_IDS", raising=False)
    _set_session(monkeypatch)

    from tools.self_improvement_tool import _handle_propose

    out = json.loads(
        _handle_propose({"feedback": "답이 너무 길어", "plan": "응답 길이 단축", "summary": "응답 말투"})
    )

    assert out["status"] == "proposed"
    assert out["owner_notified"] is False
    assert out["delivery_error"] == "HERMES_OWNER_IDS not set"
    assert "guest_ack_hint" in out

    # Proposal is persisted to the owner-confirm store regardless of delivery.
    events = [json.loads(l) for l in store_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(events) == 1
    ev = events[0]
    assert ev["state"] == "proposed"
    assert ev["action_class"] == "self_improvement"
    assert ev["confirm_verb"] == "반영"
    assert ev["target_ref"] == "slack:C123:1700.0001"
    assert ev["proposal_id"] == out["proposal_id"]


def test_missing_fields_error(isolated_paths, monkeypatch):
    _set_session(monkeypatch)
    from tools.self_improvement_tool import _handle_propose

    assert "error" in json.loads(_handle_propose({"plan": "x"}))
    assert "error" in json.loads(_handle_propose({"feedback": "y"}))


def test_approval_records_to_queue(isolated_paths):
    _, queue_path = isolated_paths
    from tools.self_improvement_tool import record_self_improvement_approval

    proposed = {
        "proposal_id": "p_abc",
        "actor": "UGUEST",
        "owner": "UOWNER",
        "target_ref": "slack:C1:root",
        "preview_ref": "self-improvement:말투",
        "risk_level": "low",
    }
    entry = record_self_improvement_approval(proposed)
    assert entry["status"] == "queued"
    assert entry["proposal_id"] == "p_abc"

    rows = [json.loads(l) for l in queue_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["proposal_id"] == "p_abc"


def test_tier_badge():
    from tools.self_improvement_tool import _tier_badge

    assert "high" in _tier_badge("high", True)
    assert "high" in _tier_badge("low", True)  # urgent overrides
    assert "normal" in _tier_badge("normal", False)
    assert "low" in _tier_badge("low", False)


@pytest.mark.asyncio
async def test_owner_approval_queues_self_improvement(isolated_paths, monkeypatch):
    """Owner approving a self_improvement card (no executor) → queued + ack."""
    store_path, queue_path = isolated_paths
    from unittest.mock import AsyncMock

    from gateway.config import PlatformConfig
    from gateway.owner_confirm import OwnerConfirmStore
    from gateway.platforms.slack import SlackAdapter

    owner = "UOWNER"
    dm = "DOWNER"
    card_ts = "1700.5000"

    # Seed a proposal exactly as the tool would (anchored to the DM card ts).
    store = OwnerConfirmStore(str(store_path))
    proposed = store.propose(
        channel=dm,
        thread_ts=card_ts,
        actor="UGUEST",
        owner=owner,
        action_class="self_improvement",
        confirm_verb="반영",
        token="A1B2",
        target_ref="slack:C1:1700.0001",
        preview_ref="self-improvement:말투",
        risk_level="low",
    )

    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake"))
    replies = []
    adapter._send_owner_confirm_reply = AsyncMock(
        side_effect=lambda c, t, text: replies.append(text)
    )

    handled = await adapter._handle_owner_confirm_message(
        text="승인: 반영 #A1B2",
        channel_id=dm,
        thread_ts=card_ts,
        message_ts="1700.6000",
        user_id=owner,
    )

    assert handled is True
    assert replies and "승인됨" in replies[0]

    # Queued for Phase 2 execution.
    rows = [json.loads(l) for l in queue_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["proposal_id"] == proposed["proposal_id"]

    # Store shows confirmed + executed(QUEUED_P1).
    states = [e["state"] for e in store.lookup(proposed["proposal_id"])]
    assert "confirmed" in states and "executed" in states
