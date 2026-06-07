"""W2 send-authorization tests for tools/send_message_tool.py.

Covers the 6/4 W2 decisions:
  • owner, session-ful      → approval gate SKIPPED (send proceeds)
  • executive, session-ful  → interactive gateway-approval gate FIRES
  • session-less (CLI/MCP)   → no gate, AUDIT-ONLY (record_side_effect called)

See docs/auth-audit-redesign.md (W2).
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import Platform
from tools.send_message_tool import send_message_tool


def _run_async_immediately(coro):
    return asyncio.run(coro)


def _slack_config():
    slack_cfg = SimpleNamespace(enabled=True, token="xoxb-test", extra={})
    config = SimpleNamespace(
        platforms={Platform.SLACK: slack_cfg},
        get_home_channel=lambda _platform: None,
    )
    return slack_cfg, config


def _session_env_factory(platform="", user_id="", chat_id=""):
    def _env(name, default=""):
        return {
            "HERMES_SESSION_PLATFORM": platform,
            "HERMES_SESSION_USER_ID": user_id,
            "HERMES_SESSION_CHAT_ID": chat_id,
        }.get(name, default)
    return _env


def _invoke(session_env, *, owner_ids="U_OWNER"):
    """Run send_message_tool to a plain channel under the given session env.

    Returns (result_dict, approval_mock, audit_mock).
    """
    slack_cfg, config = _slack_config()
    approval_mock = MagicMock(return_value={"approved": True, "choice": "once"})
    audit_mock = MagicMock()
    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("gateway.channel_directory.resolve_channel_name", return_value=None), \
         patch("gateway.session_context.get_session_env", side_effect=session_env), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         patch("tools.send_message_tool._resolve_slack_user_id_via_api",
               new=AsyncMock(return_value=("C123456", None))), \
         patch("tools.approval.request_gateway_approval", approval_mock), \
         patch("gateway.side_effect_audit.record_side_effect", audit_mock), \
         patch("tools.send_message_tool._send_to_platform",
               new=AsyncMock(return_value={"success": True})), \
         patch("gateway.mirror.mirror_to_session", return_value=True), \
         patch.dict("os.environ", {"HERMES_OWNER_IDS": owner_ids}, clear=False):
        result = json.loads(
            send_message_tool({"action": "send", "target": "slack:C123456", "message": "hi"})
        )
    return result, approval_mock, audit_mock


def test_owner_session_skips_gate():
    env = _session_env_factory(platform="slack", user_id="U_OWNER", chat_id="C_ORIGIN")
    result, approval_mock, audit_mock = _invoke(env, owner_ids="U_OWNER")
    assert result["success"] is True
    approval_mock.assert_not_called()           # owner → no meaningless self-loop
    audit_mock.assert_not_called()               # session-ful → covered by hook A, not here


def test_non_owner_on_behalf_blocked_and_escalated():
    # W3: a non-owner (e.g. executive) send is on-behalf → blocked + escalated,
    # NOT gated in the requester's own session (the 6/2 self-approval loophole).
    env = _session_env_factory(platform="slack", user_id="U_EXEC", chat_id="C_ORIGIN")
    slack_cfg, config = _slack_config()
    audit_mock = MagicMock()
    escalate_mock = MagicMock(return_value=True)
    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("gateway.channel_directory.resolve_channel_name", return_value=None), \
         patch("gateway.session_context.get_session_env", side_effect=env), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         patch("tools.send_message_tool._resolve_slack_user_id_via_api",
               new=AsyncMock(return_value=("C123456", None))), \
         patch("gateway.side_effect_audit.record_side_effect", audit_mock), \
         patch("tools.send_message_tool._escalate_on_behalf_to_owner", escalate_mock), \
         patch("tools.send_message_tool._send_to_platform",
               new=AsyncMock(return_value={"success": True})) as send_mock, \
         patch.dict("os.environ", {"HERMES_OWNER_IDS": "U_OWNER"}, clear=False):
        result = json.loads(
            send_message_tool({"action": "send", "target": "slack:C123456", "message": "hi 조이"})
        )
    assert result["success"] is False
    assert result["blocked"] is True
    assert result["owner_approval_required"] is True
    send_mock.assert_not_awaited()               # on-behalf send never fires
    escalate_mock.assert_called_once()           # owner notified
    audit_mock.assert_called_once()
    kwargs = audit_mock.call_args.kwargs
    assert kwargs["status"] == "blocked"
    assert kwargs["blocked_reason"] == "on_behalf_requires_owner_approval"
    assert kwargs["actor"] == "U_EXEC"


def test_on_behalf_block_records_audit_even_if_escalation_fails():
    # The W1 ledger record is the durable trail; a failed owner DM must not
    # swallow the block.
    env = _session_env_factory(platform="slack", user_id="U_EXEC", chat_id="C_ORIGIN")
    slack_cfg, config = _slack_config()
    audit_mock = MagicMock()
    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("gateway.channel_directory.resolve_channel_name", return_value=None), \
         patch("gateway.session_context.get_session_env", side_effect=env), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         patch("tools.send_message_tool._resolve_slack_user_id_via_api",
               new=AsyncMock(return_value=("C123456", None))), \
         patch("gateway.side_effect_audit.record_side_effect", audit_mock), \
         patch("tools.send_message_tool._escalate_on_behalf_to_owner", return_value=False), \
         patch("tools.send_message_tool._send_to_platform",
               new=AsyncMock(return_value={"success": True})) as send_mock, \
         patch.dict("os.environ", {"HERMES_OWNER_IDS": "U_OWNER"}, clear=False):
        result = json.loads(
            send_message_tool({"action": "send", "target": "slack:C123456", "message": "hi"})
        )
    assert result["blocked"] is True
    send_mock.assert_not_awaited()
    audit_mock.assert_called_once()
    assert audit_mock.call_args.kwargs["status"] == "blocked"


def test_w4_delegation_allows_on_behalf_send():
    # W4: an owner-granted delegation lets a non-owner's on-behalf send proceed,
    # audited with the delegation rationale instead of being blocked.
    env = _session_env_factory(platform="slack", user_id="U_EXEC", chat_id="C_ORIGIN")
    slack_cfg, config = _slack_config()
    audit_mock = MagicMock()
    block_mock = MagicMock()
    deleg = {"id": "dlg_test", "grantee": "U_EXEC", "action": "send"}
    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("gateway.channel_directory.resolve_channel_name", return_value=None), \
         patch("gateway.session_context.get_session_env", side_effect=env), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         patch("tools.send_message_tool._resolve_slack_user_id_via_api",
               new=AsyncMock(return_value=("C123456", None))), \
         patch("gateway.delegations.match_delegation", return_value=deleg), \
         patch("gateway.side_effect_audit.record_side_effect", audit_mock), \
         patch("tools.send_message_tool._block_on_behalf_send", block_mock), \
         patch("tools.send_message_tool._send_to_platform",
               new=AsyncMock(return_value={"success": True})) as send_mock, \
         patch("gateway.mirror.mirror_to_session", return_value=True), \
         patch.dict("os.environ", {"HERMES_OWNER_IDS": "U_OWNER"}, clear=False):
        result = json.loads(
            send_message_tool({"action": "send", "target": "slack:C123456", "message": "hi 조이"})
        )
    assert result["success"] is True            # delegation → send proceeds
    block_mock.assert_not_called()               # NOT blocked
    send_mock.assert_awaited_once()              # actually sent
    audit_mock.assert_called_once()
    kwargs = audit_mock.call_args.kwargs
    assert kwargs["source"] == "delegated"
    assert kwargs["status"] == "delegated"
    assert kwargs["rationale"] == "delegation:dlg_test"


def test_describe_actor_falls_back_to_id():
    from tools.send_message_tool import _describe_actor
    assert _describe_actor("") == "알 수 없는 사용자"
    # An id not in the roster resolves to itself (no crash).
    assert "U_UNKNOWN" in _describe_actor("U_UNKNOWN")


def test_escalate_returns_false_without_slack_token():
    from tools.send_message_tool import _escalate_on_behalf_to_owner
    slack_cfg = SimpleNamespace(enabled=True, token="", extra={})
    config = SimpleNamespace(platforms={Platform.SLACK: slack_cfg})
    with patch.dict("os.environ", {"HERMES_OWNER_IDS": "U_OWNER"}, clear=False):
        ok = _escalate_on_behalf_to_owner(
            config, actor_label="Eric", platform_name="slack",
            target_ref="C999", preview="hi",
        )
    assert ok is False


def test_escalate_returns_false_without_owner_id():
    from tools.send_message_tool import _escalate_on_behalf_to_owner
    slack_cfg = SimpleNamespace(enabled=True, token="xoxb-test", extra={})
    config = SimpleNamespace(platforms={Platform.SLACK: slack_cfg})
    with patch.dict("os.environ", {"HERMES_OWNER_IDS": ""}, clear=False):
        ok = _escalate_on_behalf_to_owner(
            config, actor_label="Eric", platform_name="slack",
            target_ref="C999", preview="hi",
        )
    assert ok is False


def test_session_less_no_gate_but_audited():
    env = _session_env_factory()  # all empty → session-less (CLI/MCP)
    result, approval_mock, audit_mock = _invoke(env, owner_ids="U_OWNER")
    assert result["success"] is True
    approval_mock.assert_not_called()            # no live session → cannot prompt
    audit_mock.assert_called_once()              # close the session-less audit hole
    kwargs = audit_mock.call_args.kwargs
    assert kwargs["source"] == "session-less"
    assert kwargs["action_class"] == "send"
    assert kwargs["status"] == "success"
