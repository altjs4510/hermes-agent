"""Current AIAgent ContextVar — lets tool handlers reach the active agent.

Tool handlers are invoked as ``handler(args, **kwargs)`` and do NOT receive the
agent instance. This ContextVar is set per turn (conversation_loop, at turn
entry) and propagated to tool-executor threads by the same context-copy
mechanism that carries ``skill_provenance`` — so a handler that needs to mutate
live agent state can reach it.

Current use: governed-skill tool re-grant in ``tools/skills_tool.py`` — when an
authorized requester loads a governance-registered skill via ``skill_view``, the
handler re-grants that skill's declared ``tool_scope`` onto the live agent
(tier-drop exception). Gated by ``HERMES_GOVERNED_TOOL_GRANT`` and the skill's
``governance.triggerers``. See ~/.hermes/persona/permissions.md §9.

Deliberately minimal and read-mostly: handlers should treat the agent as live
state to inspect/extend, not to reconfigure.
"""
import contextvars

_current_agent: contextvars.ContextVar = contextvars.ContextVar(
    "current_agent", default=None
)


def set_current_agent(agent):
    """Bind the active agent to the current context (per turn). Overwrites each
    turn; no reset needed since the agent is re-initialized per turn."""
    return _current_agent.set(agent)


def get_current_agent():
    """Return the active agent, or None (CLI/subagent/no-turn contexts)."""
    return _current_agent.get()
