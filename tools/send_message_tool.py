"""Send Message Tool -- cross-channel messaging via platform APIs (send, list targets,
react, edit); works in both CLI and gateway contexts."""

import asyncio
import json
import logging
import os
import re
from functools import partial
from typing import Any, Dict, Optional

from agent.secret_scope import get_secret

logger = logging.getLogger(__name__)

from tools.send_message_targets import _HOME_CHANNEL_ENV_OVERRIDES, _SLACK_USER_ID_RE, resolve_send_target
from tools.send_message_senders import (
    _AUDIO_EXTS, _DEFAULT_CAPTION_LIMIT, _IMAGE_EXTS, _NO_DELIVERABLE, _VIDEO_EXTS, _VOICE_EXTS,
    _adapter_media_method, _error, _live_adapter, _media_caption_split, _plugin_standalone_sender,
    _registry_standalone_send, _resolve_slack_user_target, _sanitize_error_text, _send_bluebubbles,
    _send_matrix_via_adapter, _send_qqbot, _send_signal, _send_telegram, _send_weixin, _send_yuanbao)
from tools.registry import registry, tool_error

# NOTE (upstream): upstream intentionally does NOT register ``send_message`` as an
# agent-callable model tool. For the Cookie alter deployment we DO register both
# ``send_message`` and ``update_message`` (see the registry block at the bottom) —
# the alter is a personal proxy that sends/edits on Cookie's behalf under the
# W2/W3/W4 authorization gates below. Keep this override in mind on future rebases.
# The same helpers are also the shared transport for cron delivery, the ``hermes
# send`` CLI, the kanban notifier and the opt-in MCP server.

# Cookie overlay: broader mention match (also accepts W... workspace mentions) than
# upstream's narrower _SLACK_MENTION_RE, which only feeds the ``user:`` target form.
_SLACK_MENTION_TARGET_RE = re.compile(r"^\s*<@([UW][A-Z0-9]{8,})(?:\|[^>]+)?>\s*$")
_SLACK_USER_TOKEN_FOOTER = "— sent by Cookie via cookie.hermes"
_SLACK_BOT_ACCESS_ERRORS = frozenset({"channel_not_found", "not_in_channel"})


def prepare_send_message_platforms() -> None:
    """Load enabled standalone plugins before tool schemas/cache keys are built."""
    from hermes_cli.plugins import discover_plugins
    discover_plugins()


def send_message_tool(args, **kw):
    """Handle cross-channel send_message tool calls."""
    action = args.get("action", "send")
    if action == "list":
        return _handle_list()
    if action in ("react", "unreact"):
        return _handle_react(args, remove=action == "unreact")
    return _handle_send(args)


def _resolve_tool_target(target: str, *, pass_unresolved_references: bool = False):
    """``(platform_name, chat_id, thread_id, error)``; ``chat_id`` is None when no ref was given
    (caller falls back to the home channel)."""
    platform_name, _, target_ref = target.partition(":")
    platform_name, target_ref = platform_name.strip().lower(), target_ref.strip() or None
    prepare_send_message_platforms()
    if not target_ref:
        return platform_name, None, None, None
    return platform_name, *resolve_send_target(platform_name, target_ref,
                                               pass_unresolved_references=pass_unresolved_references)


def _handle_list():
    try:
        from gateway.channel_directory import format_directory_for_display
        return json.dumps({"targets": format_directory_for_display()})
    except Exception as e:
        return json.dumps(_error(f"Failed to load channel directory: {e}"))


_TOKEN_UNSET = object()


def _authorize_relay_target(platform_name: str, chat_id, thread_id=None, *,
                            native_token=_TOKEN_UNSET) -> str | None:
    """Relay egress-authorization guard (P5a); None when the send may proceed.

    Thin delegate to ``gateway.relay.egress`` so the tool keeps working in
    environments where the gateway package can't be imported.

    THE TWO FAILURES ARE NOT THE SAME, and conflating them disabled the
    boundary. A missing gateway module means there is no relay egress to
    authorize, so proceeding is correct. A fault INSIDE the guard means
    authorization did not happen — and returning None there means "authorized",
    so a single runtime bug in the guard silently switched the whole P5(a)
    boundary off. Review found this by making the guard raise and watching the
    send go through.

    So: the import is tolerated, the CALL is not. A guard that cannot answer
    refuses, which is the only safe polarity for an authorization check.
    """
    try:
        from gateway.relay.egress import authorize_relay_target
    except ImportError as exc:
        # ABSENCE ONLY, and absence means the gateway relay module ITSELF is
        # missing — `exc.name` says which module was not found. An ImportError
        # naming a NESTED dependency is a broken installation, i.e. a fault,
        # and returning None here means "authorized". Review probed exactly
        # that (`ImportError.name = "gateway.relay.dependency"`) and got an
        # authorized verdict, so `except ImportError` alone was still fail-open.
        # ABSENCE has one shape and it is checkable: a genuinely missing module
        # raises ModuleNotFoundError with `.name` set to the module that was not
        # found (verified: `import gateway.relay.x` -> ModuleNotFoundError,
        # name="gateway.relay.x"). So a plain ImportError, or a nameless one, is
        # an unattributable FAULT — never proof that there is no relay here.
        # I previously admitted the nameless case to protect the CLI/cron path;
        # that reasoning was wrong, because that path does not produce one.
        _missing = getattr(exc, "name", None)
        if not isinstance(exc, ModuleNotFoundError) or _missing not in (
            "gateway",
            "gateway.relay",
            "gateway.relay.egress",
        ):
            logger.exception(
                "relay egress module failed to import for %s — refusing the send",
                platform_name,
            )
            return (
                f"Refusing to send to relay target '{platform_name}': the egress "
                "authorization module could not be loaded, so this destination "
                "could not be verified."
            )
        logger.debug("relay target authorization unavailable", exc_info=True)
        return None
    except Exception:  # noqa: BLE001 - the module is THERE and broke; FAIL CLOSED
        logger.exception(
            "relay egress module failed to import for %s — refusing the send",
            platform_name,
        )
        return (
            f"Refusing to send to relay target '{platform_name}': the egress "
            "authorization module could not be loaded, so this destination "
            "could not be verified."
        )

    try:
        # ONE SNAPSHOT. `native_token` is the token from the SAME pconfig the
        # dispatch below will actually send with. Letting the guard reload
        # config independently allowed a transition where authorization saw a
        # connector-only setup (exemption granted) while dispatch still held a
        # native token and sent the unattested handle itself.
        if native_token is _TOKEN_UNSET:
            # A caller that forgets the snapshot must NOT silently look like
            # "no native token", which would grant the @handle exemption.
            return authorize_relay_target(platform_name, chat_id, thread_id)
        return authorize_relay_target(
            platform_name, chat_id, thread_id, native_token=native_token
        )
    except Exception:  # noqa: BLE001 - the guard faulted; FAIL CLOSED
        logger.exception(
            "relay target authorization FAILED for %s — refusing the send",
            platform_name,
        )
        return (
            f"Refusing to send to relay target '{platform_name}': the egress "
            "authorization check failed, so this destination could not be "
            "verified. This is a bug — the send was blocked rather than "
            "allowed through unchecked."
        )


def _handle_react(args, remove=False):
    """Attach (``remove=True``: retract) an emoji reaction via the live gateway adapter; no
    standalone fallback because reacting needs the adapter's live message-id state."""
    target, emoji = args.get("target", ""), (args.get("emoji") or "").strip()
    message_id = (args.get("message_id") or "").strip() or None
    if not target or (not remove and not emoji):
        return tool_error("'target' is required when action='unreact'" if remove
                          else "Both 'target' and 'emoji' are required when action='react'")

    # Platform-native ids (e.g. photon GUIDs) match no parser/directory entry; the adapter validates.
    platform_name, chat_id, _thread_id, resolution_error = _resolve_tool_target(target, pass_unresolved_references=True)
    if resolution_error:
        return tool_error(resolution_error)
    platform, err = _platform_enum(platform_name)
    if err:
        return tool_error(err)
    if not chat_id:
        try:
            from gateway.config import load_gateway_config
            chat_id = load_gateway_config().get_home_channel(platform).chat_id
        except Exception:
            return tool_error(f"No chat specified and no home channel set for {platform_name}. "
                              f"Use '{platform_name}:chat_id'.")
    # P5(a): same egress-authorization floor as the send path — a reaction is
    # an outbound act against a named destination, so an unattested relay
    # target must be refused here too, not just on `send`.
    # The react path has no pconfig snapshot of its own; it dispatches through
    # the LIVE adapter below, never through a native token, so the guard does
    # its own credential probe here.
    _relay_denial = _authorize_relay_target(platform_name, chat_id, _thread_id)
    if _relay_denial:
        return tool_error(_relay_denial)

    _, adapter = _live_adapter(platform)
    if adapter is None:
        return tool_error(f"Reactions require a live {platform_name} adapter in the running "
                          "gateway (not available from cron/standalone contexts).")
    react_fn = getattr(adapter, "remove_reaction" if remove else "add_reaction", None)
    if not callable(react_fn):
        return tool_error(f"Platform '{platform_name}' does not support message reactions.")
    try:
        from model_tools import _run_async
        result = _run_async(react_fn(chat_id=chat_id, message_id=message_id, **({} if remove else {"emoji": emoji})))
    except Exception as e:
        return json.dumps(_error(f"Reaction failed: {e}"))
    return json.dumps(result if isinstance(result, dict) else {"success": bool(result)})


