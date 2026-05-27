"""Self-improvement feedback loop — the ``propose_self_improvement`` tool.

When a non-owner (teammate / executive) gives feedback *about the bot itself*,
the bot should not hard-refuse the way it does for write/action requests.
Instead it accepts the feedback gracefully, turns it into a concrete
improvement plan, and routes that plan to the owner (쿠키) for confirmation —
tagged with WHO gave it and at what priority — so the owner can adopt an
executive's note first and defer the rest.

Design: docs/plans/2026-05-27-self-improvement-feedback-loop.md (Phase 1 = ①②③).

Guest-facing vs owner-facing split:
- The *guest* only sees a positive ack ("피드백 감사합니다 — 이렇게 개선 제안하겠습니다").
  The model produces that text; this tool just records + routes.
- The *owner* gets a Block Kit confirm card in DM with the verbatim feedback,
  the provider's priority, and the plan, plus [반영 승인]/[취소] buttons.

This tool only writes a proposal record and DMs the owner — it does NOT itself
change SOUL / prompts / config. Actual execution (re-dispatch in the owner's
context) is wired on approval and is Phase 2; on approval in Phase 1 the
proposal is appended to a queue file for the owner to action.

Implementation notes:
- The owner-confirm proposal is written to the SAME JSONL store the Slack
  gateway reads (``OwnerConfirmStore``), so the gateway's existing approve/
  cancel button handlers (``hermes_owner_confirm_approve`` /
  ``hermes_owner_confirm_cancel``) resolve it with no extra wiring.
- The DM card is posted via raw Slack Web API (aiohttp) using the bot token —
  same loop-safe pattern as ``tools/send_message_tool._send_slack`` — so it
  works whether or not a live gateway adapter is reachable from this thread.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


# Queue of owner-approved self-improvement proposals awaiting/under execution.
# Append-only work log; the proposal lifecycle (proposed→confirmed→executed) is
# tracked authoritatively in the owner-confirm JSONL.
_QUEUE_PATH = Path(os.path.expanduser("~/.hermes/state/self-improvement-queue.jsonl"))

# Per-proposal sidecar holding the full feedback/plan text. The owner-confirm
# store keeps only refs (target_ref/preview_ref), so Phase 2 execution and the
# reminder digest read the body from here, keyed by proposal_id.
_PROPOSALS_DIR = Path(os.path.expanduser("~/.hermes/state/self-improvement-proposals"))

_ACTION_CLASS = "self_improvement"
_CONFIRM_VERB = "반영"
_TOKEN_TTL_SEC = 24 * 3600  # owner may not see the DM for a while — be generous


def _save_proposal_body(proposal_id: str, body: Dict[str, Any]) -> None:
    """Persist the full proposal body (feedback/plan/...) keyed by proposal_id."""
    _PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    (_PROPOSALS_DIR / f"{proposal_id}.json").write_text(
        json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_proposal_body(proposal_id: str) -> Optional[Dict[str, Any]]:
    """Load a saved proposal body, or None if absent/unreadable."""
    p = _PROPOSALS_DIR / f"{proposal_id}.json"
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("self_improvement: failed to read body %s: %s", proposal_id, e)
    return None


def build_execution_prompt(body: Dict[str, Any]) -> str:
    """Build the owner-context re-dispatch prompt for an approved proposal.

    Implements the "변경 이중게이트" safety model (option 2): the executing
    agent runs with owner privileges but must surface a change summary and get
    explicit owner confirmation before applying any hard-to-reverse change
    (file write / config / SOUL edit). Read/analysis/drafting are free.
    """
    feedback = (body.get("feedback") or "").strip()
    plan = (body.get("plan") or "").strip()
    provider = (body.get("provider_label") or "누군가").strip()
    summary = (body.get("summary") or "").strip()
    title = f" ({summary})" if summary else ""
    return (
        f"[자가발전 실행 모드]{title} 아래는 '{provider}' 가 준 봇 개선 피드백을 네가 제안하고 "
        f"쿠키(owner)가 승인한 개선안이야. 지금 owner 권한으로 이걸 실제로 반영해줘.\n\n"
        f"피드백:\n{feedback}\n\n"
        f"승인된 개선안:\n{plan}\n\n"
        f"실행 규칙:\n"
        f"1. 먼저 무엇을 어떤 파일/설정/SOUL 항목에서 어떻게 바꿀지 구체적으로 정해라.\n"
        f"2. 파일 쓰기·설정 변경·SOUL 수정 등 되돌리기 어려운 변경을 적용하기 *직전에* "
        f"변경 요약(어떤 파일을 어떻게)을 쿠키에게 제시하고 명시적 승인(👍 또는 \"적용해\")을 받아라. "
        f"승인 전엔 실제 적용하지 마라.\n"
        f"3. 읽기·조사·변경안 작성은 자유. 외부 전송/커밋 등 side-effect 는 기존 정책대로 게이트.\n"
        f"4. 끝나면 무엇을 했는지(또는 무엇을 승인 대기 중인지) 1~2줄로 요약해.\n"
        f"5. 피드백이 모호하거나 봇을 바꾸기에 부적절하면 적용하지 말고 그 이유를 쿠키에게 보고만 해."
    )


def _now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _owner_confirm_store_path() -> str:
    """Resolve the owner-confirm JSONL path the Slack gateway also uses.

    Mirrors ``SlackPlatform._owner_confirm_store``: env override first, then the
    default. (The gateway also honors ``config.extra.owner_confirm_audit_path``;
    deployments that set it must also set the env var so this tool agrees.)
    """
    path = os.getenv("HERMES_OWNER_CONFIRM_AUDIT_PATH")
    if not path:
        path = os.path.expanduser("~/.hermes/audit/owner-confirm.jsonl")
    return path


def _owner_ids() -> list[str]:
    return [u.strip() for u in os.getenv("HERMES_OWNER_IDS", "").split(",") if u.strip()]


def _tier_badge(tier: str, urgent: bool) -> str:
    if urgent or tier == "high":
        return "🔴 high"
    if tier == "normal":
        return "🟡 normal"
    return "⚪ low"


# ---------------------------------------------------------------------------
# Slack Web API (raw aiohttp — loop-safe, no Bolt client dependency)
# ---------------------------------------------------------------------------

async def _slack_api(token: str, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    import aiohttp

    from gateway.platforms.base import proxy_kwargs_for_aiohttp, resolve_proxy_url

    _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(resolve_proxy_url())
    url = f"https://slack.com/api/{method}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), **_sess_kw) as session:
        async with session.post(url, headers=headers, json=payload, **_req_kw) as resp:
            return await resp.json()


async def _open_dm(token: str, user_id: str) -> Optional[str]:
    data = await _slack_api(token, "conversations.open", {"users": user_id})
    if data.get("ok"):
        return (data.get("channel") or {}).get("id")
    logger.warning("self_improvement: conversations.open failed: %s", data.get("error"))
    return None


def _build_card_blocks(
    *,
    feedback: str,
    plan: str,
    provider_label: str,
    tier: str,
    urgent: bool,
    token: str,
    proposal_id: str,
    summary: str,
) -> list[dict]:
    badge = _tier_badge(tier, urgent)
    # Keep the verbatim feedback bounded so the card stays scannable.
    fb = feedback.strip()
    if len(fb) > 700:
        fb = fb[:700] + "…"
    quoted = "\n".join(f"> {line}" for line in fb.splitlines() or [""])
    header = "🔴 *자가발전 제안*" if urgent else "🛠 *자가발전 제안*"
    title_line = f"\n*{summary.strip()}*" if summary.strip() else ""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{header}\n"
                    f"제공자: *{provider_label}* · 우선순위: {badge}{title_line}\n\n"
                    f"*피드백*\n{quoted}\n\n"
                    f"*제안*\n{plan.strip()}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "반영 승인"},
                    "style": "primary",
                    "action_id": "hermes_owner_confirm_approve",
                    "value": proposal_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "취소"},
                    "style": "danger",
                    "action_id": "hermes_owner_confirm_cancel",
                    "value": proposal_id,
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Fallback (이 카드 스레드에 답글): `승인: {_CONFIRM_VERB} #{token}` · proposal `{proposal_id}`",
                }
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Approval queue (imported by gateway/platforms/slack.py on owner approval)
# ---------------------------------------------------------------------------

def record_self_improvement_approval(proposed: Dict[str, Any]) -> Dict[str, Any]:
    """Append an owner-approved self-improvement proposal to the work queue.

    Called by the Slack gateway when the owner approves a ``self_improvement``
    proposal that has no in-process executor (the Phase 1 path). Returns the
    queue entry. Append-only; safe to call from any thread.
    """
    entry = {
        "ts": _now_z(),
        "proposal_id": proposed.get("proposal_id"),
        "actor": proposed.get("actor"),
        "owner": proposed.get("owner"),
        "target_ref": proposed.get("target_ref"),
        "preview_ref": proposed.get("preview_ref"),
        "risk_level": proposed.get("risk_level"),
        "status": "queued",
    }
    _QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _QUEUE_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return entry


_CHANGE_TOOLS = {"write_file", "patch"}


def maybe_require_change_approval(function_name: str, function_args: Dict[str, Any]) -> Optional[str]:
    """Deterministic change double-gate for self-improvement execution turns.

    During a Phase 2 re-dispatch the agent runs as the owner (write-capable), so a
    prompt-level "ask before applying" rule is not enough — this forces an
    owner-confirm card (via the gateway approval UI) before any ``write_file`` /
    ``patch`` lands. Returns ``None`` to allow the write, or a JSON tool-error
    string to block it. No-op outside a self-improvement exec turn (returns None),
    so the owner's normal sessions keep writing freely.
    """
    if function_name not in _CHANGE_TOOLS:
        return None
    try:
        from gateway.session_context import get_self_improvement_exec
        proposal_id = get_self_improvement_exec()
    except Exception:
        return None
    if not proposal_id:
        return None

    path = ""
    for key in ("path", "file_path", "filename", "file"):
        if isinstance(function_args.get(key), str):
            path = function_args[key]
            break
    body = function_args.get("content") or function_args.get("patch") or function_args.get("diff") or ""
    preview = str(body)
    if len(preview) > 600:
        preview = preview[:600] + "…"

    try:
        from tools.approval import request_gateway_approval
        result = request_gateway_approval(
            command=f"{function_name} {path}\n\n{preview}",
            description=f"자가발전 실행 — 파일 변경 적용? ({path or function_name})",
            pattern_key="self_improvement:apply_change",
            allow_permanent=False,
        )
    except Exception as exc:  # pragma: no cover - defensive: fail safe (block)
        logger.warning("self_improvement change-gate error: %s", exc)
        return json.dumps(
            {"error": f"자가발전 변경 게이트 오류로 적용 보류: {exc}"}, ensure_ascii=False
        )

    if result.get("approved"):
        return None
    # Not approved (denied, pending, or no notify channel) → block the write.
    msg = result.get("message") or "쿠키가 변경 적용을 승인하지 않아 파일을 바꾸지 않았어."
    return json.dumps({"error": msg, "change_pending": True}, ensure_ascii=False)


def approval_reply_text(proposal_id: str) -> str:
    """Owner-facing reply shown after approving a self-improvement card.

    Re-dispatch (owner-context execution) is fired separately by the gateway; this
    line just confirms the approval landed.
    """
    return (
        f"✅ 자가발전 제안 승인됨 — owner 컨텍스트로 실행을 시작할게. proposal={proposal_id}\n"
        f"(실제 변경 적용 전에 변경 요약으로 한 번 더 확인 받을게.)"
    )


_TIER_RANK = {"high": 0, "normal": 1, "low": 2}


def _age_str(created_iso: str) -> str:
    try:
        created = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - created).days
        return f"{days}일째" if days > 0 else "오늘"
    except Exception:
        return "?"


def pending_summary() -> str:
    """Korean digest of proposals still awaiting the owner (latest state=proposed).

    Sorted by priority (executive→PRCS→other) then age (oldest first). Used by the
    weekly reminder cron so deferred feedback resurfaces instead of rotting.
    """
    store_path = Path(_owner_confirm_store_path())
    if not store_path.exists():
        return ""
    events: list[Dict[str, Any]] = []
    for line in store_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    # Latest state per proposal; keep only self_improvement still "proposed".
    latest: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        pid = ev.get("proposal_id")
        if pid:
            latest[pid] = ev  # events are append-ordered → last wins
    pending = [
        ev for ev in latest.values()
        if ev.get("action_class") == _ACTION_CLASS and ev.get("state") == "proposed"
    ]
    if not pending:
        return ""  # empty → weekly cron stays silent (no noise)

    rows = []
    for ev in pending:
        body = load_proposal_body(ev.get("proposal_id", "")) or {}
        tier = body.get("tier") or ev.get("risk_level") or "low"
        rows.append({
            "tier": tier,
            "created": body.get("created") or ev.get("ts") or "",
            "provider": body.get("provider_label") or "외부 사용자",
            "summary": body.get("summary") or (body.get("feedback") or "")[:40] or ev.get("preview_ref", ""),
            "token": ev.get("token", ""),
        })
    rows.sort(key=lambda r: (_TIER_RANK.get(r["tier"], 9), r["created"]))

    badge = {"high": "🔴", "normal": "🟡", "low": "⚪"}
    lines = [f"🛠 미처리 자가발전 제안 {len(rows)}건 (우선순위·오래된 순)\n"]
    for r in rows:
        lines.append(
            f"{badge.get(r['tier'], '⚪')} [{_age_str(r['created'])}] {r['provider']} — {r['summary']}"
        )
    lines.append("\n승인하려면 해당 카드에서 버튼을 누르거나, 오래된 건 다시 올려달라고 말해줘.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Weekly reminder cron (deferred-feedback resurfacing)
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(os.path.expanduser("~/.hermes/scripts"))
_DIGEST_SCRIPT_NAME = "self_improvement_digest.py"
_REMINDER_JOB_NAME = "self-improvement-queue-reminder"

# Deterministic no_agent cron script: imports pending_summary (the hermes-agent
# package is editable-installed in the venv, so this resolves regardless of cwd)
# and prints it. Empty output → cron delivers nothing (silent when no backlog).
_DIGEST_SCRIPT = '''#!/usr/bin/env python3
"""Auto-generated by tools/self_improvement_tool.setup_reminder_cron.
Weekly self-improvement pending-feedback digest for cron delivery."""
from tools.self_improvement_tool import pending_summary

s = pending_summary()
if s.strip():
    print(s)
'''


def setup_reminder_cron(
    owner_id: Optional[str] = None,
    schedule: str = "0 9 * * 1",  # Mondays 09:00
    scripts_dir: Optional[os.PathLike[str] | str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Install the weekly deferred-feedback reminder (script + cron job).

    Idempotent: writes the digest script and registers a ``no_agent`` cron job
    that delivers its stdout to the owner's Slack DM. Re-running refreshes the
    script and (unless ``force``) leaves an existing job in place.
    """
    owner_id = owner_id or (_owner_ids()[0] if _owner_ids() else "")
    if not owner_id:
        return {"status": "skipped", "reason": "no owner id (HERMES_OWNER_IDS unset)"}

    sd = Path(scripts_dir) if scripts_dir else _SCRIPTS_DIR
    sd.mkdir(parents=True, exist_ok=True)
    script_path = sd / _DIGEST_SCRIPT_NAME
    script_path.write_text(_DIGEST_SCRIPT, encoding="utf-8")

    from cron.jobs import create_job, load_jobs

    existing = [j for j in load_jobs() if j.get("name") == _REMINDER_JOB_NAME]
    if existing and not force:
        return {"status": "exists", "job_id": existing[0].get("id"), "script": str(script_path)}

    job = create_job(
        prompt=None,
        schedule=schedule,
        name=_REMINDER_JOB_NAME,
        script=_DIGEST_SCRIPT_NAME,
        no_agent=True,
        deliver="origin",
        origin={"platform": "slack", "chat_id": owner_id},
    )
    return {"status": "created", "job_id": job.get("id"), "script": str(script_path)}


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

