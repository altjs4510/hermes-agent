"""Declarative tool capability taxonomy (W7 / P1).

Replaces L1's two name-list drop sets + the MCP write-verb *name-shape guess*
(``agent_init.py:961-1012``) with an explicit ``tool name → capability`` map.
A capability is the unit per-person policy grants/denies on (see gateway/authz.py),
so this table is the foundation the whole authority model rests on.

Capabilities (coarsest meaningful axis, derived from the existing grouping
comments in agent_init):

  read      — read_file, search_files, *_show/_list, MCP reads (everyone)
  send      — external messaging (send_message, discord, feishu/yb sends)
  browse    — browser navigation/interaction
  generate  — image/video/tts generation (cost + artifacts)
  collab    — kanban mutations (shared board writes)
  mcp_write — mutating MCP tools whose capability isn't explicitly declared
  write     — local filesystem writes (write_file, patch)
  exec      — shell / process / code execution
  admin     — identity/config/personal stores/spawning/platform admin

Role defaults that reproduce today's L1 (owner / executive / other) live in
gateway/authz.py — NOT here. This module only answers "what kind of action is
this tool?", deterministically.

EQUIVALENCE NOTE (read before changing): for the P2 shadow comparison to show
ZERO divergence from current L1, the *role defaults* applied to these
capabilities must reproduce L1 exactly:
  - {write, exec, admin}            → owner only          (= _OWNER_ONLY_TOOLS)
  - {send, browse, generate, collab, mcp_write} → owner + executive
                                       (= _OTHER_BLOCKED_TOOLS minus owner-only)
  - {read} and UNKNOWN tools        → everyone
Today an unlisted non-MCP tool is blocked for NO ONE, so unknown → ``read``
here (equivalence). Tightening unknown tools to a restrictive default is a
deliberate P3 *policy* choice (it WILL diverge — that's what shadow mode
surfaces for review), not a P1 classification change.
"""

from __future__ import annotations

# Mutating MCP verb segments — kept identical to agent_init.py:1000-1004 so the
# fallback reproduces the current name-shape block exactly.
#
# KNOWN INHERITED BLIND SPOT (reproduced deliberately for equivalence): the
# match splits the tool name on "_" only, so a *hyphenated* verb in the
# claude.ai MCP style (``mcp__server__verb-noun``, e.g.
# ``mcp__notion__notion-update-page``) is NOT caught — there the verb lives
# inside one token ``notion-update-page``. agent_init has the same gap today.
# capability_of mirrors it (→ ``read``) so the P2 shadow shows 0 divergence.
# Closing it (also splitting on "-") is a deliberate, separately-reviewed
# tightening — not a silent change inside the equivalence layer.
_MCP_WRITE_VERBS = {
    "create", "update", "delete", "send", "post", "add", "remove",
    "write", "set", "move", "upload", "restore", "archive", "invite",
    "reply", "edit", "patch", "rename", "cancel", "approve", "submit",
}

# Explicit table — grounded in agent_init.py:961-998 groupings.
_EXPLICIT: dict[str, str] = {
    # write — filesystem writes
    "write_file": "write", "patch": "write",
    # exec — shell / process / code
    "terminal": "exec", "process": "exec", "execute_code": "exec", "computer_use": "exec",
    # admin — identity / config / personal / spawning / platform admin
    "memory": "admin", "todo": "admin", "skill_manage": "admin",
    "cronjob": "admin", "delegate_task": "admin",
    "ha_call_service": "admin", "spotify_playback": "admin", "spotify_queue": "admin",
    "discord_admin": "admin",
    # send — external messaging
    "send_message": "send", "discord": "send",
    "feishu_drive_reply_comment": "send", "feishu_drive_add_comment": "send",
    "yb_send_dm": "send", "yb_send_sticker": "send",
    # browse — browser interaction
    "browser_navigate": "browse", "browser_click": "browse", "browser_type": "browse",
    "browser_scroll": "browse", "browser_back": "browse", "browser_press": "browse",
    "browser_cdp": "browse", "browser_dialog": "browse",
    # generate — media generation
    "image_generate": "generate", "video_generate": "generate", "text_to_speech": "generate",
    # collab — kanban mutations
    "kanban_create": "collab", "kanban_complete": "collab", "kanban_block": "collab",
    "kanban_unblock": "collab", "kanban_comment": "collab", "kanban_link": "collab",
    "kanban_heartbeat": "collab",
}

# Capabilities granted by the owner-only tier ceiling (= _OWNER_ONLY_TOOLS).
OWNER_ONLY_CAPABILITIES = frozenset({"write", "exec", "admin"})
# Capabilities an executive keeps but "other" loses.
EXECUTIVE_CAPABILITIES = frozenset({"send", "browse", "generate", "collab", "mcp_write"})


def capability_of(tool_name: str) -> str:
    """Classify a tool into one capability. Deterministic; never raises.

    Explicit table first; then MCP name-shape fallback (mutating verb →
    ``mcp_write``, else ``read``); then unknown non-MCP → ``read`` (equivalence
    — see module docstring).
    """
    name = tool_name or ""
    if name in _EXPLICIT:
        return _EXPLICIT[name]
    low = name.lower()
    if low.startswith("mcp"):
        if set(low.split("_")) & _MCP_WRITE_VERBS:
            return "mcp_write"
        return "read"
    return "read"