# --- Cookie: Slack person targets ("<@U...>", "로이봉 이사님") ---------------------------


def _normalize_slack_person_query(value: str) -> str:
    """Normalize a human-entered Slack person target for matching."""
    text = (value or "").strip()
    mention = _SLACK_MENTION_TARGET_RE.fullmatch(text)
    if mention:
        return mention.group(1).lower()
    text = text.lstrip("@").strip().lower()
    for suffix in (
        "이사님", "본부장님", "대표님", "팀장님", "파트장님", "대리님", "과장님", "차장님", "님",
        "이사", "본부장", "대표", "팀장", "파트장", "대리", "과장", "차장",
    ):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            break
    return re.sub(r"[\s._\-]+", "", text)


def _slack_user_match_fields(user: Dict[str, Any]) -> list[str]:
    profile = user.get("profile") or {}
    raw_values = [
        user.get("id"),
        user.get("name"),
        user.get("real_name"),
        profile.get("display_name"),
        profile.get("real_name"),
        profile.get("first_name"),
        profile.get("last_name"),
    ]
    fields: list[str] = []
    for value in raw_values:
        if value:
            fields.append(_normalize_slack_person_query(str(value)))
    first = str(profile.get("first_name") or "").strip()
    last = str(profile.get("last_name") or "").strip()
    if first and last:
        fields.append(_normalize_slack_person_query(f"{first}{last}"))
        fields.append(_normalize_slack_person_query(f"{last}{first}"))
    return [field for field in fields if field]


