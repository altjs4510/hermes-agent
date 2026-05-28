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
    proposals_dir = tmp_path / "proposals"
    monkeypatch.setenv("HERMES_OWNER_CONFIRM_AUDIT_PATH", str(store_path))

    import tools.self_improvement_tool as sit

    monkeypatch.setattr(sit, "_QUEUE_PATH", queue_path)
    monkeypatch.setattr(sit, "_PROPOSALS_DIR", proposals_dir)
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


# --- Phase 2 -------------------------------------------------------------

def test_proposal_body_saved_and_loadable(isolated_paths, monkeypatch):
    monkeypatch.delenv("HERMES_OWNER_IDS", raising=False)
    _set_session(monkeypatch, user_id="U0AM13JAWM8")  # director-ish id
    from tools.self_improvement_tool import _handle_propose, load_proposal_body

    out = json.loads(_handle_propose({"feedback": "X 피드백", "plan": "Y 계획", "summary": "Z"}))
    body = load_proposal_body(out["proposal_id"])
    assert body is not None
    assert body["feedback"] == "X 피드백"
    assert body["plan"] == "Y 계획"
    assert body["summary"] == "Z"
    assert body["target_ref"].startswith("slack:")


def test_build_execution_prompt_has_double_gate():
    from tools.self_improvement_tool import build_execution_prompt

    p = build_execution_prompt({
        "feedback": "응답이 장황", "plan": "결론 먼저", "provider_label": "이사 박봉섭", "summary": "간결성",
    })
    assert "자가발전 실행 모드" in p
    assert "이사 박봉섭" in p
    assert "결론 먼저" in p
    # the change double-gate must be present
    assert "승인" in p and ("적용" in p)


def test_pending_summary(isolated_paths, monkeypatch):
    store_path, _ = isolated_paths
    # empty store → silent
    from tools.self_improvement_tool import _save_proposal_body, pending_summary
    from gateway.owner_confirm import OwnerConfirmStore

    assert pending_summary() == ""

    store = OwnerConfirmStore(str(store_path))
    # one executive (high) proposal left in "proposed" state
    pr = store.propose(
        channel="D1", thread_ts="t1", actor="U0AM13JAWM8", owner="UOWNER",
        action_class="self_improvement", confirm_verb="반영", token="AAAA",
        target_ref="slack:C1:root", preview_ref="self-improvement:간결성", risk_level="high",
    )
    _save_proposal_body(pr["proposal_id"], {
        "created": "2026-05-01T00:00:00Z", "tier": "high",
        "provider_label": "이사 박봉섭", "summary": "응답 간결성", "feedback": "장황",
    })
    s = pending_summary()
    assert "미처리 자가발전 제안 1건" in s
    assert "이사 박봉섭" in s and "🔴" in s

    # once confirmed, it drops out of the pending digest
    store.confirm(proposal_id=pr["proposal_id"], actor="UOWNER", confirm_message_ts="m1")
    assert pending_summary() == ""


