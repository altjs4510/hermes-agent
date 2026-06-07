"""Tests for agent/tool_capabilities.py (W7 P1).

The critical property is EQUIVALENCE with the current L1 lists in
agent_init.py: every owner-only tool must classify into an owner-only
capability, every other-blocked tool into an executive-or-owner capability,
and MCP write-shape into mcp_write. These lists are copied from
agent_init.py:961-998 — if that code changes, this test must be updated in
lockstep (that is the point: the mapping is now explicit, not guessed).
"""

import pytest

from agent.tool_capabilities import (
    capability_of,
    OWNER_ONLY_CAPABILITIES,
    EXECUTIVE_CAPABILITIES,
)

# Copied verbatim from agent_init.py:961-998.
_OWNER_ONLY_TOOLS = {
    "write_file", "patch",
    "terminal", "process", "execute_code", "computer_use",
    "memory", "todo", "skill_manage", "cronjob", "delegate_task",
    "ha_call_service", "spotify_playback", "spotify_queue",
    "discord_admin",
}
_OTHER_BLOCKED_EXTRA = {
    "send_message", "discord",
    "browser_navigate", "browser_click", "browser_type", "browser_scroll",
    "browser_back", "browser_press", "browser_cdp", "browser_dialog",
    "image_generate", "video_generate", "text_to_speech",
    "feishu_drive_reply_comment", "feishu_drive_add_comment",
    "yb_send_dm", "yb_send_sticker",
    "kanban_create", "kanban_complete", "kanban_block", "kanban_unblock",
    "kanban_comment", "kanban_link", "kanban_heartbeat",
}


@pytest.mark.parametrize("tool", sorted(_OWNER_ONLY_TOOLS))
def test_owner_only_tools_map_to_owner_only_capability(tool):
    assert capability_of(tool) in OWNER_ONLY_CAPABILITIES


@pytest.mark.parametrize("tool", sorted(_OTHER_BLOCKED_EXTRA))
def test_other_blocked_tools_map_to_executive_capability(tool):
    # These are blocked for "other" but allowed for executive → must NOT be
    # owner-only, and must be in the executive-capability set.
    cap = capability_of(tool)
    assert cap in EXECUTIVE_CAPABILITIES
    assert cap not in OWNER_ONLY_CAPABILITIES


def test_read_tools_are_read():
    for tool in ("read_file", "search_files", "kanban_show", "kanban_list", "web_search"):
        assert capability_of(tool) == "read"


def test_mcp_write_shape_is_mcp_write():
    # Underscore-separated verb (the style agent_init targets) → caught.
    assert capability_of("mcp_ms365_mcp_create_calendar_event") == "mcp_write"


def test_mcp_hyphenated_verb_is_inherited_blind_spot():
    # KNOWN GAP, reproduced for equivalence: agent_init's name-shape splits on
    # "_" only, so a hyphenated verb (claude.ai style mcp__server__verb-noun)
    # is NOT caught — there 'notion-update-page' is one token. capability_of
    # reproduces this exactly (→ read) so the P2 shadow shows 0 divergence.
    # Closing this gap is a deliberate, separately-reviewed change, NOT a
    # silent half-fix smuggled into the equivalence layer.
    assert capability_of("mcp__notion__notion-update-page") == "read"


def test_mcp_read_shape_is_read():
    assert capability_of("mcp__notion__notion-fetch") == "read"
    assert capability_of("mcp_slack_search_public") == "read"


def test_unknown_non_mcp_tool_is_read_for_equivalence():
    # Currently unlisted non-MCP tools are blocked for no one → read.
    assert capability_of("some_brand_new_tool") == "read"


def test_capability_sets_are_disjoint():
    assert OWNER_ONLY_CAPABILITIES.isdisjoint(EXECUTIVE_CAPABILITIES)