async def _resolve_slack_user_id_via_api(token: str, target_ref: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve a Slack person target to a U/W user ID using Slack Web API."""
    query = (target_ref or "").strip()
    mention = _SLACK_MENTION_TARGET_RE.fullmatch(query)
    if mention:
        return mention.group(1), None
    if re.fullmatch(r"[UW][A-Z0-9]{8,}", query):
        return query, None

    normalized = _normalize_slack_person_query(query)
    if not normalized:
        return None, "empty Slack user target"

    try:
        import aiohttp
    except Exception as exc:
        return None, f"aiohttp unavailable for Slack user lookup: {exc}"

    url = "https://slack.com/api/users.list"
    headers = {"Authorization": f"Bearer {token}"}
    matches: list[Dict[str, Any]] = []
    cursor: Optional[str] = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            for _page in range(20):
                params = {"limit": "200"}
                if cursor:
                    params["cursor"] = cursor
                async with session.get(url, headers=headers, params=params) as resp:
                    data = await resp.json()
                if not data.get("ok"):
                    return None, f"Slack users.list failed: {data.get('error', 'unknown')}"
                for user in data.get("members", []):
                    if user.get("deleted") or user.get("is_bot"):
                        continue
                    fields = _slack_user_match_fields(user)
                    if normalized in fields or any(field.startswith(normalized) for field in fields):
                        matches.append(user)
                cursor = (data.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
    except Exception as exc:
        return None, f"Slack user lookup failed: {exc}"

    exact = [user for user in matches if normalized in _slack_user_match_fields(user)]
    candidates = exact or matches
    unique: Dict[str, Dict[str, Any]] = {str(user.get("id")): user for user in candidates if user.get("id")}
    if len(unique) == 1:
        return next(iter(unique.keys())), None
    if len(unique) > 1:
        labels = []
        for user in list(unique.values())[:5]:
            profile = user.get("profile") or {}
            label = (profile.get("real_name") or profile.get("display_name") or user.get("real_name")
                     or user.get("name") or user.get("id"))
            labels.append(str(label))
        return None, "Ambiguous Slack user target; use @mention or user ID. Candidates: " + ", ".join(labels)
    return None, f"Could not resolve Slack user '{target_ref}'. Use @mention or U... user ID."


async def _open_slack_dm_channel(token: str, user_id: str) -> Optional[str]:
    """Open or fetch a Slack DM conversation ID for a user ID."""
    import aiohttp

    url = "https://slack.com/api/conversations.open"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.post(url, headers=headers, json={"users": [user_id]}) as resp:
            data = await resp.json()
            if data.get("ok"):
                return data["channel"]["id"]
            return None


# --- Cookie: send authorization (W1 audit / W2 owner / W3 on-behalf / W4 delegation) ----


def _audit_side_effect(**fields) -> None:
    """Best-effort write to the W1 side-effect ledger; never fails the caller."""
    try:
        from gateway.side_effect_audit import record_side_effect
        record_side_effect(**fields)
    except Exception:
        logger.debug("side-effect audit write failed", exc_info=True)


def _session_actor() -> tuple[str, bool, bool]:
    """``(actor_uid, session_ful, actor_is_owner)`` for the current gateway session.

    Raises rather than guessing: an unreadable session context must not resolve to
    "owner" (the gates below fail closed on the exception)."""
    from gateway.session_context import get_session_env
    actor_uid = get_session_env("HERMES_SESSION_USER_ID", "")
    session_ful = bool(actor_uid and get_session_env("HERMES_SESSION_CHAT_ID", ""))
    owner_ids = {u.strip() for u in os.getenv("HERMES_OWNER_IDS", "").split(",") if u.strip()}
    return actor_uid, session_ful, bool(actor_uid) and actor_uid in owner_ids


def _describe_actor(actor_uid: str) -> str:
    """Resolve a requester's Slack id to a human label (people roster), else id."""
    if not actor_uid:
        return "알 수 없는 사용자"
    try:
        from gateway.people_priority import feedback_priority
        label = (feedback_priority(actor_uid) or {}).get("label")
        if label and label != "외부 사용자":
            return f"{label} ({actor_uid})"
    except Exception:
        pass
    return actor_uid


def _escalate_on_behalf_to_owner(config, *, actor_label, platform_name, target_ref, preview) -> bool:
    """Best-effort: DM the owner that an on-behalf send was blocked. Returns sent?.

    The W1 ledger record is the durable trail; this DM is a courtesy heads-up.
    """
    try:
        from gateway.config import Platform
        owner_ids = [u.strip() for u in os.getenv("HERMES_OWNER_IDS", "").split(",") if u.strip()]
        if not owner_ids:
            return False
        owner_id = owner_ids[0]
        slack_cfg = config.platforms.get(Platform.SLACK) if config else None
        token = str(getattr(slack_cfg, "token", "") or "") if slack_cfg else ""
        if not token:
            return False
        from model_tools import _run_async
        from tools.self_improvement_tool import _open_dm, _slack_api

        text = (
            "🔐 대리 발송 승인 요청 (보류됨)\n"
            f"• 요청자: {actor_label}\n"
            f"• 대상: {platform_name}:{target_ref}\n"
            f"• 내용: {preview}\n\n"
            "타인 대신 발송이라 쿠키 승인 없이 보내지 않았습니다. 직접 보내거나 무시하세요."
        )

        async def _do() -> bool:
            dm = await _open_dm(token, owner_id)
            if not dm:
                return False
            posted = await _slack_api(token, "chat.postMessage", {"channel": dm, "text": text})
            return bool(posted.get("ok"))

        return bool(_run_async(_do()))
    except Exception:
        return False


def _block_on_behalf_send(*, config, actor_uid, platform_name, target_ref, message, media_files) -> str:
    """Block a non-owner's send/edit (on-behalf) and escalate to the owner. [W3]

    guardrails.md §4: sending on someone else's behalf requires explicit owner
    approval. Records the blocked attempt to the W1 ledger, best-effort DMs the
    owner, and returns a JSON block result — the send never fires.
    """
    actor_label = _describe_actor(actor_uid)
    preview = (message or "").strip() or _describe_media_for_mirror(media_files) or "(media only)"
    if len(preview) > 800:
        preview = preview[:800] + "…"

    _audit_side_effect(
        tool_name="send_message",
        action_class="send",
        source="agent",
        status="blocked",
        actor=actor_uid or None,
        target_ref=f"{platform_name}:{target_ref}"[:200],
        args={"requester": actor_label, "message": preview[:200]},
        blocked_reason="on_behalf_requires_owner_approval",
    )

    notified = _escalate_on_behalf_to_owner(
        config,
        actor_label=actor_label,
        platform_name=platform_name,
        target_ref=target_ref,
        preview=preview,
    )

    return json.dumps({
        "success": False,
        "blocked": True,
        "owner_approval_required": True,
        "error": (
            f"이건 {actor_label} 님이 요청한 '대리 발송'이라 쿠키(owner) 승인이 필요해 보내지 않았습니다. "
            + ("쿠키에게 확인 요청을 전달했습니다." if notified
               else "쿠키 DM 전달은 실패했지만 감사 원장에 기록했습니다.")
        ),
    }, ensure_ascii=False)


def _send_actor_gate(*, config, platform_name, target_ref, message, media_files):
    """W2 send authorization — ``(block_result | None, session_less, actor_uid)``.

    Slack sends are shared side effects. This is the single decision point; the
    alter-style owner-confirm token gate stays scoped to self_improvement adoption
    (gateway/owner_confirm.py), and the interactive native approval UI is used only
    for the Slack user-token fallback below.

    Axes (L1 in agent_init.py already strips send_message from non-owner/non-executive
    sessions, so only owner & executive reach here):
      • owner, session-ful     → SKIP gate. The owner authorizes by asking; prompting
        them to approve their own send is a meaningless self-loop. [W2, 6/4]
        (Audited by the W1 post-tool hook, source=agent.)
      • non-owner, session-ful → ON-BEHALF: a non-owner (e.g. an executive) using
        send_message is asking the bot to message a NEW external target on their
        behalf (in-thread replies don't use send_message — operating.md §13).
        guardrails.md §4 requires EXPLICIT owner approval, and an interactive gate
        would render in the REQUESTER's own session (self-approval loophole — the
        6/2 Eric→조이 case), so these are BLOCKED and escalated to the owner's DM,
        unless W4 matches a pre-registered delegation. [W3/W4, 6/4]
      • session-less (CLI ``hermes send``, MCP messages_send) → AUDIT-ONLY: no live
        session to prompt in. These run on the owner's own machine and bypass
        tool_executor (the W1 hook never sees them), so the caller emits the audit
        after the send. [W2, 6/4]
    """
    actor_uid, session_ful, actor_is_owner = _session_actor()
    session_less = not session_ful and os.environ.get("HERMES_CRON_SESSION") != "1"
    if not session_ful or actor_is_owner:
        return None, session_less, actor_uid

    # W4: an explicit, unexpired owner-granted delegation for this person+action in
    # context/delegations.yaml authorizes the on-behalf send. Empty registry → block.
    try:
        from gateway.delegations import match_delegation
        delegation = match_delegation(
            actor_uid=actor_uid,
            action_class="send",
            platform=platform_name,
            target=str(target_ref),
        )
    except Exception:
        delegation = None
    if delegation is None:
        return _block_on_behalf_send(
            config=config,
            actor_uid=actor_uid,
            platform_name=platform_name,
            target_ref=str(target_ref),
            message=message,
            media_files=media_files,
        ), session_less, actor_uid
    # Record the exercise (the send completion itself is logged by the W1 post-tool
    # hook); this row carries the delegation rationale.
    _audit_side_effect(
        tool_name="send_message",
        action_class="send",
        source="delegated",
        status="delegated",
        actor=actor_uid or None,
        target_ref=f"{platform_name}:{target_ref}"[:200],
        rationale=f"delegation:{delegation.get('id', '?')}",
    )
    return None, session_less, actor_uid


def _handle_send(args):
    target, message = args.get("target", ""), args.get("message", "")
    if not target or not message:
        return tool_error("Both 'target' and 'message' are required when action='send'")
    target_ref = target.partition(":")[2].strip() or None
    platform_name, chat_id, thread_id, resolution_error = _resolve_tool_target(target)
    # Cookie: a Slack person target is not a channel. "<@U...>"/"<@W...>" is a user id
    # straight away; a human name ("slack:로이봉 이사님") is looked up via users.list
    # below, once the bot token is loaded — NOT treated as "no target given".
    slack_person_uid = unresolved_target_ref = None
    if platform_name == "slack" and target_ref:
        if mention := _SLACK_MENTION_TARGET_RE.fullmatch(target_ref):
            chat_id, thread_id, resolution_error = mention.group(1), None, None
            slack_person_uid = chat_id
        elif resolution_error:
            chat_id, thread_id, resolution_error = None, None, None
            unresolved_target_ref = target_ref
    if resolution_error:
        return tool_error(resolution_error)
    from tools.interrupt import is_interrupted
    if is_interrupted():
        return tool_error("Interrupted")
    try:
        from gateway.config import load_gateway_config
        config = load_gateway_config()
    except Exception as e:
        return json.dumps(_error(f"Failed to load gateway config: {e}"))
    platform, pconfig, entry, err = _resolve_platform_config(platform_name, config)
    if err:
        return tool_error(err)
    from gateway.platforms.base import BasePlatformAdapter
    # Capture [[as_document]] before extract_media strips it (images keep original bytes via send_document).
    force_document_attachments = "[[as_document]]" in message
    media_files, cleaned_message = BasePlatformAdapter.extract_media(message)
    media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
    mirror_text = cleaned_message.strip() or _describe_media_for_mirror(media_files)
    if unresolved_target_ref and not chat_id:
        try:
            from model_tools import _run_async
            user_id, lookup_error = _run_async(
                _resolve_slack_user_id_via_api(str(pconfig.token or ""), unresolved_target_ref))
        except Exception as e:
            return json.dumps(_error(f"Failed to resolve Slack user '{unresolved_target_ref}': {e}"))
        if not user_id:
            return json.dumps(_error(lookup_error or f"Could not resolve Slack user '{unresolved_target_ref}'."))
        chat_id = slack_person_uid = user_id
    used_home_channel = not chat_id
    if used_home_channel:
        chat_id, err = _home_chat_id(config, platform, platform_name)
        if err:
            return tool_error(err)
    if duplicate_skip := _maybe_skip_cron_duplicate_send(platform_name, chat_id, thread_id):
        return json.dumps(duplicate_skip)
    # Slack: resolve user targets to DM channel IDs before sending. _parse_target_ref emits internal
    # ``user:U...`` / ``user_name:@handle`` targets; a bare U... id can also arrive from session metadata,
    # the home-channel config, or the Cookie mention/person lookup above. All are opened via
    # conversations.open (fixes #19236).
    if platform_name == "slack" and chat_id:
        chat_id, resolve_err = _slack_dm_chat_id(pconfig, chat_id, person_uid=slack_person_uid)
        if resolve_err:
            return json.dumps(resolve_err)
    # POSITION IS LOAD-BEARING — this must stay BELOW Slack user→DM resolution.
    # `_parse_target_ref` emits internal pseudo-ids (`user_name:ben`,
    # `user:U...`) that no provenance can ever contain, because provenances
    # record RESOLVED conversation ids. Authorizing above the resolver compared
    # a handle against a set of `D...` ids and refused every Slack DM — a fix
    # that caused the outage it was meant to prevent. Pinned by
    # test_slack_user_targets_resolve_then_authorize; moving this call back up
    # turns those cases red.
    # thread_id is part of the DESTINATION: on Discord the thread is the literal
    # REST target, so an attested parent must not vouch for an arbitrary thread.
    _relay_denial = _authorize_relay_target(platform_name, chat_id, thread_id,
                                            native_token=getattr(pconfig, "token", None))
    if _relay_denial:
        return tool_error(_relay_denial)
    blocked, session_less, actor_uid = _send_actor_gate(
        config=config, platform_name=platform_name, target_ref=str(target_ref or chat_id),
        message=cleaned_message, media_files=media_files)
    if blocked:
        return blocked

    try:
        from model_tools import _run_async
        # Only custom plugin handlers receive the complete typed request.
        handler_args = {"args": args} if entry is not None and entry.send_message_handler is not None else {}
        result = _run_async(_send_to_platform(platform, pconfig, chat_id, cleaned_message, thread_id=thread_id,
                                              media_files=media_files, force_document=force_document_attachments,
                                              **handler_args))
        if isinstance(result, dict) and result.get("success"):
            if used_home_channel:
                result["note"] = f"Sent to {platform_name} home channel (chat_id: {chat_id})"
            # The user-token fallback rewrites the delivered text (provenance footer).
            delivered = result.get("mirror_text") or mirror_text
            if delivered and _mirror_sent_message(platform_name, chat_id, delivered, thread_id):
                result["mirrored"] = True
        if session_less:
            # W2: CLI (`hermes send`) and MCP (messages_send) bypass tool_executor, so the
            # W1 post-tool hook never records them — close that hole with the real outcome.
            # Session-ful agent sends are covered by hook A (source=agent), not here.
            _ok = isinstance(result, dict) and bool(result.get("success"))
            _audit_side_effect(
                tool_name="send_message",
                action_class="send",
                source="session-less",
                status="success" if _ok else "failed",
                actor=actor_uid or None,
                target_ref=str(target_ref or chat_id)[:200],
                result_preview=result,
                error_type=None if _ok else "send_failed",
            )
        if isinstance(result, dict) and "error" in result:
            result["error"] = _sanitize_error_text(result["error"])
        return json.dumps(result)
    except Exception as e:
        return json.dumps(_error(f"Send failed: {e}"))


def _platform_enum(platform_name):
    """``(Platform, None)`` or ``(None, error)`` for a platform name."""
    from gateway.config import Platform
    try:
        return Platform(platform_name), None
    except (ValueError, KeyError):
        return None, f"Unknown platform: {platform_name}"


def _resolve_platform_config(platform_name, config):
    """``(platform, pconfig, registry_entry, error)``. Plugin platforms must be registered;
    disabled/missing platforms error, except Weixin, which may be configured purely via .env."""
    from gateway.config import Platform
    from gateway.platform_registry import platform_registry
    entry = platform_registry.get(platform_name)
    if entry is None and platform_name not in {member.value for member in Platform}:
        return None, None, None, f"Unknown or unregistered plugin platform: {platform_name}"
    platform, err = _platform_enum(platform_name)
    if err:
        return None, None, None, err
    pconfig = config.platforms.get(platform)
    if not pconfig or not pconfig.enabled:
        pconfig = _weixin_env_pconfig() if platform_name == "weixin" else None
    if pconfig is None:
        return None, None, None, (f"Platform '{platform_name}' is not configured. Set up credentials in "
                                  "~/.hermes/config.yaml or environment variables.")
    return platform, pconfig, entry, None


def _home_chat_id(config, platform, platform_name):
    """``(home chat_id, None)`` or ``(None, actionable error)``; Weixin also honours WEIXIN_HOME_CHANNEL."""
    home = config.get_home_channel(platform)
    if home:
        return home.chat_id, None
    wx_home = os.getenv("WEIXIN_HOME_CHANNEL", "").strip() if platform_name == "weixin" else ""
    if wx_home:
        return wx_home, None
    home_env = _HOME_CHANNEL_ENV_OVERRIDES.get(platform_name, f"{platform_name.upper()}_HOME_CHANNEL")
    return None, (f"No home channel set for {platform_name} to determine where to send the message. "
                  f"Either specify a channel directly with '{platform_name}:CHANNEL_NAME', "
                  f"or set a home channel via: hermes config set {home_env} <channel_id>")


def _slack_dm_chat_id(pconfig, chat_id, *, person_uid=None):
    """Open Slack user targets (``user:``/``user_name:`` from the parser, a bare U... id from
    session metadata / home-channel config, or ``person_uid`` from the Cookie mention/person
    lookup) as DM conversations. ``(chat_id, None)`` or ``(None, error_dict)``."""
    from model_tools import _run_async
    if person_uid and chat_id == person_uid:
        try:
            dm_channel = _run_async(_open_slack_dm_channel(str(pconfig.token or ""), chat_id))
        except Exception as e:
            return None, _error(f"Failed to open Slack DM: {e}")
        if not dm_channel:
            return None, _error(f"Could not open DM with Slack user {chat_id}. Check bot permissions (im:write).")
        return dm_channel, None
    dm_target = f"user:{chat_id}" if chat_id.startswith("U") and _SLACK_USER_ID_RE.fullmatch(chat_id) else chat_id
    if not dm_target.startswith(("user:", "user_name:")):
        return chat_id, None
    return _run_async(_resolve_slack_user_target(pconfig.token, dm_target))


def _mirror_sent_message(platform_name, chat_id, mirror_text, thread_id):
    """Best-effort mirror of the sent message into the target's gateway session."""
    try:
        from gateway.mirror import mirror_to_session
        from gateway.session_context import get_session_env
        return bool(mirror_to_session(
            platform_name, chat_id, mirror_text, thread_id=thread_id,
            source_label=get_session_env("HERMES_SESSION_PLATFORM", "cli"),
            user_id=get_session_env("HERMES_SESSION_USER_ID", "") or None))
    except Exception:
        return False


def _weixin_env_pconfig():
    """Synthesize a Weixin PlatformConfig from .env secrets, or None."""
    wx_token = get_secret("WEIXIN_TOKEN", "").strip()
    wx_account = get_secret("WEIXIN_ACCOUNT_ID", "").strip()
    if not (wx_token and wx_account):
        return None
    from gateway.config import PlatformConfig
    return PlatformConfig(enabled=True, token=wx_token, extra={
        "account_id": wx_account, "base_url": get_secret("WEIXIN_BASE_URL", "").strip(),
        "cdn_base_url": get_secret("WEIXIN_CDN_BASE_URL", "").strip()})


def _describe_media_for_mirror(media_files):
    """Return a human-readable mirror summary when a message only contains media."""
    if not media_files:
        return ""
    if len(media_files) != 1:
        return f"[Sent {len(media_files)} media attachments]"
    media_path, is_voice = media_files[0]
    ext = os.path.splitext(media_path)[1].lower()
    if is_voice and ext in _VOICE_EXTS:
        return "[Sent voice message]"
    kind = next((k for exts, k in ((_IMAGE_EXTS, "image"), (_VIDEO_EXTS, "video"), (_AUDIO_EXTS, "audio"))
                 if ext in exts), "document")
    return f"[Sent {kind} attachment]"


def _maybe_skip_cron_duplicate_send(platform_name: str, chat_id: str, thread_id: str | None):
    """Skip redundant cron send_message calls when the scheduler will auto-deliver there."""
    from gateway.session_context import get_session_env
    auto_platform = get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM", "").strip().lower()
    auto_chat_id = get_session_env("HERMES_CRON_AUTO_DELIVER_CHAT_ID", "").strip()
    if not (auto_platform and auto_chat_id and auto_platform == platform_name and auto_chat_id == str(chat_id)
            and (get_session_env("HERMES_CRON_AUTO_DELIVER_THREAD_ID", "").strip() or None) == thread_id):
        return None
    target_label = f"{platform_name}:{chat_id}" + (f":{thread_id}" if thread_id is not None else "")
    return {"success": True, "skipped": True, "reason": "cron_auto_delivery_duplicate_target", "target": target_label,
        "note": (f"Skipped send_message to {target_label}. This cron job will already auto-deliver "
                 "its final response to that same target. Put the intended user-facing content in "
                 "your final response instead, or use a different target if you want an additional message.")}


def _bounded_send_error(detail, max_chars=900):
    """Bound untrusted adapter/plugin error detail returned by send_message."""
    text = str(detail or "send failed")
    return text if len(text) <= max_chars else f"{text[: max_chars - 3]}..."


async def _send_live_adapter_media(adapter, chat_id, message, media_files, *, thread_id=None, metadata=None,
                                   force_document=False):
    """Deliver text and every media descriptor through adapter media APIs; adapters that only
    inherit the BasePlatformAdapter stub for a kind are unsupported, not no-op'd."""
    caption, separate_text = _media_caption_split(message, media_files, max_caption_len=_DEFAULT_CAPTION_LIMIT)
    last_result = None
    if separate_text and separate_text.strip():
        last_result = await adapter.send(chat_id=chat_id, content=separate_text, metadata=metadata)
        if not last_result.success:
            return {"error": f"Adapter send failed: {_bounded_send_error(last_result.error)}"}
    from gateway.platforms.base import BasePlatformAdapter
    total = len(media_files)
    for index, descriptor in enumerate(media_files):
        media_path = descriptor[0] if isinstance(descriptor, (list, tuple)) and descriptor else None
        if not isinstance(media_path, str) or not media_path:
            return {"error": f"Adapter media send failed: invalid media descriptor {index + 1}/{total}"}
        is_voice = len(descriptor) > 1 and bool(descriptor[1])
        if not os.path.exists(media_path):
            return {"error": f"Adapter media send failed: media file {index + 1}/{total} was not found"}
        ext = os.path.splitext(media_path)[1].lower()
        method_name, media_kind = _adapter_media_method(ext, is_voice or ext in _AUDIO_EXTS, force_document)
        adapter_method = getattr(type(adapter), method_name, None)
        if adapter_method is None or adapter_method is getattr(BasePlatformAdapter, method_name):
            return {"error": (f"Live adapter does not implement native {media_kind} delivery; "
                              f"media file {index + 1}/{total} was not sent")}
        try:
            last_result = await getattr(adapter, method_name)(
                chat_id, media_path, caption=caption if index == 0 else None, reply_to=thread_id, metadata=metadata)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = _bounded_send_error(exc)
        else:
            if last_result.success:
                continue
            detail = _bounded_send_error(last_result.error or "media send failed")
        return {"error": f"Adapter media send failed after {index}/{total} files: {detail}"}
    if last_result is None:
        return {"error": _NO_DELIVERABLE}
    return {"success": True, "message_id": last_result.message_id, "media_delivered": True}


async def _dispatch_on_gateway_loop(runner, make_coro, log_message):
    """Await ``make_coro()`` on the gateway's loop: adapter.send() uses queues/tasks bound to it,
    so awaiting from another loop (the tool worker thread) deadlocks."""
    gateway_loop = getattr(runner, "_gateway_loop", None)
    if gateway_loop is None or asyncio.get_running_loop() is gateway_loop:
        return await make_coro()  # same loop / no gateway loop (CLI, tests)
    if not gateway_loop.is_running():
        return {"error": "Gateway loop is not running; cannot dispatch adapter send"}
    from agent.async_utils import safe_schedule_threadsafe
    fut = safe_schedule_threadsafe(make_coro(), gateway_loop, logger=logger, log_message=log_message)
    if fut is None:
        return {"error": "Gateway loop unavailable for send dispatch"}
    # shield: a cancelled caller must not cancel the enqueued send (a retry would duplicate it).
    # No timeout: the adapter and outer _run_async bound the wait.
    return await asyncio.shield(asyncio.wrap_future(fut))


async def _send_via_adapter(platform, pconfig, chat_id, chunk, *, thread_id=None, media_files=None,
                            force_document=False):
    """Live in-process gateway adapter first, else the plugin's ``standalone_sender_fn`` (cron),
    else an error naming both; media uses the adapter's native media APIs under the same rules."""
    platform_name = platform.value if hasattr(platform, "value") else str(platform)
    runner, adapter = _live_adapter(platform)
    if adapter is not None:
        try:
            metadata = {**({"thread_id": thread_id} if thread_id else {}),
                        **({"publish_topic": chat_id} if platform_name == "ntfy" and chat_id else {})} or None
            if media_files:  # always a dict result, returned as-is below
                make_coro = lambda: _send_live_adapter_media(  # noqa: E731
                    adapter, chat_id, chunk, media_files, thread_id=thread_id, metadata=metadata,
                    force_document=force_document)
            else:
                make_coro = lambda: adapter.send(chat_id=chat_id, content=chunk, metadata=metadata)  # noqa: E731
            result = await _dispatch_on_gateway_loop(
                runner, make_coro, f"send_message: failed to schedule{' media send' if media_files else ''} on gateway loop")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return {"error": f"Plugin platform send failed: {_bounded_send_error(e)}"}
        if isinstance(result, dict):
            return result
        if result.success:
            return {"success": True, "message_id": result.message_id}
        return {"error": f"Adapter send failed: {_bounded_send_error(result.error)}"}
    try:
        from gateway.platform_registry import platform_registry
        sender = platform_registry.get(platform_name).standalone_sender_fn
    except Exception:
        sender = None
    if sender is None:
        return {"error": (f"No live adapter for platform '{platform_name}'. Is the gateway running with this platform "
                          f"connected? For out-of-process delivery (e.g. cron in a separate process), the platform "
                          f"plugin must register a standalone_sender_fn on its PlatformEntry.")}
    try:
        result = await sender(pconfig, chat_id, chunk, thread_id=thread_id, media_files=media_files,
                              force_document=force_document)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug("Plugin standalone send for %s raised", platform_name, exc_info=True)
        return {"error": f"Plugin standalone send failed: {_bounded_send_error(e)}"}
    if isinstance(result, dict) and (result.get("success") or result.get("error")):
        return {**result, "error": _bounded_send_error(result["error"])} if result.get("error") else result
    return {"error": (f"Plugin standalone send for '{platform_name}' returned an invalid result: "
                      f"expected a dict with 'success' or 'error' keys, got {type(result).__name__}")}


async def _send_chunks(chunks, send_one):
    """``send_one(chunk, is_last)`` in order; stop at the first error dict, else last result."""
    result = None
    # --- Matrix: route ALL sends through the native adapter so text is encrypted in E2EE rooms too (issue:
    # text-only sends arrived with a red padlock because they took the raw-HTTP standalone path). The
    # adapter reuses the live gateway's E2EE session when available (#46310) and falls back to an
    # encryption-aware ephemeral adapter for standalone/cron. ---
    for i, chunk in enumerate(chunks):
        result = await send_one(chunk, i == len(chunks) - 1)
        if isinstance(result, dict) and result.get("error"):
            break
    return result


def _platform_max_length(platform):
    """Chunking limit: Signal's adapter constant (its raw JSON-RPC path bypasses the adapter's
    chunking), the registry's ``max_message_length`` for plugins, else None (no chunking)."""
    from gateway.config import Platform
    if platform == Platform.SIGNAL:
        try:
            from gateway.platforms.signal import MAX_MESSAGE_LENGTH
            return MAX_MESSAGE_LENGTH
        except ImportError:
            return 8000
    try:
        from gateway.platform_registry import platform_registry
        entry = platform_registry.get(platform.value)
        return entry.max_message_length if entry and entry.max_message_length > 0 else None
    except Exception:
        return None


# Plugin platforms whose media (Discord: all) sends deliberately bypass the live adapter for the
# registry ``standalone_sender_fn`` (Discord: forums/threads/multipart; Slack: files_upload_v2;
# WhatsApp: Baileys /send-media). platform -> (error label, run discover_plugins first,
# caption-capable, media_files sentinel for non-final chunks, forward force_document)
_PLUGIN_STANDALONE_MEDIA = {"discord": ("Discord", False, True, [], False), "feishu": ("Feishu", True, False, None, False),
                            "slack": ("Slack", True, True, [], False), "whatsapp": ("WhatsApp", True, True, None, True)}


async def _send_plugin_standalone(platform_name, pconfig, chat_id, message, chunks, media_files, *, thread_id,
                                  max_len, force_document):
    """Chunked send through a plugin's standalone_sender_fn; one captionable file + short text
    rides as the media caption."""
    label, discover, captionable, empty_media, pass_force = _PLUGIN_STANDALONE_MEDIA[platform_name]
    sender, err = _plugin_standalone_sender(platform_name, label=label, discover=discover)
    if err:
        return err
    extra = {"force_document": force_document} if pass_force else {}
    if captionable:
        # Cap on the platform's own message limit so the caption is deliverable.
        caption, _ = _media_caption_split(message, media_files, max_caption_len=(max_len or _DEFAULT_CAPTION_LIMIT))
        if caption is not None:
            return await sender(pconfig, chat_id, "", thread_id=thread_id, media_files=media_files,
                                caption=caption, **extra)
    return await _send_chunks(chunks, lambda chunk, is_last: sender(
        pconfig, chat_id, chunk, thread_id=thread_id, media_files=media_files if is_last else empty_media, **extra))


def _via_adapter_route(p, pc, cid, chunk, media, tid, fd):
    return _send_via_adapter(p, pc, cid, chunk, thread_id=tid, media_files=media, force_document=fd)


# Native-media chunked routes for built-in platforms; media rides on the final chunk, non-final
# chunks get the sentinel. platform -> (media required, sentinel, sender(platform, pconfig,
# chat_id, chunk, media, thread_id, force_document)). Matrix: ALL sends use the native adapter
# (E2EE text). Signal: attachments ride the JSON-RPC param. Yuanbao / WeCom: media needs the
# running gateway. Slack text has its own route (_send_slack_text_chunks). Names resolve at call
# time so tests can monkeypatch ``_send_signal``.
_CHUNKED_ROUTES = {
    "matrix": (False, [], lambda p, pc, cid, chunk, media, tid, fd: _send_matrix_via_adapter(
        pc, cid, chunk, media_files=media, thread_id=tid)),
    "signal": (True, [], lambda p, pc, cid, chunk, media, tid, fd: _send_signal(
        pc.extra, cid, chunk, media_files=media)),
    "yuanbao": (True, None, lambda p, pc, cid, chunk, media, tid, fd: _send_yuanbao(cid, chunk, media_files=media)),
    "wecom": (True, None, _via_adapter_route)}

# Text-only senders for built-in platforms (generic path; media is dropped with a
# warning). Signature: (pconfig, chat_id, chunk, thread_id) -> result.
_TEXT_SENDERS = {
    **{name: partial(_registry_standalone_send, name)
       for name in ("whatsapp", "email", "sms", "dingtalk", "feishu", "wecom")},
    "signal": lambda pc, cid, chunk, tid: _send_signal(pc.extra, cid, chunk),
    "bluebubbles": lambda pc, cid, chunk, tid: _send_bluebubbles(pc.extra, cid, chunk),
    "qqbot": lambda pc, cid, chunk, tid: _send_qqbot(pc, cid, chunk),
    "yuanbao": lambda pc, cid, chunk, tid: _send_yuanbao(cid, chunk)}

_MEDIA_PLATFORMS_NOTE = "telegram, discord, matrix, weixin, signal, yuanbao, feishu, whatsapp and slack"


async def _send_to_platform(platform, pconfig, chat_id, message, thread_id=None, media_files=None, force_document=False, args=None):
    """Route to the platform sender, chunking long text with the adapters' splitter. Order matters:
    Weixin first (its native helper must not be blocked by unrelated optional imports such as
    lark-oapi), Telegram (chunks itself), plugin standalone media, native chunked, generic text."""
    from gateway.config import Platform
    platform_name = platform.value if hasattr(platform, "value") else str(platform)
    media_files = media_files or []
    if platform == Platform.WEIXIN:
        return await _send_weixin(pconfig, chat_id, message, media_files=media_files)
    # Telegram chunks internally on the *formatted* text (escaping inflates length).
    if platform == Platform.TELEGRAM:
        return await _send_telegram(
            pconfig.token, chat_id, message, media_files=media_files, thread_id=thread_id, force_document=force_document,
            disable_link_previews=bool(getattr(pconfig, "extra", {}) and pconfig.extra.get("disable_link_previews")))
    from gateway.platforms.base import BasePlatformAdapter
    max_len = _platform_max_length(platform)
    chunks = BasePlatformAdapter.truncate_message(message, max_len) if max_len else [message]
    if platform_name == "discord" or (media_files and platform_name in _PLUGIN_STANDALONE_MEDIA):
        return await _send_plugin_standalone(platform_name, pconfig, chat_id, message, chunks, media_files,
                                             thread_id=thread_id, max_len=max_len, force_document=force_document)
    if platform_name == "slack":
        return await _send_slack_text_chunks(platform, pconfig, chat_id, chunks, thread_id, force_document)
    route = _CHUNKED_ROUTES.get(platform_name)
    if route is not None and (media_files or not route[0]):
        _, empty_media, sender = route
        return await _send_chunks(chunks, lambda chunk, is_last: sender(
            platform, pconfig, chat_id, chunk, media_files if is_last else empty_media, thread_id, force_document))

    # Generic path: text only. Buzz delivers media natively via _send_via_adapter, so no warning.
    warning = None
    if media_files and platform_name != "buzz":
        if not message.strip():
            return {"error": (f"send_message MEDIA delivery is currently only supported for {_MEDIA_PLATFORMS_NOTE}; "
                              f"target {platform_name} had only media attachments")}
        warning = (f"MEDIA attachments were omitted for {platform_name}; "
                   f"native send_message media delivery is currently only supported for {_MEDIA_PLATFORMS_NOTE}")
    text_sender = _TEXT_SENDERS.get(platform_name)
    if text_sender is not None:
        send_one = lambda chunk, is_last: text_sender(pconfig, chat_id, chunk, thread_id)  # noqa: E731
    else:
        from gateway.platform_registry import platform_registry
        entry = platform_registry.get(platform_name)
        if entry is not None and entry.send_message_handler is not None:
            # Custom handler receives the full typed request once (not per chunk).
            try:
                import inspect
                result = entry.send_message_handler(args or {}, chat_id, platform_name, pconfig)
                return await result if inspect.isawaitable(result) else result
            except Exception as e:
                return {"error": f"Plugin send_message handler failed: {e}"}
        # Plugin platform: live gateway adapter if available, else standalone_sender_fn.
        send_one = lambda chunk, is_last: _via_adapter_route(  # noqa: E731
            platform, pconfig, chat_id, chunk, media_files if is_last else [], thread_id, force_document)
    last_result = await _send_chunks(chunks, send_one)
    if (warning and isinstance(last_result, dict) and last_result.get("success")
            and not last_result.get("media_delivered")):
        last_result["warnings"] = [*last_result.get("warnings", []), warning]
    return last_result


# --- Cookie: Slack text route (ts registration + owner-approved user-token fallback) ----


def _append_slack_user_token_footer(message: str) -> str:
    """Append the Cookie user-token provenance footer once."""
    text = (message or "").rstrip()
    if _SLACK_USER_TOKEN_FOOTER in text:
        return text
    if not text:
        return _SLACK_USER_TOKEN_FOOTER
    return f"{text}\n\n{_SLACK_USER_TOKEN_FOOTER}"


def _slack_error_code(result: dict | None) -> str | None:
    """Extract a Slack Web API error code from a send result."""
    if not isinstance(result, dict):
        return None
    code = result.get("slack_error")
    if code:
        return str(code)
    error = str(result.get("error") or "")
    match = (re.search(r"Slack API error:\s*([A-Za-z0-9_\-]+)", error)
             # Live-adapter / slack_sdk failures carry the bare code in free text.
             or re.search(r"\b(%s)\b" % "|".join(sorted(_SLACK_BOT_ACCESS_ERRORS)), error))
    return match.group(1) if match else None


def _is_slack_bot_access_error(result: dict | None) -> bool:
    """Return True when bot-token send failed because the bot cannot access the conversation."""
    return _slack_error_code(result) in _SLACK_BOT_ACCESS_ERRORS


async def _send_slack_text_chunks(platform, pconfig, chat_id, chunks, thread_id, force_document):
    """Slack text send: upstream's live-adapter-then-standalone route plus two Cookie
    behaviours — register each sent ts on the live adapter (a standalone send bypasses
    ``adapter.send()``, so un-@mentioned replies to a bot-opened thread would be dropped by
    the inbound gate), and, when the bot cannot reach the conversation at all, offer the
    owner-approved SLACK_USER_TOKEN fallback (first chunk only, before anything was posted)."""
    result = None
    for index, chunk in enumerate(chunks):
        result = await _via_adapter_route(platform, pconfig, chat_id, chunk, [], thread_id, force_document)
        if isinstance(result, dict) and result.get("success"):
            _register_bot_sent_ts_on_live_adapter(result.get("message_id"), thread_id)
            continue
        if index == 0 and _is_slack_bot_access_error(result):
            return await _send_slack_user_token_fallback(
                chat_id=chat_id, chunks=chunks, start_index=index, original_error=result, thread_id=thread_id)
        break
    return result


def _register_bot_sent_ts_on_live_adapter(sent_ts, thread_id=None):
    """Register a sent Slack ts on the live adapter's ``_bot_message_ts`` so mention-less
    thread replies to bot-posted threads are picked up by the inbound gate — parity with
    ``SlackAdapter.send()``.

    A standalone send (registry ``standalone_sender_fn``) never touches the live adapter's
    ``_bot_message_ts``. Without this, a bot message posted by a tool/cron/skill (e.g. the
    weekly-share draft) opens a thread the gate doesn't recognize, so the owner's
    un-@mentioned reply to that thread is dropped at ``reply_to_bot_thread``. No-op when out
    of process (cron in a separate process: the runner weakref is ``None``)."""
    if not sent_ts:
        return
    try:
        from gateway.run import _gateway_runner_ref
        from gateway.config import Platform

        runner = _gateway_runner_ref()
        if runner is None:
            return
        adapter = runner.adapters.get(Platform.SLACK)
        if adapter is None or not hasattr(adapter, "_bot_message_ts"):
            return
        adapter._bot_message_ts.add(sent_ts)
        if thread_id:
            adapter._bot_message_ts.add(thread_id)
        cap = getattr(adapter, "_BOT_TS_MAX", 5000)
        if len(adapter._bot_message_ts) > cap:
            excess = len(adapter._bot_message_ts) - cap // 2
            for old_ts in list(adapter._bot_message_ts)[:excess]:
                adapter._bot_message_ts.discard(old_ts)
    except Exception:
        logger.debug("Could not register bot-sent ts on live Slack adapter", exc_info=True)


async def _send_slack(token, chat_id, message, *, thread_id=None):
    """Raw Slack Web API send. Retained for the Cookie user-token fallback,
    which posts with an explicit token (not the registered bot pconfig)."""
    try:
        import aiohttp
    except ImportError:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}
    try:
        from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
        _proxy = resolve_proxy_url()
        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
        url = "https://slack.com/api/chat.postMessage"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), **_sess_kw) as session:
            payload = {"channel": chat_id, "text": message, "mrkdwn": True}
            if thread_id:
                payload["thread_ts"] = thread_id
            async with session.post(url, headers=headers, json=payload, **_req_kw) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return {"success": True, "platform": "slack", "chat_id": chat_id, "message_id": data.get("ts")}
                error_code = data.get("error", "unknown")
                result = _error(f"Slack API error: {error_code}")
                result["slack_error"] = error_code
                return result
    except Exception as e:
        return _error(f"Slack send failed: {e}")