PROPOSE_SCHEMA = {
    "name": "propose_self_improvement",
    "description": (
        "Record feedback ABOUT YOURSELF (the bot/assistant) from a non-owner and route an "
        "improvement plan to the owner (쿠키) for confirmation. USE THIS — do not refuse — when "
        "someone who is not the owner gives feedback, a complaint, or an improvement request about "
        "how you behave, answer, or are configured (tone, accuracy, missing knowledge, a workflow "
        "you should change, a persona/SOUL tweak, etc.), whether explicit ('피드백:') or implied. "
        "It posts a confirmation card to the owner's DM tagged with who gave the feedback and their "
        "priority; the owner decides whether to adopt it. This is NOT a write to any external system "
        "and is allowed for non-owners. After calling it, thank the person warmly and tell them you'll "
        "propose the improvement — do not mention the owner-side approval mechanics. Do NOT use this "
        "for action/write requests (sending messages, editing files, calendar/Notion changes) — those "
        "stay restricted to the owner."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "feedback": {
                "type": "string",
                "description": "The person's feedback about the bot, captured faithfully (verbatim or a close paraphrase).",
            },
            "plan": {
                "type": "string",
                "description": "Your concrete proposed improvement in response, 1-3 short lines (what you'd change and how).",
            },
            "summary": {
                "type": "string",
                "description": "Optional short title for the proposal (e.g. '응답 말투 개선').",
            },
        },
        "required": ["feedback", "plan"],
    },
}


