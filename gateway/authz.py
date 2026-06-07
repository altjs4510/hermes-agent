"""Central authorization policy engine (W7 / P2).

The single ``evaluate(actor, capability, resource) -> Decision`` that the
scattered enforcement points (L1 tool drop, send gates, slash, ...) will
delegate to. Today only the L1 *shadow* comparison consults it (P2): it
computes what it WOULD decide and logs divergence from current behavior,
WITHOUT enforcing. Enforcement is flipped on at P3 once shadow shows the
engine reproduces L1.

Policy model: RBAC (roles = the existing owner/executive/other tiers, as
capability defaults) + per-person grant/deny overrides (scope + expiry +
provenance). Decision order is deterministic and every decision names the
rule that produced it (``rule_id``) so "why?" is always answerable — the exact
gap (preached-but-not-coded) this whole project closes.

Policy file: ``~/.hermes/context/authority.yaml`` (owner-edited; absorbs
delegations.yaml at P4). EMPTY/missing file → pure role defaults → reproduces
current L1 exactly. Owner and the local/system principals BYPASS policy
(break-glass: a broken policy file can never lock the owner out).
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agent.tool_capabilities import OWNER_ONLY_CAPABILITIES

_lock = threading.Lock()
_cache: dict[str, Any] = {"mtime": None, "policy": None}


@dataclass(frozen=True)
class Decision:
    allow: bool
    rule_id: str
    reason: str


def _policy_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "context" / "authority.yaml"
    except Exception:
        return Path(os.path.expanduser("~/.hermes/context/authority.yaml"))


def _owner_ids() -> set[str]:
    return {u.strip() for u in os.getenv("HERMES_OWNER_IDS", "").split(",") if u.strip()}


def _executive_ids() -> set[str]:
    return {u.strip() for u in os.getenv("HERMES_EXECUTIVE_IDS", "").split(",") if u.strip()}


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _load_policy() -> dict:
    """Load + cache authority.yaml. Missing/invalid → empty policy (defaults)."""
    path = _policy_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        with _lock:
            _cache["mtime"] = None
            _cache["policy"] = {"roles": {}, "grants": [], "denies": []}
        return _cache["policy"]
    with _lock:
        if _cache["mtime"] == mtime and _cache["policy"] is not None:
            return _cache["policy"]
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        policy = {
            "roles": data.get("roles") or {},
            "grants": data.get("grants") or [],
            "denies": data.get("denies") or [],
        }
    except Exception:
        policy = {"roles": {}, "grants": [], "denies": []}
    with _lock:
        _cache["mtime"] = mtime
        _cache["policy"] = policy
    return policy


def role_of(actor) -> str:
    """Map an actor to a role: owner | executive | other | system | local."""
    if actor.kind in ("local", "system"):
        return actor.kind
    if actor.id in _owner_ids():
        return "owner"
    if actor.id in _executive_ids():
        return "executive"
    return "other"


def _role_allows(role: str, capability: str, role_overrides: dict) -> bool:
    """Default capability grant per role, with optional authority.yaml override.

    Code defaults reproduce current L1:
      - executive: everything EXCEPT owner-only caps (write/exec/admin)
      - other:     read only
    An authority.yaml ``roles`` entry may tighten (``deny: [...]``) or
    restrict to an explicit allowlist (``allow: [...]``).
    """
    override = role_overrides.get(role) or {}
    deny = set(override.get("deny") or [])
    if capability in deny:
        return False
    allow = override.get("allow")
    if allow is not None:
        return capability in set(allow)
    if role == "executive":
        return capability not in OWNER_ONLY_CAPABILITIES
    if role == "other":
        return capability == "read"
    return False  # unknown role → fail-closed


def _grant_matches(entry: dict, *, actor_id: str, capability: str, resource: str,
                   owners: set[str], now: datetime) -> bool:
    if not isinstance(entry, dict):
        return False
    if str(entry.get("grantee", "")) != str(actor_id):
        return False
    allow = entry.get("allow") or {}
    cap = str(allow.get("capability", "")).strip().lower()
    if cap not in ("*", capability.strip().lower()):
        return False
    if str(entry.get("granted_by", "")) not in owners:
        return False  # only owner-granted overrides are honored
    expires = _parse_iso(entry.get("expires_at"))
    if expires is None or expires <= now:
        return False  # missing/expired → invalid (no perpetual)
    res = str(allow.get("resource", "")).strip()
    if res and res != "*" and res != str(resource):
        return False
    return True


def evaluate(actor, capability: str, resource: str = "", now: Optional[datetime] = None) -> Decision:
    """Decide whether ``actor`` may exercise ``capability`` on ``resource``.

    Deterministic order: owner/local/system bypass → explicit deny → valid
    grant → role default → fail-closed deny. ``rule_id`` names the deciding rule.
    """
    now = now or datetime.now(timezone.utc)
    role = role_of(actor)

    # 1. Break-glass / internal principals bypass policy.
    if role == "owner":
        return Decision(True, "owner_bypass", "actor is owner")
    if role == "local":
        return Decision(True, "local_bypass", "session-less owner-machine path")
    if role == "system":
        return Decision(True, "system_bypass", "internal automation (cron)")

    policy = _load_policy()
    owners = _owner_ids()

    # 2. Explicit deny wins over any allow.
    for entry in policy["denies"]:
        if _grant_matches(entry, actor_id=actor.id, capability=capability,
                           resource=resource, owners=owners, now=now):
            return Decision(False, f"deny:{entry.get('id', '?')}", "explicit deny")

    # 3. Per-person grant override.
    for entry in policy["grants"]:
        if _grant_matches(entry, actor_id=actor.id, capability=capability,
                          resource=resource, owners=owners, now=now):
            return Decision(True, f"grant:{entry.get('id', '?')}", "explicit grant")

    # 4. Role default (reproduces current tier behavior).
    if _role_allows(role, capability, policy["roles"]):
        return Decision(True, f"role:{role}", f"{role} default allows {capability}")

    # 5. Fail-closed.
    return Decision(False, f"role:{role}", f"{role} default denies {capability}")