def _request_slack_user_token_fallback_approval(
    *,
    chat_id: str,
    thread_id: str | None,
    preview: str,
    original_error: dict | None,
) -> tuple[bool, str | None]:
    """Ask the gateway owner to approve posting as Cookie via SLACK_USER_TOKEN."""
    try:
        from gateway.session_context import get_session_env

        source_platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        owner_user_id = get_session_env("HERMES_SESSION_USER_ID", "")
        origin_chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        if source_platform != "slack" or not owner_user_id or not origin_chat_id:
            return False, "Cookie user-token fallback requires an interactive Slack owner approval session."

        from tools.approval import request_gateway_approval

        error_code = _slack_error_code(original_error) or "unknown"
        approval = request_gateway_approval(
            command=(
                "send_message Slack user-token fallback\n"
                f"target={chat_id} thread={thread_id or '-'} bot_error={error_code}\n\n"
                f"{preview}"
            ),
            description="Post to Slack as Cookie via SLACK_USER_TOKEN?",
            pattern_key="tool:send_message:slack:user_token_fallback",
            allow_permanent=False,
        )
        if approval.get("approved"):
            return True, None
        return False, approval.get("message") or "Cookie user-token fallback was not approved."
    except Exception as exc:
        return False, f"Cookie user-token fallback approval failed: {exc}"