def test_setup_reminder_cron(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_OWNER_IDS", "UOWNER")
    import tools.self_improvement_tool as sit

    created = {}
    monkeypatch.setattr("cron.jobs.load_jobs", lambda *a, **k: [])
    monkeypatch.setattr("cron.jobs.create_job", lambda **kw: created.update(kw) or {"id": "job_x"})

    res = sit.setup_reminder_cron(scripts_dir=tmp_path, schedule="0 9 * * 1")
    assert res["status"] == "created" and res["job_id"] == "job_x"
    # script written and imports pending_summary
    script = (tmp_path / sit._DIGEST_SCRIPT_NAME).read_text(encoding="utf-8")
    assert "from tools.self_improvement_tool import pending_summary" in script
    # cron job wired no_agent + owner DM origin
    assert created["no_agent"] is True
    assert created["origin"] == {"platform": "slack", "chat_id": "UOWNER"}
    assert created["script"] == sit._DIGEST_SCRIPT_NAME

    # idempotent: existing job by name → not recreated
    monkeypatch.setattr("cron.jobs.load_jobs", lambda *a, **k: [{"name": sit._REMINDER_JOB_NAME, "id": "job_x"}])
    res2 = sit.setup_reminder_cron(scripts_dir=tmp_path)
    assert res2["status"] == "exists"


def test_setup_reminder_cron_skips_without_owner(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_OWNER_IDS", raising=False)
    import tools.self_improvement_tool as sit

    res = sit.setup_reminder_cron(scripts_dir=tmp_path)
    assert res["status"] == "skipped"


# --- change double-gate (deterministic) ----------------------------------

def test_change_gate_noop_without_exec_marker(monkeypatch):
    from tools.self_improvement_tool import maybe_require_change_approval

    # non-change tool, and change tool outside an exec turn → never gated
    assert maybe_require_change_approval("read_file", {"path": "/x"}) is None
    assert maybe_require_change_approval("write_file", {"path": "/x", "content": "y"}) is None


def test_change_gate_blocks_when_denied(monkeypatch):
    import tools.self_improvement_tool as sit
    from gateway.session_context import reset_self_improvement_exec, set_self_improvement_exec

    calls = {}
    monkeypatch.setattr(
        "tools.approval.request_gateway_approval",
        lambda **kw: calls.update(kw) or {"approved": False, "message": "거부"},
    )
    tok = set_self_improvement_exec("p_x")
    try:
        out = sit.maybe_require_change_approval("write_file", {"path": "/etc/soul", "content": "z"})
    finally:
        reset_self_improvement_exec(tok)
    assert out is not None
    payload = json.loads(out)
    assert payload["change_pending"] is True
    # the approval card carried the path + a preview
    assert "/etc/soul" in calls["command"]
    assert calls["pattern_key"] == "self_improvement:apply_change"


def test_cleanup_stale_proposals(isolated_paths, monkeypatch):
    from datetime import datetime, timedelta, timezone

    store_path, _ = isolated_paths
    from gateway.owner_confirm import OwnerConfirmStore
    from tools.self_improvement_tool import cleanup_stale_proposals

    store = OwnerConfirmStore(str(store_path))
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    old = now - timedelta(days=100)
    fresh = now - timedelta(days=10)

    def _mk(token, when):
        return store.propose(
            channel="D1", thread_ts=token, actor="U1", owner="UO",
            action_class="self_improvement", confirm_verb="반영", token=token,
            target_ref="r", preview_ref="p", risk_level="low", now=when,
        )

    stale = _mk("AAAA", old)       # 100d old, pending → should expire
    recent = _mk("BBBB", fresh)    # 10d old, pending → keep
    done = _mk("CCCC", old)        # old but confirmed → keep
    store.confirm(proposal_id=done["proposal_id"], actor="UO", confirm_message_ts="m", now=old)

    res = cleanup_stale_proposals(max_age_days=90, now=now)
    assert res["expired"] == 1
    assert stale["proposal_id"] in res["ids"]
    assert recent["proposal_id"] not in res["ids"]
    assert done["proposal_id"] not in res["ids"]

    # the stale one is now in expired state
    states = {e["state"] for e in store.lookup(stale["proposal_id"])}
    assert "expired" in states


def test_setup_stale_cleanup_cron(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_OWNER_IDS", "UOWNER")
    import tools.self_improvement_tool as sit

    created = {}
    monkeypatch.setattr("cron.jobs.load_jobs", lambda *a, **k: [])
    monkeypatch.setattr("cron.jobs.create_job", lambda **kw: created.update(kw) or {"id": "job_s"})

    res = sit.setup_stale_cleanup_cron(scripts_dir=tmp_path)
    assert res["status"] == "created" and res["job_id"] == "job_s"
    assert created["schedule"] == "0 9 1 * *"  # monthly
    assert created["no_agent"] is True
    script = (tmp_path / sit._STALE_SCRIPT_NAME).read_text(encoding="utf-8")
    assert "cleanup_stale_proposals" in script

    monkeypatch.setattr("cron.jobs.load_jobs", lambda *a, **k: [{"name": sit._STALE_CLEANUP_JOB_NAME, "id": "x"}])
    assert sit.setup_stale_cleanup_cron(scripts_dir=tmp_path)["status"] == "exists"


def test_change_gate_allows_when_approved(monkeypatch):
    import tools.self_improvement_tool as sit
    from gateway.session_context import reset_self_improvement_exec, set_self_improvement_exec

    monkeypatch.setattr(
        "tools.approval.request_gateway_approval",
        lambda **kw: {"approved": True, "message": None},
    )
    tok = set_self_improvement_exec("p_x")
    try:
        out = sit.maybe_require_change_approval("patch", {"path": "/f", "patch": "@@"})
    finally:
        reset_self_improvement_exec(tok)
    assert out is None  # approved → write proceeds
