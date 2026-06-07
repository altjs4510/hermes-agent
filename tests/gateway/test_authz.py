"""Tests for gateway/authz.py (W7 P2 — central policy engine).

The critical property: with an EMPTY policy file, evaluate() reproduces the
current L1 tier behavior exactly (owner=all, executive=all-minus-owner-only,
other=read-only). Plus grant/deny override semantics and bypass principals.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import gateway.authz as authz
from agent.identity import ActorId
from agent.tool_capabilities import OWNER_ONLY_CAPABILITIES, EXECUTIVE_CAPABILITIES

OWNER = "U_OWNER"
EXEC = "U_EXEC"
OTHER = "U_OTHER"
FUTURE = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat().replace("+00:00", "Z")
PAST = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")

ALL_CAPS = sorted(OWNER_ONLY_CAPABILITIES | EXECUTIVE_CAPABILITIES | {"read"})


@pytest.fixture(autouse=True)
def _empty_policy_and_env(tmp_path):
    # Empty policy file + known owner/exec ids.
    (tmp_path / "authority.yaml").write_text("roles: {}\ngrants: []\ndenies: []\n", encoding="utf-8")
    authz._cache["mtime"] = None
    authz._cache["policy"] = None
    with patch("gateway.authz._policy_path", return_value=tmp_path / "authority.yaml"), \
         patch.dict("os.environ", {"HERMES_OWNER_IDS": OWNER, "HERMES_EXECUTIVE_IDS": EXEC}, clear=False):
        yield
    authz._cache["mtime"] = None
    authz._cache["policy"] = None


def _set_policy(tmp_path, text):
    (tmp_path / "authority.yaml").write_text(text, encoding="utf-8")
    authz._cache["mtime"] = None
    authz._cache["policy"] = None


# ── role defaults reproduce L1 ────────────────────────────────────────

@pytest.mark.parametrize("cap", ALL_CAPS)
def test_owner_allows_everything(cap):
    assert authz.evaluate(ActorId("user", OWNER), cap).allow is True


@pytest.mark.parametrize("cap", ALL_CAPS)
def test_executive_matches_l1(cap):
    d = authz.evaluate(ActorId("user", EXEC), cap)
    # Executive keeps everything except owner-only caps.
    assert d.allow is (cap not in OWNER_ONLY_CAPABILITIES)


@pytest.mark.parametrize("cap", ALL_CAPS)
def test_other_read_only(cap):
    d = authz.evaluate(ActorId("user", OTHER), cap)
    assert d.allow is (cap == "read")


def test_local_and_system_bypass():
    assert authz.evaluate(ActorId("local", "cli"), "exec").allow is True
    assert authz.evaluate(ActorId("system", "cron"), "write").allow is True
    assert authz.evaluate(ActorId("local", "cli"), "exec").rule_id == "local_bypass"


def test_rule_id_names_the_decider():
    assert authz.evaluate(ActorId("user", OWNER), "write").rule_id == "owner_bypass"
    assert authz.evaluate(ActorId("user", OTHER), "write").rule_id == "role:other"


# ── grant / deny overrides ────────────────────────────────────────────

def test_grant_allows_otherwise_denied_capability(tmp_path):
    _set_policy(tmp_path, f"""
roles: {{}}
grants:
  - id: g1
    grantee: {EXEC}
    allow: {{ capability: write, resource: "*" }}
    expires_at: "{FUTURE}"
    granted_by: {OWNER}
denies: []
""")
    d = authz.evaluate(ActorId("user", EXEC), "write")
    assert d.allow is True
    assert d.rule_id == "grant:g1"


def test_expired_grant_does_not_apply(tmp_path):
    _set_policy(tmp_path, f"""
grants:
  - id: g1
    grantee: {EXEC}
    allow: {{ capability: write, resource: "*" }}
    expires_at: "{PAST}"
    granted_by: {OWNER}
""")
    assert authz.evaluate(ActorId("user", EXEC), "write").allow is False


def test_non_owner_granted_by_ignored(tmp_path):
    _set_policy(tmp_path, f"""
grants:
  - id: g1
    grantee: {OTHER}
    allow: {{ capability: send, resource: "*" }}
    expires_at: "{FUTURE}"
    granted_by: {EXEC}
""")
    assert authz.evaluate(ActorId("user", OTHER), "send").allow is False


def test_resource_scope_enforced_on_grant(tmp_path):
    _set_policy(tmp_path, f"""
grants:
  - id: g1
    grantee: {EXEC}
    allow: {{ capability: write, resource: "repo:/a" }}
    expires_at: "{FUTURE}"
    granted_by: {OWNER}
""")
    assert authz.evaluate(ActorId("user", EXEC), "write", resource="repo:/a").allow is True
    assert authz.evaluate(ActorId("user", EXEC), "write", resource="repo:/b").allow is False


def test_deny_overrides_grant(tmp_path):
    _set_policy(tmp_path, f"""
grants:
  - id: g1
    grantee: {EXEC}
    allow: {{ capability: send, resource: "*" }}
    expires_at: "{FUTURE}"
    granted_by: {OWNER}
denies:
  - id: d1
    grantee: {EXEC}
    allow: {{ capability: send, resource: "*" }}
    expires_at: "{FUTURE}"
    granted_by: {OWNER}
""")
    d = authz.evaluate(ActorId("user", EXEC), "send")
    assert d.allow is False
    assert d.rule_id == "deny:d1"


def test_role_override_tightens(tmp_path):
    _set_policy(tmp_path, f"""
roles:
  executive: {{ deny: [generate] }}
""")
    # generate is normally executive-allowed; override removes it.
    assert authz.evaluate(ActorId("user", EXEC), "generate").allow is False
    assert authz.evaluate(ActorId("user", EXEC), "send").allow is True


def test_missing_policy_file_uses_defaults(tmp_path):
    with patch("gateway.authz._policy_path", return_value=tmp_path / "nope.yaml"):
        authz._cache["mtime"] = None
        authz._cache["policy"] = None
        assert authz.evaluate(ActorId("user", OTHER), "read").allow is True
        assert authz.evaluate(ActorId("user", OTHER), "send").allow is False