async def _send_slack_user_token_fallback(
    *,
    chat_id: str,
    chunks: list[str],
    start_index: int,
    original_error: dict | None,
    thread_id: str | None = None,
) -> dict:
    """Retry a bot-inaccessible Slack send using Cookie's user token after approval."""
    user_token = os.getenv("SLACK_USER_TOKEN", "").strip()
    if not user_token:
        return original_error or {"error": "Slack bot-token send failed and SLACK_USER_TOKEN is not set."}

    fallback_chunks = list(chunks[start_index:])
    if not fallback_chunks:
        return original_error or {"error": "Slack bot-token send failed before any fallback content was available."}
    fallback_chunks[-1] = _append_slack_user_token_footer(fallback_chunks[-1])

    preview = "\n\n".join(fallback_chunks).strip()
    if len(preview) > 1200:
        preview = preview[:1200] + "..."
    approved, denial = _request_slack_user_token_fallback_approval(
        chat_id=chat_id,
        thread_id=thread_id,
        preview=preview,
        original_error=original_error,
    )
    if not approved:
        result: dict[str, Any] = dict(original_error or {"error": "Slack bot-token send failed."})
        result["user_token_fallback_available"] = True
        result["approval_required"] = True
        result["fallback_error"] = _sanitize_error_text(denial or "Cookie user-token fallback was not approved.")
        return result

    last_result = None
    for fallback_chunk in fallback_chunks:
        result = await _send_slack(user_token, chat_id, fallback_chunk, thread_id=thread_id)
        if isinstance(result, dict) and result.get("error"):
            result["used_user_token_fallback"] = True
            return result
        last_result = result

    if isinstance(last_result, dict):
        last_result["used_user_token_fallback"] = True
        last_result["bot_token_error"] = _slack_error_code(original_error) or "unknown"
        last_result["mirror_text"] = "\n\n".join(fallback_chunks).strip()
    return last_result or {"error": "Cookie user-token fallback produced no Slack response."}


