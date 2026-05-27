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


# Queue of owner-approved self-improvement proposals awaiting execution.
# Phase 1 stops here (append-only audit + work queue); Phase 2 re-dispatches.
_QUEUE_PATH = Path(os.path.expanduser("~/.hermes/state/self-improvement-queue.jsonl"))

_ACTION_CLASS = "self_improvement"
_CONFIRM_VERB = "반영"
_TOKEN_TTL_SEC = 24 * 3600  # owner may not see the DM for a while — be generous


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


def approval_reply_text(proposal_id: str) -> str:
    """Owner-facing reply shown after approving a Phase 1 self-improvement card."""
    return (
        f"✅ 자가발전 제안 승인됨 — 실행 큐에 적재했어. proposal={proposal_id}\n"
        f"(재디스패치 자동 실행은 Phase 2에서 연결돼.)"
    )


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
