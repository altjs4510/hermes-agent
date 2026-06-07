"""W7 P2 shadow EQUIVALENCE test — the proof that the policy engine reproduces
L1 tier behavior exactly (0 divergence) before P3 flips enforcement on.

For a representative tool set, we compute L1's drop decision (replicating
agent_init.py:_blocked for executive and other tiers) and the engine's
decision (capability_of + authz.evaluate), and assert they agree for EVERY
tool. Any disagreement here is exactly what the live shadow logger would flag.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import gateway.authz as authz
from agent.identity import ActorId
from agent.tool_capabilities import capability_of

OWNER, EXEC, OTHER = "U_OWNER", "U_EXEC", "U_OTHER"

# Replicated from agent_init.py:961-1004 (the L1 source of truth).
_OWNER_ONLY_TOOLS = {
    "write_file", "patch", "terminal", "process", "execute_code", "computer_use",
    "memory", "todo", "skill_manage", "cronjob", "delegate_task",
    "ha_call_service", "spotify_playback", "spotify_queue", "discord_admin",
}
_OTHER_BLOCKED_TOOLS = _OWNER_ONLY_TOOLS | {
    "send_message", "discord",
    "browser_navigate", "browser_click", "browser_type", "browser_scroll",
    "browser_back", "browser_press", "browser_cdp", "browser_dialog",
    "image_generate", "video_generate", "text_to_speech",
    "feishu_drive_reply_comment", "feishu_drive_add_comment",
    "yb_send_dm", "yb_send_sticker",
    "kanban_create", "kanban_complete", "kanban_block", "kanban_unblock",
    "kanban_comment", "kanban_link", "kanban_heartbeat",
}
_MCP_WRITE_VERBS = {
    "create", "update", "delete", "send", "post", "add", "remove",
    "write", "set", "move", "upload", "restore", "archive", "invite",
    "reply", "edit", "patch", "rename", "cancel", "approve", "submit",
}


def _l1_blocked(name, *, is_executive):
    if is_executive:
        return name in _OWNER_ONLY_TOOLS
    if name in _OTHER_BLOCKED_TOOLS:
        return True
    low = name.lower()
    if low.startswith("mcp") and (set(low.split("_")) & _MCP_WRITE_VERBS):
        return True
    return False


# Representative tool universe: every tier-relevant tool + reads + MCP variants.
_TOOLS = sorted(_OTHER_BLOCKED_TOOLS | {
    "read_file", "search_files", "kanban_show", "kanban_list", "web_search",
    "mcp__notion__notion-fetch",            # mcp read
    "mcp_slack_search_public",              # mcp read
    "mcp_ms365_mcp_create_calendar_event",  # mcp write (underscore verb) — L1 blocks for other
    "mcp__notion__notion-update-page",      # mcp write (hyphen verb) — L1 blind spot, NOT blocked
    "some_unknown_future_tool",             # unlisted → allowed for all
})


@pytest.fixture(autouse=True)
def _empty_policy(tmp_path):
    (tmp_path / "authority.yaml").write_text("roles: {}\ngrants: []\ndenies: []\n", encoding="utf-8")
    authz._cache["mtime"] = None
    authz._cache["policy"] = None
    with patch("gateway.authz._policy_path", return_value=tmp_path / "authority.yaml"), \
         patch.dict("os.environ", {"HERMES_OWNER_IDS": OWNER, "HERMES_EXECUTIVE_IDS": EXEC}, clear=False):
        yield
    authz._cache["mtime"] = None
    authz._cache["policy"] = None


def test_executive_engine_matches_l1_zero_divergence():
    actor = ActorId("user", EXEC)
    divergences = []
    for name in _TOOLS:
        l1_allow = not _l1_blocked(name, is_executive=True)
        engine_allow = authz.evaluate(actor, capability_of(name)).allow
        if engine_allow != l1_allow:
            divergences.append((name, capability_of(name), l1_allow, engine_allow))
    assert divergences == [], f"executive divergences: {divergences}"


def test_other_engine_matches_l1_zero_divergence():
    actor = ActorId("user", OTHER)
    divergences = []
    for name in _TOOLS:
        l1_allow = not _l1_blocked(name, is_executive=False)
        engine_allow = authz.evaluate(actor, capability_of(name)).allow
        if engine_allow != l1_allow:
            divergences.append((name, capability_of(name), l1_allow, engine_allow))
    assert divergences == [], f"other divergences: {divergences}"


def test_owner_never_blocked():
    actor = ActorId("user", OWNER)
    for name in _TOOLS:
        assert authz.evaluate(actor, capability_of(name)).allow is True