# --- Cookie: update_message (edit a message the bot itself sent) ------------------------


async def _update_slack_standalone(token, chat_id, message_ts, new_text):
    """Edit a Slack message via chat.update with the bot token (out-of-process
    fallback when no live adapter is reachable). Mirrors ``_send_slack``."""
    try:
        import aiohttp
    except ImportError:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}
    try:
        from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
        _proxy = resolve_proxy_url()
        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
        url = "https://slack.com/api/chat.update"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), **_sess_kw) as session:
            payload = {"channel": chat_id, "ts": message_ts, "text": new_text, "mrkdwn": True}
            async with session.post(url, headers=headers, json=payload, **_req_kw) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return {"success": True, "platform": "slack", "chat_id": chat_id,
                            "message_id": data.get("ts", message_ts)}
                error_code = data.get("error", "unknown")
                result = _error(f"Slack API error: {error_code}")
                result["slack_error"] = error_code
                return result
    except Exception as e:
        return _error(f"Slack edit failed: {e}")


async def _resolve_latest_bot_message_ts_slack(token, chat_id, thread_ts=None):
    """Return the ts of the most recent message authored by THIS bot in the
    given Slack channel (or thread, if thread_ts is set). None if not found."""
    try:
        import aiohttp
    except ImportError:
        return None
    try:
        from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
        _proxy = resolve_proxy_url()
        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), **_sess_kw) as session:
            # Resolve our own bot user id.
            async with session.get("https://slack.com/api/auth.test", headers=headers, **_req_kw) as resp:
                auth = await resp.json()
            bot_uid = auth.get("user_id") if auth.get("ok") else None
            if not bot_uid:
                return None
            # Most recent messages first.
            if thread_ts:
                api = "https://slack.com/api/conversations.replies"
                params = {"channel": chat_id, "ts": thread_ts, "limit": "50"}
            else:
                api = "https://slack.com/api/conversations.history"
                params = {"channel": chat_id, "limit": "50"}
            async with session.get(api, headers=headers, params=params, **_req_kw) as resp:
                data = await resp.json()
            if not data.get("ok"):
                return None
            msgs = data.get("messages", [])
            # history returns newest-first; replies returns oldest-first.
            ordered = msgs if not thread_ts else list(reversed(msgs))
            for m in ordered:
                if m.get("user") == bot_uid or (m.get("bot_id") and m.get("user") == bot_uid):
                    return m.get("ts")
            # Fallback: match on bot_id presence when user field is absent.
            for m in ordered:
                if m.get("bot_id") and not m.get("user"):
                    return m.get("ts")
            return None
    except Exception:
        return None


