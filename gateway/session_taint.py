"""Session-scoped *content taint* tracking for egress lockdown (Phase 1).

The egress-lockdown threat model (docs/plans/2026-06-08-tool-egress-lockdown.md)
is: ``private data + untrusted external content + external egress path``. The
defense is NOT "ask the model whether some text is a prompt injection" — it is
to record, deterministically, that this session has *touched external/untrusted
content*, and to let a downstream egress policy decide what outbound tools may
then do.

This module owns only the first half: "has this session read external/private
content, and from where?". It answers *state*, never *what's allowed* (that is
``gateway/egress_policy.py``), mirroring the identity↔authz split.

Scoping: task-local via ``contextvars`` — the SAME pattern as
``gateway/session_context.py`` — so two concurrently-handled messages never
share taint. A child ``asyncio`` task inherits the parent's taint object by
copy-of-context (desired: a sub-task of a tainted turn is still tainted).

Phase 1 is **shadow only**: taint is recorded and read by the egress policy in
shadow mode, but nothing here changes tool behavior.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# Trust levels, coarsest first. ``external`` is the prompt-injection carrier we
# most care about; ``internal``/``private`` are instruction-bearing too (a
# forwarded mail/doc can carry injected instructions) but not public web.
TRUST_EXTERNAL = "external"
TRUST_INTERNAL = "internal"
TRUST_PRIVATE = "private"
TRUST_UNKNOWN = "unknown"


@dataclass
class TaintEvent:
    """One read that introduced (potentially) untrusted content this session."""

    source_class: str  # web | browser | messaging | mcp | file | email | unknown
    source_ref: str    # URL, platform:channel, message id, file path, tool name
    trust_level: str   # external | internal | private | unknown
    tool_name: str
    ts: str


@dataclass
class SessionTaint:
    """Accumulated content-taint for the current session/task."""

    external_content_seen: bool = False
    private_context_seen: bool = False
    events: list[TaintEvent] = field(default_factory=list)

    @property
    def any_taint(self) -> bool:
        return self.external_content_seen or self.private_context_seen

    def add(self, event: TaintEvent) -> None:
        if event.trust_level == TRUST_EXTERNAL:
            self.external_content_seen = True
        elif event.trust_level in (TRUST_INTERNAL, TRUST_PRIVATE):
            self.private_context_seen = True
        # unknown trust taints nothing on its own but is still recorded.
        self.events.append(event)


# Default ``None`` (NOT a shared mutable SessionTaint): a mutable default would
# be shared across every contextvar context. We lazily create one per context
# on first write so each task gets its own instance.
_SESSION_TAINT: ContextVar[Optional[SessionTaint]] = ContextVar(
    "HERMES_SESSION_TAINT", default=None
)


def current_taint() -> SessionTaint:
    """Return this task's taint, creating an empty one if none exists yet."""
    taint = _SESSION_TAINT.get()
    if taint is None:
        taint = SessionTaint()
        _SESSION_TAINT.set(taint)
    return taint


def clear_taint() -> None:
    """Reset taint for the current context (session boundary / tests).

    Sets ``None`` (not a concrete instance) deliberately: a child ``asyncio``
    task copies the parent context by reference, so a shared SessionTaint object
    would leak across concurrent tasks. ``None`` forces each task to lazily
    create its own via ``current_taint()`` — see ``test_taint_is_task_isolated``.
    """
    _SESSION_TAINT.set(None)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ── read-source classification ────────────────────────────────────────────
# Conservative initial mapping (plan §"Source classification"). Only clear
# *read* tools taint — side-effect tools are NEVER classified here (the post
# hook must not mistake an egress call for a read).

_WEB_READ = frozenset({"web_search", "web_extract", "web_fetch", "x_search"})
# Browser tools that surface page content into context. Navigation/click/type
# are deliberately EXCLUDED here: they are ``browse`` egress actions, not reads,
# and the page they load is captured by a following snapshot.
_BROWSER_READ = frozenset(
    {"browser_snapshot", "browser_console", "browser_get_images", "browser_vision"}
)
_FILE_READ = frozenset({"read_file", "search_files", "grep"})


def classify_read_source(
    tool_name: str, args: Optional[dict] = None, result: object = None
) -> Optional[TaintEvent]:
    """Classify a *read* tool into a TaintEvent, or ``None`` if it is not a
    taint-introducing read.

    Deterministic, never raises. ``args``/``result`` are accepted for future
    refinement (e.g. URL/source extraction) and used best-effort for
    ``source_ref``.
    """
    name = tool_name or ""
    args = args if isinstance(args, dict) else {}

    # Never taint a known side-effect tool as a read. classify_side_effect
    # catches hyphenated MCP write verbs (mcp__notion__notion-update-page) that
    # capability_of's "_"-only split misses, so this keeps the two classifiers
    # consistent: a mutating call is egress, not a read.
    try:
        from gateway.side_effect_audit import classify_side_effect

        if classify_side_effect(name, args) is not None:
            return None
    except Exception:
        pass

    if name in _WEB_READ:
        ref = str(args.get("url") or args.get("urls") or args.get("query") or name)[:200]
        return TaintEvent("web", ref, TRUST_EXTERNAL, name, _utc_now_iso())
    if name in _BROWSER_READ:
        ref = str(args.get("url") or name)[:200]
        return TaintEvent("browser", ref, TRUST_EXTERNAL, name, _utc_now_iso())
    if name in _FILE_READ:
        ref = str(args.get("path") or args.get("file_path") or args.get("pattern") or name)[:200]
        return TaintEvent("file", ref, TRUST_PRIVATE, name, _utc_now_iso())

    low = name.lower()
    if low.startswith("mcp"):
        # Internal/private instruction-bearing content (M365/Notion/Slack reads).
        # Only *read* MCP tools taint; mutating MCP verbs are egress, classified
        # by capability_of as ``mcp_write`` and handled by the egress policy.
        try:
            from agent.tool_capabilities import capability_of

            if capability_of(name) == "read":
                return TaintEvent("mcp", name, TRUST_INTERNAL, name, _utc_now_iso())
        except Exception:
            pass
    return None


def mark_read(tool_name: str, args: Optional[dict] = None, result: object = None) -> Optional[TaintEvent]:
    """Classify ``tool_name`` and, if it is a taint-introducing read, record it
    on the current session taint. Returns the recorded event (or ``None``).

    Best-effort: never raises, never blocks the tool.
    """
    try:
        event = classify_read_source(tool_name, args, result)
        if event is None:
            return None
        current_taint().add(event)
        return event
    except Exception:
        return None
