"""Single source of truth for "who is the current actor" (W7 / P0).

Authorization decisions are scattered across the codebase and each reads the
requesting user from a *different* carrier:
  - ``agent._user_id``                 (L1 tool filtering, set at agent init)
  - ``HERMES_SESSION_USER_ID`` ctxvar  (send-time gates, slash access)
  - ``source.user_id``                 (gateway message handler, the origin)

That fragmentation is fine for ad-hoc checks but unreliable as the basis for
per-person policy: "who is this?" must have ONE answer. ``get_current_actor()``
unifies the carriers into a small value object so callers stop re-deriving it
(and stop fail-opening when one carrier is empty).

This module is identity ONLY — it answers *who*, never *what they may do*
(that's gateway/authz.py). Owner/role resolution lives in the policy layer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ActorId:
    """Who is driving the current action.

    kind:
      - ``user``   — a real platform user (slack/telegram/... id in ``id``).
      - ``system`` — a non-human trigger (cron); ``id`` names the source.
      - ``local``  — a process on the owner's own machine with no session
                     (CLI ``hermes send``, MCP server). Treated as owner-trusted
                     by policy, but kept distinct so it is auditable.
    """

    kind: str
    id: str

    @property
    def is_user(self) -> bool:
        return self.kind == "user"

    @property
    def is_system(self) -> bool:
        return self.kind == "system"

    @property
    def is_local(self) -> bool:
        return self.kind == "local"

    def __str__(self) -> str:
        return f"{self.kind}:{self.id}"


def get_current_actor(user_id_hint: str | None = None) -> ActorId:
    """Resolve the current actor from the available carriers.

    Priority:
      1. ``user_id_hint`` (explicit — e.g. ``agent._user_id`` at init time)
      2. ``HERMES_SESSION_USER_ID`` session contextvar (gateway request scope)
      3. cron flag → ``system:cron`` (session is intentionally cleared in cron)
      4. otherwise → ``local:cli`` (session-less owner-machine path)

    Never returns ``None``: an unidentifiable caller resolves to ``local`` so
    downstream policy makes a deliberate decision instead of fail-opening on a
    missing id.
    """
    if user_id_hint:
        return ActorId("user", str(user_id_hint))
    try:
        from gateway.session_context import get_session_env
        uid = get_session_env("HERMES_SESSION_USER_ID", "")
    except Exception:
        uid = os.environ.get("HERMES_SESSION_USER_ID", "")
    if uid:
        return ActorId("user", uid)
    if os.environ.get("HERMES_CRON_SESSION") == "1":
        return ActorId("system", "cron")
    return ActorId("local", "cli")