def _handle_update(args):
    """Edit a previously-sent message (the bot's own) on a platform target."""
    target = args.get("target", "")
    new_message = args.get("new_message", "")
    message_ts = (args.get("message_ts") or "").strip() or None
    if not target or not new_message:
        return tool_error("Both 'target' and 'new_message' are required.")

    # pass_unresolved_references: a bare channel id the directory doesn't know still edits.
    platform_name, chat_id, thread_id, resolution_error = _resolve_tool_target(
        target, pass_unresolved_references=True)
    if resolution_error:
        return tool_error(resolution_error)
    if not chat_id:
        return tool_error("Could not resolve a channel from 'target'. Use 'platform:chat_id' or 'platform:#channel'.")

    try:
        from gateway.config import load_gateway_config
        config = load_gateway_config()
    except Exception as e:
        return tool_error(f"Failed to load gateway config: {e}")
    platform, pconfig, _entry, err = _resolve_platform_config(platform_name, config)
    if err:
        return tool_error(err)

    # Permission gate (mirrors send: an on-behalf edit requires the owner) [W3].
    actor_uid, session_ful, actor_is_owner = _session_actor()
    if session_ful and not actor_is_owner:
        return _block_on_behalf_send(
            config=config,
            actor_uid=actor_uid,
            platform_name=platform_name,
            target_ref=str(chat_id),
            message=new_message,
            media_files=[],
        )
    # P5(a): an edit is an outbound act against a named destination, same floor as send.
    _relay_denial = _authorize_relay_target(platform_name, chat_id, thread_id,
                                            native_token=getattr(pconfig, "token", None))
    if _relay_denial:
        return tool_error(_relay_denial)

    from model_tools import _run_async

    # Resolve which message to edit.
    if not message_ts:
        if platform_name == "slack":
            message_ts = _run_async(
                _resolve_latest_bot_message_ts_slack(str(pconfig.token or ""), chat_id, thread_id)
            )
        if not message_ts:
            return tool_error(
                "No 'message_ts' given and could not find a recent bot message to edit. "
                "Read the channel/thread and pass the exact message id."
            )

    # Execute: prefer the live in-process adapter (uniform across platforms), fall back to a
    # standalone Slack chat.update.
    result = None
    _runner, adapter = _live_adapter(platform)
    if adapter is not None and hasattr(adapter, "edit_message"):
        try:
            send_result = _run_async(adapter.edit_message(chat_id, message_ts, new_message))
            if getattr(send_result, "success", False):
                result = {"success": True, "platform": platform_name, "chat_id": chat_id, "message_id": message_ts}
            else:
                result = _error(f"Edit failed: {getattr(send_result, 'error', 'unknown')}")
        except Exception as e:
            result = _error(f"Edit via adapter failed: {e}")
    if result is None:
        # Out-of-process fallback (Slack only).
        if platform_name == "slack":
            result = _run_async(_update_slack_standalone(str(pconfig.token or ""), chat_id, message_ts, new_message))
        else:
            result = _error(
                f"No live adapter for '{platform_name}' to edit the message "
                "(out-of-process editing is only supported for Slack)."
            )

    _audit_side_effect(
        tool_name="update_message",
        action_class="edit",
        source="tool",
        status="ok" if isinstance(result, dict) and result.get("success") else "error",
        actor=actor_uid or None,
        target_ref=f"{platform_name}:{chat_id}:{message_ts}"[:200],
    )
    if isinstance(result, dict) and "error" in result:
        result["error"] = _sanitize_error_text(result["error"])
    return json.dumps(result)


