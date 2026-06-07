"""Per-person delegation registry (W4).

Owner-curated explicit delegations: "this person's requests of this type may
proceed autonomously, without the W3 on-behalf block." This promotes the 4th
authorization axis ("explicit delegation") from prompt-trust to a code-checked
data file. See docs/auth-audit-redesign.md (W4).

Registry file: ``~/.hermes/context/delegations.yaml`` (owner-edited by hand —
NO auto-learning, that's an explicit non-goal). Schema::

    delegations:
      - id: dlg_eric_zoee_api
        grantee: U0AM13JAWM8           # whose requests this covers (slack id)
        grantee_label: "박봉섭 이사님"   # optional, readability only
        action: send                    # action_class covered ("*" = any)
        scope:
          platforms: [slack]            # optional allowlist; empty/absent = any
          targets: ["*"]                # optional target allowlist; "*" = any
        expires_at: "2026-07-04T00:00:00Z"   # REQUIRED — no perpetual grants
        rationale: "..."                # why (audited)
        granted_by: U0AMDSG0Q49         # MUST be an owner id, else ignored
        created_at: "2026-06-04T00:00:00Z"

Invariants:
- Empty/missing file → no delegations → current behavior (W3 blocks all
  on-behalf sends).
- Only entries ``granted_by`` an owner AND not expired match (defense in depth:
  even though the file is owner-edited, the grant is re-validated at read time).
- Missing/invalid ``expires_at`` → entry does NOT match (no perpetual default).
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_lock = threading.Lock()
_cache: dict[str, Any] = {"mtime": None, "entries": []}


def _registry_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "context" / "delegations.yaml"
    except Exception:
        return Path(os.path.expanduser("~/.hermes/context/delegations.yaml"))


def _owner_ids() -> set[str]:
    return {u.strip() for u in os.getenv("HERMES_OWNER_IDS", "").split(",") if u.strip()}


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _load_entries() -> list[dict]:
    """Load + cache delegation entries, reloading on file mtime change."""
    path = _registry_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        with _lock:
            _cache["mtime"] = None
            _cache["entries"] = []
        return []
    with _lock:
        if _cache["mtime"] == mtime:
            return _cache["entries"]
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = data.get("delegations") or []
        if not isinstance(entries, list):
            entries = []
    except Exception:
        entries = []
    with _lock:
        _cache["mtime"] = mtime
        _cache["entries"] = entries
    return entries


def match_delegation(
    *,
    actor_uid: str,
    action_class: str,
    platform: str,
    target: str,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Return the matching valid delegation dict, or ``None``.

    Match requires ALL: grantee == actor_uid, action ∈ {action_class, "*"},
    granted_by ∈ owner ids, not expired (valid expires_at in the future),
    platform allowed by scope, target allowed by scope.
    """
    if not actor_uid:
        return None
    now = now or datetime.now(timezone.utc)
    owners = _owner_ids()
    for entry in _load_entries():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("grantee", "")) != str(actor_uid):
            continue
        action = str(entry.get("action", "")).strip().lower()
        if action not in ("*", str(action_class).strip().lower()):
            continue
        if str(entry.get("granted_by", "")) not in owners:
            continue  # only owner-granted delegations are honored
        expires = _parse_iso(entry.get("expires_at"))
        if expires is None or expires <= now:
            continue  # expired or missing/invalid expiry → not valid
        scope = entry.get("scope") or {}
        platforms = [str(p).strip().lower() for p in (scope.get("platforms") or [])]
        if platforms and str(platform).strip().lower() not in platforms:
            continue
        targets = [str(t) for t in (scope.get("targets") or [])]
        if targets and "*" not in targets and str(target) not in targets:
            continue
        return entry
    return None
