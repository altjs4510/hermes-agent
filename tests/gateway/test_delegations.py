"""Tests for the W4 delegation registry (gateway/delegations.py).

Covers matching, owner-grant validation, expiry, scope, and cache reload.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import gateway.delegations as dele


FUTURE = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat().replace("+00:00", "Z")
PAST = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
OWNER = "U_OWNER"
ERIC = "U_EXEC"


def _write_registry(tmp_path, yaml_text):
    p = tmp_path / "delegations.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    return p


def _entry(**over):
    base = {
        "id": "dlg_1",
        "grantee": ERIC,
        "action": "send",
        "scope": {"platforms": ["slack"], "targets": ["*"]},
        "expires_at": FUTURE,
        "granted_by": OWNER,
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _reset_cache():
    dele._cache["mtime"] = None
    dele._cache["entries"] = []
    yield
    dele._cache["mtime"] = None
    dele._cache["entries"] = []


def _match(entries, tmp_path, **kw):
    import yaml
    _write_registry(tmp_path, yaml.safe_dump({"delegations": entries}))
    with patch("gateway.delegations._registry_path", return_value=tmp_path / "delegations.yaml"), \
         patch.dict("os.environ", {"HERMES_OWNER_IDS": OWNER}, clear=False):
        defaults = dict(actor_uid=ERIC, action_class="send", platform="slack", target="C999")
        defaults.update(kw)
        return dele.match_delegation(**defaults)


def test_valid_delegation_matches(tmp_path):
    assert _match([_entry()], tmp_path) is not None


def test_empty_registry_no_match(tmp_path):
    assert _match([], tmp_path) is None


def test_wrong_actor_no_match(tmp_path):
    assert _match([_entry(grantee="U_SOMEONE")], tmp_path) is None


def test_expired_no_match(tmp_path):
    assert _match([_entry(expires_at=PAST)], tmp_path) is None


def test_missing_expiry_no_match(tmp_path):
    e = _entry()
    del e["expires_at"]
    assert _match([e], tmp_path) is None


def test_non_owner_grant_ignored(tmp_path):
    # Even if present in the file, a delegation not granted_by an owner is ignored.
    assert _match([_entry(granted_by="U_EXEC")], tmp_path) is None


def test_action_wildcard_matches(tmp_path):
    assert _match([_entry(action="*")], tmp_path, action_class="send") is not None


def test_action_mismatch_no_match(tmp_path):
    assert _match([_entry(action="write")], tmp_path, action_class="send") is None


def test_platform_scope_enforced(tmp_path):
    assert _match([_entry(scope={"platforms": ["telegram"]})], tmp_path, platform="slack") is None
    assert _match([_entry(scope={"platforms": ["slack"]})], tmp_path, platform="slack") is not None


def test_target_allowlist_enforced(tmp_path):
    assert _match([_entry(scope={"targets": ["C111"]})], tmp_path, target="C999") is None
    assert _match([_entry(scope={"targets": ["C999"]})], tmp_path, target="C999") is not None


def test_empty_scope_allows_any(tmp_path):
    assert _match([_entry(scope={})], tmp_path, platform="telegram", target="anything") is not None


def test_no_actor_no_match(tmp_path):
    assert _match([_entry()], tmp_path, actor_uid="") is None


def test_missing_file_returns_empty(tmp_path):
    with patch("gateway.delegations._registry_path", return_value=tmp_path / "nope.yaml"), \
         patch.dict("os.environ", {"HERMES_OWNER_IDS": OWNER}, clear=False):
        assert dele.match_delegation(actor_uid=ERIC, action_class="send",
                                     platform="slack", target="C1") is None
