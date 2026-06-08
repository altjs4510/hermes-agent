"""Deterministic egress policy for tool side effects under content taint
(egress lockdown — Phase 1, shadow only).

Pairs with ``gateway/session_taint.py`` (which answers "did this session read
external/private content?") to answer the second half: "given that taint, what
should this outbound side-effect tool be allowed to do?".

The whole point (per OpenAI Lockdown Mode, adapted) is that this is a
*deterministic policy*, not a model judgment: once external content has entered
the session, the LAST egress path (send / mcp_write / browser action / write /
exec) is governed here — allow, confirm, or block — rather than trusting the
model to notice an injected instruction.

Phase 1 contract — **no behavior change**:
  - ``evaluate_egress`` computes the *latent* decision (what we WOULD do).
  - The dispatch preflight runs it in ``mode="shadow"``: it records a
    ``would_allow``/``would_confirm``/``would_block`` audit event and then
    ALWAYS proceeds. Enforcement (send-only, then mcp/browser/exec) is Phase 2+.

This module reuses the capability taxonomy (``agent/tool_capabilities.py``) and
does NOT replace the W7 authz engine — authz answers "may this actor use this
capability at all?"; egress policy adds "...given the session has touched
untrusted content, and to this resource?".
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from gateway.session_taint import SessionTaint

# Capabilities that are an *egress* path worth governing under taint. Read /
# generate / collab / admin are out of Phase 1 scope (plan §"Egress
# classification"). Order of this set has no meaning.
EGRESS_CAPABILITIES = frozenset({"send", "mcp_write", "browse", "write", "exec"})

# Modes (plan §"Policy file"): Phase 1 ships ``shadow`` everywhere.
MODE_SHADOW = "shadow"
MODE_ENFORCE_SEND = "enforce_send"
MODE_ENFORCE_ALL = "enforce_all"

_lock = threading.Lock()
_cache: dict[str, Any] = {"mtime": None, "policy": None}


@dataclass(frozen=True)
class EgressDecision:
    action: str  # allow | confirm | block | audit_only
    rule_id: str
    reason: str
    risk_level: str  # none | low | medium | high


def _policy_path() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "context" / "egress.yaml"
    except Exception:
        return Path(os.path.expanduser("~/.hermes/context/egress.yaml"))


def _default_policy() -> dict:
    # Missing/invalid file → shadow mode with no extra allowlist. Built-in rules
    # in ``evaluate_egress`` still apply (this only augments the send allowlist).
    return {"mode": MODE_SHADOW, "allow": [], "confirm": [], "block": []}


def _load_policy() -> dict:
    path = _policy_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        with _lock:
            _cache["mtime"] = None
            _cache["policy"] = _default_policy()
        return _cache["policy"]
    with _lock:
        if _cache["mtime"] == mtime and _cache["policy"] is not None:
            return _cache["policy"]
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        policy = {
            "mode": str(data.get("mode") or MODE_SHADOW),
            "allow": data.get("allow") or [],
            "confirm": data.get("confirm") or [],
            "block": data.get("block") or [],
        }
    except Exception:
        policy = _default_policy()
    with _lock:
        _cache["mtime"] = mtime
        _cache["policy"] = policy
    return policy


def get_mode() -> str:
    """Current egress enforcement mode. Phase 1 default: ``shadow``."""
    return _load_policy().get("mode", MODE_SHADOW)


def _send_resource_allowlisted(resource: str, policy: dict) -> Optional[str]:
    """Return the matching allow-rule id if ``resource`` is an allowlisted send
    target, else ``None``. Built-in safe targets plus any ``allow`` entries in
    egress.yaml with ``capability: send``.
    """
    res = (resource or "").strip()
    # Built-in safe same-surface targets (a normal reply, not exfil).
    if res in ("", "current_thread", "same_thread"):
        return "same-thread-reply"
    for entry in policy.get("allow", []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("capability", "")).strip().lower() not in ("send", "*"):
            continue
        rule_res = str(entry.get("resource", "")).strip()
        if rule_res in ("*", res):
            return str(entry.get("id", "allow"))
    return None


def evaluate_egress(
    actor: Any,
    capability: str,
    resource: str,
    taint: SessionTaint,
    args: Optional[dict] = None,
) -> EgressDecision:
    """Compute the *latent* egress decision for one side-effect tool call.

    This is mode-independent: it answers "what would the policy do?". The caller
    decides whether to enforce it (Phase 2+) or only record it (Phase 1 shadow).

    Deterministic order:
      1. non-egress capability → allow (out of scope).
      2. no content taint → allow (preserve current authz/send-gate behavior).
      3. egress under taint → per-capability decision (allowlist > confirm > block).
    """
    cap = (capability or "").strip().lower()
    args = args if isinstance(args, dict) else {}

    if cap not in EGRESS_CAPABILITIES:
        return EgressDecision("allow", "egress:not-egress", f"{cap} is not an egress path", "none")

    if not taint.any_taint:
        return EgressDecision("allow", "egress:no-taint", "no external/private content read this session", "none")

    policy = _load_policy()
    external = taint.external_content_seen

    if cap == "send":
        allow_id = _send_resource_allowlisted(resource, policy)
        if allow_id is not None:
            return EgressDecision("allow", f"egress:{allow_id}", "allowlisted send target", "low")
        return EgressDecision(
            "confirm",
            "egress:tainted-external-send" if external else "egress:tainted-internal-send",
            "side-effect send after reading untrusted content; target not allowlisted",
            "high" if external else "medium",
        )

    if cap == "mcp_write":
        return EgressDecision(
            "confirm",
            "egress:tainted-mcp-write",
            "MCP write after reading untrusted content",
            "high" if external else "medium",
        )

    if cap == "browse":
        return EgressDecision(
            "confirm",
            "egress:tainted-browser-action",
            "browser action (submit/click/type/navigate) after reading untrusted content",
            "medium",
        )

    if cap == "exec":
        return EgressDecision(
            "confirm",
            "egress:tainted-exec",
            "shell/code execution after reading untrusted content (may egress)",
            "high" if external else "medium",
        )

    if cap == "write":
        # Local drafts/specs are normal; do not blanket-block (plan open
        # decision #5). Phase 1: allow + low-risk audit signal only.
        return EgressDecision(
            "allow",
            "egress:tainted-local-write",
            "local write after taint — allowed, not publishable egress",
            "low",
        )

    # Unreachable given EGRESS_CAPABILITIES, but fail-closed-ish to confirm.
    return EgressDecision("confirm", "egress:tainted-unknown", f"unclassified egress {cap}", "medium")
