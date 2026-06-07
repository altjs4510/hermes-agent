"""Append-only structured ledger for external side-effect actions (W1).

Records every external / state-changing action the bot performs — message
sends, file writes, shell exec, MCP writes, self-improvement proposals —
into one queryable JSONL ledger, independent of the owner-confirm approval
store (``audit/owner-confirm.jsonl``).

This is **observability, not a gate**: recording never blocks an action
(all writes are best-effort under ``try/except``) and the store changes no
approval behavior. See ``docs/auth-audit-redesign.md`` (W1).

Two integration points feed this ledger (W0 call-site matrix):

  1. ``model_tools._emit_post_tool_call_hook`` — fires for every
     agent-invoked side-effect TOOL (send_message, write_file, patch,
     terminal, mcp writes, self_improvement). This is the convergence point
     for paths ①③⑤⑦ of the W0 matrix. The audit emit sits *before* the
     plugin ``has_hook`` gate so it records regardless of plugin
     registration.
  2. ``cron.scheduler._deliver_result`` — no_agent cron deliveries that
     bypass the tool path entirely (morning-brief etc.; W0 paths ②③b).

Out of scope (out-of-process, documented in W0): CLI ``hermes send``, MCP
server send, and manually-run skill scripts — they run in separate
processes that never reach either hook.

Performance: records are buffered (size *and* interval) under a single lock
and flushed with one ``fsync`` per batch, so the hot tool-execution path
(up to ``_MAX_TOOL_WORKERS`` concurrent workers) never serializes on
per-record fsync. ``atexit`` flushes the tail on clean shutdown. Hard-crash
loss is bounded by the batch: ≤ ``BATCH_SIZE`` records or
``FLUSH_INTERVAL_SEC`` of buffered activity.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

EVENT_VERSION = "1.0"
BATCH_SIZE = 10
FLUSH_INTERVAL_SEC = 2.0

# ── side-effect classification ────────────────────────────────────────────
# Curated to the W0 matrix §part-2 side-effect tools. Read tools fall through
# to ``None`` via O(1) set lookups so the hot path skips them cheaply.

_SEND_TOOLS = frozenset({"send_message"})
_WRITE_TOOLS = frozenset({"write_file", "patch", "apply_patch", "edit_file", "create_file"})
_EXEC_TOOLS = frozenset({"terminal", "execute_code"})
_SELF_IMP_TOOLS = frozenset({"self_improvement", "propose_self_improvement"})
_MCP_WRITE_VERBS = ("create", "update", "delete", "send", "post", "write", "merge", "upload", "remove")
_BROWSER_WRITE_VERBS = ("click", "fill", "type", "navigate", "press", "select", "drag", "upload", "submit", "goto")


def classify_side_effect(tool_name: str, args: Optional[dict]) -> Optional[tuple[str, str]]:
    """Return ``(action_class, target_ref)`` for side-effect tools, else ``None``.

    O(1) set lookups first so non-side-effect (read) tools skip the rest.
    """
    name = tool_name or ""
    args = args if isinstance(args, dict) else {}
    if name in _SEND_TOOLS:
        target = (args.get("target") or args.get("channel") or args.get("chat_id")
                  or args.get("to") or args.get("recipient") or "")
        return ("send", str(target)[:200])
    if name in _WRITE_TOOLS:
        return ("write", str(args.get("path") or args.get("file_path") or "")[:200])
    if name in _EXEC_TOOLS:
        return ("exec", str(args.get("command") or args.get("code") or "")[:120])
    if name in _SELF_IMP_TOOLS:
        return ("self_improvement", "owner_dm")
    low = name.lower()
    if low.startswith("mcp") and any(v in low for v in _MCP_WRITE_VERBS):
        return ("mcp_write", name)
    if low.startswith("browser_") and any(v in low for v in _BROWSER_WRITE_VERBS):
        return ("browser", str(args.get("url") or args.get("selector") or "")[:120])
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _truncate(value: Any, limit: int) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return s[:limit] + "…" if len(s) > limit else s


# Map the runtime's native tool-result vocab ("ok"/"error") onto the documented
# ledger status enum (success|failed|blocked|cancelled) so the file is queryable
# against the schema in docs/auth-audit-redesign.md.
_STATUS_ALIASES = {"ok": "success", "error": "failed", "": "unknown"}


def _normalize_status(status: Optional[str]) -> str:
    s = (status or "").strip().lower()
    return _STATUS_ALIASES.get(s, s or "unknown")


class SideEffectAuditStore:
    """Append-only JSONL ledger with batched, lock-guarded writes."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        batch_size: int = BATCH_SIZE,
        flush_interval_sec: float = FLUSH_INTERVAL_SEC,
    ):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._buffer: list[str] = []
        self._batch_size = max(1, batch_size)
        self._flush_interval = max(0.0, flush_interval_sec)
        self._last_flush = time.monotonic()

    def record(self, **fields: Any) -> None:
        """Buffer one side-effect event. Best-effort — never raises."""
        try:
            event: dict[str, Any] = {"event_version": EVENT_VERSION, "ts": _utc_now_iso()}
            for key, value in fields.items():
                if value is None:
                    continue
                event[key] = value
            line = json.dumps(event, ensure_ascii=False, sort_keys=True)
            with self._lock:
                self._buffer.append(line)
                due = (
                    len(self._buffer) >= self._batch_size
                    or (time.monotonic() - self._last_flush) >= self._flush_interval
                )
                if due:
                    self._flush_unlocked()
        except Exception:
            pass

    def _flush_unlocked(self) -> None:
        if not self._buffer:
            return
        lines = self._buffer
        self._buffer = []
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            # Best-effort observability: on persistent write failure, drop the
            # batch rather than re-buffer (which would grow unbounded).
            pass
        finally:
            self._last_flush = time.monotonic()

    def flush(self) -> None:
        with self._lock:
            self._flush_unlocked()


_STORE: Optional[SideEffectAuditStore] = None
_STORE_LOCK = threading.Lock()


def _default_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "audit" / "side-effect-actions.jsonl"
    except Exception:
        return Path(os.path.expanduser("~/.hermes/audit/side-effect-actions.jsonl"))


def get_store() -> SideEffectAuditStore:
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                store = SideEffectAuditStore(_default_path())
                atexit.register(store.flush)
                _STORE = store
    return _STORE


def record_side_effect(
    *,
    tool_name: str,
    action_class: str,
    source: str,
    status: str,
    actor: Optional[str] = None,
    target_ref: Optional[str] = None,
    args: Any = None,
    result_preview: Any = None,
    duration_ms: Optional[int] = None,
    error_type: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    task_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    rationale: Optional[str] = None,
) -> None:
    """Buffer one side-effect record. Truncates ``args``/``result_preview``."""
    get_store().record(
        tool_name=tool_name,
        action_class=action_class,
        source=source,
        status=_normalize_status(status),
        actor=actor,
        target_ref=target_ref,
        args=_truncate(args, 300) if args is not None else None,
        result_preview=_truncate(result_preview, 200) if result_preview is not None else None,
        duration_ms=duration_ms,
        error_type=error_type,
        blocked_reason=blocked_reason,
        task_id=task_id,
        tool_call_id=tool_call_id,
        turn_id=turn_id,
        rationale=rationale,
    )
