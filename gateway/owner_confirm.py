"""Owner-confirm primitives for gateway side-effect approvals.

This module implements the small, deterministic parts of the L3 owner-confirm
contract used by messaging gateways:

- parse the thread-local confirmation grammar,
- append auditable proposal/confirmation/execution/rejection events,
- guard duplicate execution with an idempotency key,
- classify action classes that require owner confirmation.

The gateway integration layer is intentionally kept outside this module so the
logic is easy to test without Slack/Discord adapters.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

_CONFIRM_RE = re.compile(
    r"^\s*승인\s*:\s*(전송|수정|생성|삭제|반영|commit)\s+#([A-Fa-f0-9]{4,8})\s*$"
)
# "반영" (apply/adopt) is the verb for self-improvement proposals raised by the
# feedback loop — see docs/plans/2026-05-27-self-improvement-feedback-loop.md.
_ALLOWED_CONFIRM_VERBS = {"전송", "수정", "생성", "삭제", "반영", "commit"}
_DEFAULT_TOKEN_TTL_SEC = 600


@dataclass(frozen=True)
class ParsedConfirm:
    """Parsed owner-confirm message."""

    verb: str
    token: str


def parse_confirm(text: str) -> ParsedConfirm | None:
    """Parse ``승인: <verb> #<token>`` or return ``None`` on mismatch.

    Natural-language confirmations and generic ``실행`` are deliberately not
    accepted. Tokens are normalized to uppercase for case-insensitive matching.
    """

    match = _CONFIRM_RE.match(text or "")
    if not match:
        return None
    verb, token = match.groups()
    return ParsedConfirm(verb=verb, token=token.upper())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(event: dict[str, Any], now: datetime) -> bool:
    """Whether a proposal event's token TTL has elapsed by ``now``."""
    expires_at = str(event.get("expires_at") or "")
    if not expires_at:
        return False
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return now > parsed.astimezone(timezone.utc)


def _iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _proposal_id() -> str:
    return f"p_{uuid.uuid4().hex[:16]}"


def idempotency_key(proposal_id: str, confirm_verb: str, token: str) -> str:
    return f"{proposal_id}:{confirm_verb}:{token.upper()}"


