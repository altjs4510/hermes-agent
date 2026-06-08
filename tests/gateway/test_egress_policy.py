"""Tests for gateway/egress_policy.py (egress lockdown Phase 1).

The latent decision table under content taint. Phase 1 never enforces these —
``evaluate_egress`` only computes what WOULD happen; the preflight records it.
"""

from unittest.mock import patch

import pytest

from agent.identity import ActorId
from gateway import egress_policy as ep
from gateway.egress_policy import evaluate_egress
from gateway.session_taint import (
    SessionTaint,
    TaintEvent,
    TRUST_EXTERNAL,
    TRUST_INTERNAL,
)

ACTOR = ActorId("user", "U_OWNER")


@pytest.fixture(autouse=True)
def _no_policy_file(tmp_path):
    # No egress.yaml → built-in shadow defaults, deterministic.
    ep._cache["mtime"] = None
    ep._cache["policy"] = None
    with patch("gateway.egress_policy._policy_path", return_value=tmp_path / "egress.yaml"):
        yield
    ep._cache["mtime"] = None
    ep._cache["policy"] = None


def _external() -> SessionTaint:
    t = SessionTaint()
    t.add(TaintEvent("web", "u", TRUST_EXTERNAL, "web_extract", "t"))
    return t


def _internal() -> SessionTaint:
    t = SessionTaint()
    t.add(TaintEvent("mcp", "m", TRUST_INTERNAL, "mcp__x__y_get", "t"))
    return t


# ── no taint / non-egress: always allow (preserve current behavior) ──────

def test_no_taint_allows_send():
    d = evaluate_egress(ACTOR, "send", "slack:#public", SessionTaint(), {})
    assert d.action == "allow"
    assert d.rule_id == "egress:no-taint"


def test_non_egress_capability_allows():
    d = evaluate_egress(ACTOR, "read", "x", _external(), {})
    assert d.action == "allow"
    assert d.rule_id == "egress:not-egress"


def test_generate_is_out_of_phase1_scope():
    d = evaluate_egress(ACTOR, "generate", "x", _external(), {})
    assert d.action == "allow"


# ── tainted send ─────────────────────────────────────────────────────────

def test_tainted_send_to_external_target_confirms():
    d = evaluate_egress(ACTOR, "send", "slack:#public", _external(), {})
    assert d.action == "confirm"
    assert d.rule_id == "egress:tainted-external-send"
    assert d.risk_level == "high"


def test_tainted_same_thread_reply_allowed():
    # Empty/current_thread resource = a normal same-surface reply, not exfil.
    assert evaluate_egress(ACTOR, "send", "", _external(), {}).action == "allow"
    assert evaluate_egress(ACTOR, "send", "current_thread", _external(), {}).action == "allow"


def test_internal_taint_send_is_medium_confirm():
    d = evaluate_egress(ACTOR, "send", "slack:#public", _internal(), {})
    assert d.action == "confirm"
    assert d.rule_id == "egress:tainted-internal-send"
    assert d.risk_level == "medium"


# ── other egress capabilities under taint ────────────────────────────────

def test_tainted_mcp_write_confirms():
    d = evaluate_egress(ACTOR, "mcp_write", "mcp__x__create", _external(), {})
    assert d.action == "confirm"
    assert d.rule_id == "egress:tainted-mcp-write"


def test_tainted_browser_action_confirms():
    d = evaluate_egress(ACTOR, "browse", "https://x", _external(), {})
    assert d.action == "confirm"


def test_tainted_exec_confirms():
    d = evaluate_egress(ACTOR, "exec", "curl x", _external(), {})
    assert d.action == "confirm"
    assert d.rule_id == "egress:tainted-exec"


def test_tainted_local_write_allowed_not_blocked():
    # Local drafts/specs are normal; Phase 1 does not blanket-block writes.
    d = evaluate_egress(ACTOR, "write", "/tmp/draft.md", _external(), {})
    assert d.action == "allow"
    assert d.risk_level == "low"


# ── allowlist via egress.yaml ─────────────────────────────────────────────

def test_yaml_allowlist_promotes_send_to_allow(tmp_path):
    (tmp_path / "egress.yaml").write_text(
        "mode: shadow\nallow:\n  - id: owner-dm\n    capability: send\n    resource: slack:dm:owner\n",
        encoding="utf-8",
    )
    ep._cache["mtime"] = None
    ep._cache["policy"] = None
    d = evaluate_egress(ACTOR, "send", "slack:dm:owner", _external(), {})
    assert d.action == "allow"
    assert d.rule_id == "egress:owner-dm"


def test_default_mode_is_shadow():
    assert ep.get_mode() == "shadow"
