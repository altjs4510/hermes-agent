"""Feedback-priority lookup from the saved F&F people roster.

Used by the self-improvement feedback loop (see
docs/plans/2026-05-27-self-improvement-feedback-loop.md): when a non-owner
gives feedback about the bot, the owner-confirm card shows WHO gave it and at
what priority, so the owner can act on an executive's note first and defer
others.

Source of truth is the curated roster people.json (keyed by Slack user id, not
live display name — so it can't be spoofed by renaming). Read-only.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Roster locations, in preference order. Same file in both; the jarvis
# knowledge dir is canonical, the alter dir is a mirror.
_PEOPLE_PATHS = [
    Path.home() / ".claude" / "knowledge" / "world" / "people.json",
    Path.home() / ".my-alter" / "people.json",
]

# Returned when the requester isn't in the roster.
_DEFAULT = {"tier": "low", "label": "외부 사용자", "urgent": False}


def _load_people() -> List[Dict[str, Any]]:
    for path in _PEOPLE_PATHS:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("people_priority: failed to read %s: %s", path, e)
    return []


def feedback_priority(slack_id: str) -> Dict[str, Any]:
    """Map a Slack user id to a feedback priority tier from the roster.

    Returns ``{"tier", "label", "urgent"}``:
    - ``role == "executive"``      -> tier=high,   urgent=True   (e.g. 이사 박봉섭)
    - ``team`` starts with "PRCS"   -> tier=normal, urgent=False  (process div: PRCS AIE/AX/…)
    - otherwise in roster           -> tier=low
    - not in roster / no id          -> tier=low, label="외부 사용자"

    (team is matched by "PRCS" prefix because the roster uses "PRCS AIE",
    "PRCS AX", etc. — only the director's entry is the bare "PRCS".)
    """
    if not slack_id:
        return dict(_DEFAULT)
    for person in _load_people():
        if person.get("slackId") != slack_id:
            continue
        role = (person.get("role") or "").strip().lower()
        team = (person.get("team") or "").strip()
        name = person.get("name") or person.get("displayName") or slack_id
        title = (person.get("title") or "").strip()
        if role == "executive":
            return {"tier": "high", "label": f"{title} {name}".strip(), "urgent": True}
        if team.upper().startswith("PRCS"):
            return {"tier": "normal", "label": f"{team} {name}".strip(), "urgent": False}
        return {"tier": "low", "label": name, "urgent": False}
    return dict(_DEFAULT)