class OwnerConfirmStore:
    """Append-only JSONL audit store with in-process idempotency guard."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read_events_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            events.append(json.loads(line))
        return events

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._read_events_unlocked()

    def lookup(self, proposal_id: str) -> list[dict[str, Any]]:
        return [event for event in self.events() if event.get("proposal_id") == proposal_id]

    def latest_proposed(self, *, channel: str, thread_ts: str) -> dict[str, Any] | None:
        events = self.events()
        for event in reversed(events):
            if (
                event.get("state") == "proposed"
                and event.get("channel") == channel
                and event.get("thread_ts") == thread_ts
            ):
                return event
        return None

    def live_proposals(
        self, *, channel: str, thread_ts: str, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Proposals in this channel+thread still awaiting confirmation.

        A proposal is *live* when its most recent event is ``proposed`` (not
        yet confirmed/rejected/executed) and its token has not expired.
        Returned oldest-first. Used by the gateway to resolve a bare ``승인``
        against the single obvious pending proposal in a thread.
        """
        now = now or _utc_now()
        latest_by_id: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for event in self.events():
            if event.get("channel") != channel or event.get("thread_ts") != thread_ts:
                continue
            pid = str(event.get("proposal_id") or "")
            if not pid:
                continue
            if pid not in latest_by_id:
                order.append(pid)
            latest_by_id[pid] = event
        live: list[dict[str, Any]] = []
        for pid in order:
            event = latest_by_id[pid]
            if event.get("state") != "proposed":
                continue
            if _is_expired(event, now):
                continue
            live.append(event)
        return live

    def _append_unlocked(self, event: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return event

    def _append(self, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._append_unlocked(event)

    def propose(
        self,
        *,
        channel: str,
        thread_ts: str,
        actor: str,
        owner: str,
        action_class: str,
        confirm_verb: str,
        token: str,
        target_ref: str,
        preview_ref: str,
        risk_level: str = "low",
        side_effect: bool = True,
        token_ttl_sec: int = _DEFAULT_TOKEN_TTL_SEC,
        proposal_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if confirm_verb not in _ALLOWED_CONFIRM_VERBS:
            raise ValueError(f"Unsupported confirm verb: {confirm_verb}")
        timestamp = now or _utc_now()
        normalized_token = token.upper().lstrip("#")
        proposal_id = proposal_id or _proposal_id()
        expires_at = timestamp + timedelta(seconds=token_ttl_sec)
        event = {
            "state": "proposed",
            "event_version": "1.0",
            "ts": _iso_z(timestamp),
            "channel": channel,
            "thread_ts": thread_ts,
            "proposal_id": proposal_id,
            "actor": actor,
            "owner": owner,
            "action_class": action_class,
            "confirm_verb": confirm_verb,
            "token": normalized_token,
            "token_ttl_sec": token_ttl_sec,
            "expires_at": _iso_z(expires_at),
            "target_ref": target_ref,
            "preview_ref": preview_ref,
            "risk_level": risk_level,
            "side_effect": bool(side_effect),
            "idempotency_key": idempotency_key(proposal_id, confirm_verb, normalized_token),
            "status": "awaiting_confirm",
        }
        return self._append(event)

    def confirm(
        self,
        *,
        proposal_id: str,
        actor: str,
        confirm_message_ts: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            proposed = _latest_state(self._read_events_unlocked(), proposal_id, "proposed")
            if proposed is None:
                raise KeyError(f"Unknown proposal_id: {proposal_id}")
            timestamp = now or _utc_now()
            event = _copy_common(proposed)
            event.update(
                {
                    "state": "confirmed",
                    "ts": _iso_z(timestamp),
                    "actor": actor,
                    "confirmed_at": _iso_z(timestamp),
                    "confirm_message_ts": confirm_message_ts,
                    "status": "approved",
                }
            )
            return self._append_unlocked(event)

    def reject(
        self,
        *,
        proposal_id: str,
        actor: str,
        reject_reason: str,
        reject_detail: str,
        confirm_message_ts: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            proposed = _latest_state(self._read_events_unlocked(), proposal_id, "proposed")
            if proposed is None:
                raise KeyError(f"Unknown proposal_id: {proposal_id}")
            timestamp = now or _utc_now()
            event = _copy_common(proposed)
            event.update(
                {
                    "state": "rejected",
                    "ts": _iso_z(timestamp),
                    "actor": actor,
                    "status": "failed",
                    "reject_reason": reject_reason,
                    "reject_detail": reject_detail,
                }
            )
            if confirm_message_ts is not None:
                event["confirm_message_ts"] = confirm_message_ts
            return self._append_unlocked(event)

    def expire(
        self,
        *,
        proposal_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            proposed = _latest_state(self._read_events_unlocked(), proposal_id, "proposed")
            if proposed is None:
                raise KeyError(f"Unknown proposal_id: {proposal_id}")
            timestamp = now or _utc_now()
            created_at = _parse_iso_z(str(proposed["ts"]))
            event = _copy_common(proposed)
            event.update(
                {
                    "state": "expired",
                    "ts": _iso_z(timestamp),
                    "actor": proposed.get("actor", ""),
                    "status": "failed",
                    "expired_at": _iso_z(timestamp),
                    "ttl_sec": int(proposed.get("token_ttl_sec", _DEFAULT_TOKEN_TTL_SEC)),
                    "pending_seconds": max(0, int((timestamp - created_at).total_seconds())),
                    "preview_ref": proposed.get("preview_ref", ""),
                }
            )
            return self._append_unlocked(event)

    def execute(
        self,
        *,
        proposal_id: str,
        actor: str,
        result: str,
        result_code: str,
        execution_ref: str | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            events = self._read_events_unlocked()
            proposed = _latest_state(events, proposal_id, "proposed")
            if proposed is None:
                raise KeyError(f"Unknown proposal_id: {proposal_id}")
            key = proposed["idempotency_key"]
            if any(event.get("state") == "executed" and event.get("idempotency_key") == key for event in events):
                timestamp = now or _utc_now()
                rejected = _copy_common(proposed)
                rejected.update(
                    {
                        "state": "rejected",
                        "ts": _iso_z(timestamp),
                        "actor": actor,
                        "status": "failed",
                        "reject_reason": "ALREADY_EXECUTED",
                        "reject_detail": "Execution already recorded for this proposal.",
                    }
                )
                return self._append_unlocked(rejected)

            timestamp = now or _utc_now()
            normalized_result = "success" if result == "success" else "failed"
            event = _copy_common(proposed)
            event.update(
                {
                    "state": "executed",
                    "ts": _iso_z(timestamp),
                    "actor": actor,
                    "executed_at": _iso_z(timestamp),
                    "result": normalized_result,
                    "result_code": result_code,
                    "target_ref": proposed.get("target_ref", ""),
                    "preview_ref": proposed.get("preview_ref", ""),
                    "execution_ref": execution_ref,
                    "error": error,
                    "status": "completed" if normalized_result == "success" else "failed",
                }
            )
            return self._append_unlocked(event)


def _parse_iso_z(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _latest_state(events: Iterable[dict[str, Any]], proposal_id: str, state: str) -> dict[str, Any] | None:
    for event in reversed(list(events)):
        if event.get("proposal_id") == proposal_id and event.get("state") == state:
            return event
    return None


def _copy_common(proposed: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "event_version",
        "channel",
        "thread_ts",
        "proposal_id",
        "idempotency_key",
        "owner",
        "action_class",
        "confirm_verb",
        "token",
    )
    return {key: proposed[key] for key in keys if key in proposed}


_NO_CONFIRM_CLASSES = {"read", "read-only", "readonly", "draft", "preview", "code_edit"}
_CONFIRM_CLASSES = {
    "send",
    "post",
    "update",
    "delete",
    "commit",
    "push",
    "merge",
    "release",
    "issue_create",
    "db_update",
    "notion_update",
    # Self-improvement proposals from the non-owner feedback loop: a non-owner's
    # feedback about the bot is turned into a plan that only the owner may adopt.
    "self_improvement",
}


def requires_owner_confirm(action_class: str, *, side_effect: bool | None = None) -> bool:
    """Return whether an action class must pass owner-confirm.

    Local ``code_edit`` is treated as a speculative working-copy change and does
    not require owner-confirm until it becomes shared state (commit/push/etc.).
    Unknown classes fall back to the explicit ``side_effect`` flag; if absent,
    they are conservatively treated as requiring confirmation.
    """

    normalized = (action_class or "").strip().lower().replace("_local", "")
    if normalized in _NO_CONFIRM_CLASSES:
        return False
    if normalized in _CONFIRM_CLASSES:
        return True
    if side_effect is not None:
        return bool(side_effect)
    return True

_FAILURE_RESPONSES = {
    "FORMAT_MISMATCH": '실행 안 됨: 승인 형식 불일치. "승인: <동사> #<토큰>"으로 입력해줘.',
    "VERB_MISMATCH": "실행 안 됨: 승인 동작 불일치. preview의 동작과 승인 문구가 달라.",
    "TOKEN_MISMATCH": "실행 안 됨: 승인 토큰 불일치. 최신 preview의 토큰을 확인해줘.",
    "OWNER_MISMATCH": "실행 안 됨: 승인 권한 없음. owner-confirm은 owner만 가능해.",
    "EXPIRED_TOKEN": "실행 안 됨: 토큰 만료. preview를 다시 생성할게.",
    "ALREADY_EXECUTED": "실행 안 됨: 이미 실행된 승인이라 중복 실행하지 않았어.",
}


def failure_response(reject_reason: str) -> str:
    return _FAILURE_RESPONSES.get(
        reject_reason,
        f"실행 안 됨: 승인 실패({reject_reason}). preview를 확인해줘.",
    )
