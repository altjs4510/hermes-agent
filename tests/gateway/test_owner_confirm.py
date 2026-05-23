import json
from datetime import datetime, timedelta, timezone

from gateway import owner_confirm


def test_parse_confirm_accepts_only_standard_verbs_and_token():
    parsed = owner_confirm.parse_confirm("  승인 : 전송   #A17f  ")

    assert parsed is not None
    assert parsed.verb == "전송"
    assert parsed.token == "A17F"


def test_parse_confirm_rejects_generic_execute_and_natural_language():
    assert owner_confirm.parse_confirm("승인: 실행 #A17F") is None
    assert owner_confirm.parse_confirm("좋아 진행해") is None


def test_propose_confirm_execute_payloads_are_joined_by_proposal_id(tmp_path):
    store = owner_confirm.OwnerConfirmStore(tmp_path / "owner-confirm.jsonl")
    now = datetime(2026, 5, 22, 5, 30, tzinfo=timezone.utc)

    proposed = store.propose(
        channel="C123",
        thread_ts="1779427792.105499",
        actor="U_bot",
        owner="U_owner",
        action_class="send",
        confirm_verb="전송",
        token="A17F",
        target_ref="slack:channel:C123|msg:draft",
        preview_ref="artifact://preview/1",
        risk_level="medium",
        side_effect=True,
        now=now,
    )
    confirmed = store.confirm(
        proposal_id=proposed["proposal_id"],
        actor="U_owner",
        confirm_message_ts="1779427860.905559",
        now=now + timedelta(seconds=30),
    )
    executed = store.execute(
        proposal_id=proposed["proposal_id"],
        actor="U_bot",
        result="success",
        result_code="OK",
        execution_ref="1779427862.000000",
        now=now + timedelta(seconds=32),
    )

    assert proposed["state"] == "proposed"
    assert confirmed["state"] == "confirmed"
    assert executed["state"] == "executed"
    assert confirmed["proposal_id"] == proposed["proposal_id"]
    assert executed["proposal_id"] == proposed["proposal_id"]
    assert proposed["idempotency_key"] == f"{proposed['proposal_id']}:전송:A17F"
    assert executed["status"] == "completed"

    records = [json.loads(line) for line in (tmp_path / "owner-confirm.jsonl").read_text().splitlines()]
    assert [record["state"] for record in records] == ["proposed", "confirmed", "executed"]


def test_duplicate_execution_is_rejected_with_already_executed(tmp_path):
    store = owner_confirm.OwnerConfirmStore(tmp_path / "owner-confirm.jsonl")
    proposed = store.propose(
        channel="C123",
        thread_ts="1779427792.105499",
        actor="U_bot",
        owner="U_owner",
        action_class="send",
        confirm_verb="전송",
        token="A17F",
        target_ref="slack:channel:C123|msg:draft",
        preview_ref="artifact://preview/1",
    )
    store.confirm(
        proposal_id=proposed["proposal_id"],
        actor="U_owner",
        confirm_message_ts="1779427860.905559",
    )
    store.execute(
        proposal_id=proposed["proposal_id"],
        actor="U_bot",
        result="success",
        result_code="OK",
        execution_ref="1779427862.000000",
    )

    rejected = store.execute(
        proposal_id=proposed["proposal_id"],
        actor="U_bot",
        result="success",
        result_code="OK",
        execution_ref="1779427863.000000",
    )

    assert rejected["state"] == "rejected"
    assert rejected["reject_reason"] == "ALREADY_EXECUTED"
    assert rejected["status"] == "failed"


def test_side_effect_policy_requires_confirm_only_for_shared_state_changes():
    assert owner_confirm.requires_owner_confirm("read") is False
    assert owner_confirm.requires_owner_confirm("draft") is False
    assert owner_confirm.requires_owner_confirm("code_edit") is False
    assert owner_confirm.requires_owner_confirm("send") is True
    assert owner_confirm.requires_owner_confirm("delete") is True
    assert owner_confirm.requires_owner_confirm("commit") is True


def test_failure_response_always_says_execution_did_not_happen():
    response = owner_confirm.failure_response("EXPIRED_TOKEN")

    assert "실행 안 됨" in response
    assert "preview" in response