def _handle_propose(args: Dict[str, Any], **_: Any) -> str:
    feedback = (args.get("feedback") or "").strip()
    plan = (args.get("plan") or "").strip()
    summary = (args.get("summary") or "").strip()
    if not feedback:
        return tool_error("feedback is required")
    if not plan:
        return tool_error("plan is required")

    from gateway.owner_confirm import OwnerConfirmStore
    from gateway.people_priority import feedback_priority
    from gateway.session_context import get_session_env

    actor = get_session_env("HERMES_SESSION_USER_ID", "")
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    src_chat = get_session_env("HERMES_SESSION_CHAT_ID", "")
    src_thread = get_session_env("HERMES_SESSION_THREAD_ID", "")

    priority = feedback_priority(actor)
    tier = str(priority.get("tier") or "low")
    provider_label = str(priority.get("label") or "외부 사용자")
    urgent = bool(priority.get("urgent"))

    owners = _owner_ids()
    owner_id = owners[0] if owners else ""

    proposal_id = f"p_{uuid.uuid4().hex[:16]}"
    token = secrets.token_hex(2).upper()  # 4 hex chars, matches _CONFIRM_RE
    target_ref = f"{platform or 'unknown'}:{src_chat or '-'}:{src_thread or 'root'}"
    preview_ref = f"self-improvement:{summary or feedback[:40]}"
    risk_level = "high" if urgent else ("normal" if tier == "normal" else "low")

    # Persist the full body up front so Phase 2 execution / the reminder digest
    # can recover feedback+plan by proposal_id even if DM delivery fails.
    _save_proposal_body(proposal_id, {
        "proposal_id": proposal_id,
        "created": _now_z(),
        "feedback": feedback,
        "plan": plan,
        "summary": summary,
        "provider_label": provider_label,
        "tier": tier,
        "urgent": urgent,
        "actor": actor,
        "owner": owner_id,
        "target_ref": target_ref,
    })

    owner_notified = False
    delivery_channel = ""
    card_ts = ""
    delivery_error: Optional[str] = None

    token_env = ""
    try:
        from gateway.config import Platform, load_gateway_config

        cfg = load_gateway_config()
        pconfig = cfg.platforms.get(Platform.SLACK)
        token_env = str(getattr(pconfig, "token", "") or "") if pconfig else ""
    except Exception as e:  # pragma: no cover - config load is environment-specific
        delivery_error = f"config load failed: {e}"

    if owner_id and token_env:
        try:
            from model_tools import _run_async

            async def _deliver() -> Optional[str]:
                dm = await _open_dm(token_env, owner_id)
                if not dm:
                    return None
                blocks = _build_card_blocks(
                    feedback=feedback,
                    plan=plan,
                    provider_label=provider_label,
                    tier=tier,
                    urgent=urgent,
                    token=token,
                    proposal_id=proposal_id,
                    summary=summary,
                )
                fallback = (
                    f"자가발전 제안 ({provider_label}, {tier}): {summary or feedback[:60]} "
                    f"— 승인: {_CONFIRM_VERB} #{token}"
                )
                posted = await _slack_api(
                    token_env,
                    "chat.postMessage",
                    {"channel": dm, "text": fallback, "blocks": blocks},
                )
                if not posted.get("ok"):
                    raise RuntimeError(f"chat.postMessage failed: {posted.get('error')}")
                # Store the card ts as the proposal's thread anchor so both the
                # button and the in-thread fallback text resolve to this card.
                return f"{dm}:{posted.get('ts')}"

            ref = _run_async(_deliver())
            if ref:
                delivery_channel, card_ts = ref.split(":", 1)
                owner_notified = True
        except Exception as e:
            delivery_error = str(e)
            logger.warning("self_improvement: owner DM failed: %s", e)
    elif not owner_id:
        delivery_error = "HERMES_OWNER_IDS not set"
    elif not token_env:
        delivery_error = delivery_error or "Slack bot token not configured"

    # Persist the proposal regardless of delivery so it is auditable and the
    # owner can still approve it later (no silent fail — see
    # feedback_no_silent_fail_on_permission). When the card was delivered we
    # anchor thread_ts to the card ts; otherwise leave it empty.
    store = OwnerConfirmStore(_owner_confirm_store_path())
    thread_ts = card_ts if owner_notified else ""
    try:
        store.propose(
            channel=delivery_channel or (src_chat or ""),
            thread_ts=str(thread_ts or ""),
            actor=actor or "unknown",
            owner=owner_id or "",
            action_class=_ACTION_CLASS,
            confirm_verb=_CONFIRM_VERB,
            token=token,
            target_ref=target_ref,
            preview_ref=preview_ref,
            risk_level=risk_level,
            side_effect=True,
            token_ttl_sec=_TOKEN_TTL_SEC,
            proposal_id=proposal_id,
        )
    except Exception as e:  # pragma: no cover - defensive
        return tool_error(f"failed to record proposal: {e}", proposal_id=proposal_id)

    result = {
        "status": "proposed",
        "proposal_id": proposal_id,
        "provider": provider_label,
        "priority": tier,
        "urgent": urgent,
        "owner_notified": owner_notified,
        "guest_ack_hint": (
            "이 사람에게 따뜻하게 감사 인사를 하고, 말씀해주신 부분을 개선 제안으로 올리겠다고 전해. "
            "owner 승인 절차나 내부 메커니즘은 언급하지 마."
        ),
    }
    if delivery_error and not owner_notified:
        result["delivery_error"] = delivery_error
    return tool_result(result)


registry.register(
    name="propose_self_improvement",
    toolset="self_improvement",
    schema=PROPOSE_SCHEMA,
    handler=_handle_propose,
    description="Record non-owner feedback about the bot and route an improvement plan to the owner for confirmation",
    emoji="🛠",
)