def update_message_tool(args, **kw):
    """Handle update_message (edit a previously-sent bot message)."""
    from tools.interrupt import is_interrupted
    if is_interrupted():
        return tool_error("Interrupted")
    return _handle_update(args)


def _check_send_message():
    """Gate send_message on gateway running (always available on messaging platforms).

    Also passes for kanban workers — the dispatcher sets ``HERMES_KANBAN_TASK``
    on every spawned worker, but those workers run with the assignee profile's
    ``HERMES_HOME`` which has no ``gateway.pid``, so the gateway-running check
    would fail even though the parent gateway is alive. Honoring the env var
    lets workers call ``send_message`` to deliver rich content directly to the
    originating chat (paired with ``kanban_complete`` for the short notifier
    summary), which is the canonical pattern for any worker that needs to
    reply with more than the ~200-char first-line truncation the kanban
    notifier applies.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    from gateway.session_context import get_session_env
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    if platform and platform != "local":
        return True
    try:
        from gateway.status import is_gateway_running
        return is_gateway_running()
    except Exception:
        return False


def _check_update_message():
    """Gate update_message identically to send_message."""
    return _check_send_message()


SEND_MESSAGE_SCHEMA = {
    "name": "send_message",
    "description": (
        "Send a message to a connected messaging platform, or list available targets.\n\n"
        "IMPORTANT: When the user asks to send to a specific channel or person "
        "(not just a bare platform name), call send_message(action='list') FIRST to see "
        "available targets, then send to the correct one.\n"
        "If the user just says a platform name like 'send to telegram', send directly "
        "to the home channel without listing first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "list", "react", "unreact"],
                "description": "Action to perform. 'send' (default) sends a message. 'list' returns all available channels/contacts across connected platforms. 'react' attaches an emoji reaction to a message (platforms that support it, e.g. photon/iMessage tapbacks). 'unreact' retracts a previously-added reaction."
            },
            "target": {
                "type": "string",
                "description": "Delivery target. Format: 'platform' (uses home channel), 'platform:#channel-name', 'platform:chat_id', or 'platform:chat_id:thread_id' for Telegram topics and Discord threads. Examples: 'telegram', 'telegram:-1001234567890:17585', 'discord:999888777:555444333', 'discord:#bot-home', 'slack:#engineering', 'slack:<@U12345678>' (DM a person), 'signal:+155****4567', 'matrix:!roomid:server.org', 'matrix:@user:server.org', 'ntfy:alerts-channel' (explicit ntfy topic), 'yuanbao:direct:<account_id>' (DM), 'yuanbao:group:<group_code>' (group chat)"
            },
            "message": {
                "type": "string",
                "description": "The message text to send. To send an image or file, include MEDIA:<local_path> (e.g. 'MEDIA:/tmp/report.pdf') in the message — the platform will deliver it as a native media attachment."
            },
            "emoji": {
                "type": "string",
                "description": "For action='react': the emoji to react with (e.g. '❤️'). On iMessage, ❤️👍👎😂‼️❓ render as native tapbacks; other emoji use custom-emoji reactions."
            },
            "message_id": {
                "type": "string",
                "description": "For action='react'/'unreact': id of the message to react to. Omit to target the most recent message received in that chat (usually the one being replied to)."
            }
        },
        "required": []
    }
}

UPDATE_MESSAGE_SCHEMA = {
    "name": "update_message",
    "description": (
        "Edit a message THIS bot previously sent on a messaging platform — e.g. to "
        "correct a mistake the owner pointed out. Only the bot's own messages can be "
        "edited (platform rule).\n\n"
        "Provide 'target' (same format as send_message: 'slack:#channel' or "
        "'slack:CHANNELID') and 'new_message' (the full replacement text — editing "
        "REPLACES the message, it does not append). 'message_ts' identifies which "
        "message to edit; if you omit it, the bot's most recent message in that "
        "channel/thread is edited (Slack). When unsure which message, read the "
        "channel/thread first to get the right ts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Where the message lives. Format: 'platform:chat_id', 'platform:#channel-name', or 'platform:chat_id:thread_id'. Example: 'slack:#쿠키테스트', 'slack:C0ANUN2AQER'.",
            },
            "new_message": {
                "type": "string",
                "description": "The full replacement text. Editing replaces the entire message body.",
            },
            "message_ts": {
                "type": "string",
                "description": "Optional. The id/timestamp of the message to edit (Slack 'ts', Discord message id). If omitted, the bot's most recent message in the target channel/thread is edited.",
            },
        },
        "required": ["target", "new_message"],
    },
}


# --- Registry (Cookie overlay: upstream registers neither tool) ---
registry.register(
    name="send_message",
    toolset="messaging",
    schema=SEND_MESSAGE_SCHEMA,
    handler=send_message_tool,
    check_fn=_check_send_message,
    emoji="📨",
)

registry.register(
    name="update_message",
    toolset="messaging",
    schema=UPDATE_MESSAGE_SCHEMA,
    handler=update_message_tool,
    check_fn=_check_update_message,
    emoji="✏️",
)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import time  # noqa: F401,E402

_PLUGIN_COMPAT_LAZY = {
    'redact_sensitive_text': ('agent.redact', 'redact_sensitive_text'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
