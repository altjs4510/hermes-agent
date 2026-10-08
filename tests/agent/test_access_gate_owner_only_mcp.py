"""Owner-private MCP servers (``mcp_servers.<name>.owner_only: true``) are hidden from
non-owners entirely — reads included — because e.g. ms365 is the owner's own mailbox.
authz alone classifies ``mcp__ms365_mcp__outlook_email_search`` as plain ``read``."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import gateway.authz as authz
from agent.agent_init import _load_tools

OWNER, EXEC, OTHER = "U_OWNER", "U_EXEC", "U_OTHER"

_TOOLS = [
    "read_file",
    "mcp__ms365_mcp__outlook_email_search",
    "mcp__ms365_mcp__outlook_send_mail",
    "mcp__dcsai__get_intents",
]
_CFG = {"mcp_servers": {"ms365-mcp": {"url": "x", "owner_only": True}, "dcsai": {"url": "y"}}}


@pytest.fixture(autouse=True)
def _env(tmp_path):
    (tmp_path / "authority.yaml").write_text("roles: {}\ngrants: []\ndenies: []\n", encoding="utf-8")
    authz._cache["mtime"] = None
    authz._cache["policy"] = None
    with patch("gateway.authz._policy_path", return_value=tmp_path / "authority.yaml"), \
         patch.dict("os.environ", {"HERMES_OWNER_IDS": OWNER, "HERMES_EXECUTIVE_IDS": EXEC}, clear=False), \
         patch("model_tools.get_tool_definitions",
               side_effect=lambda **_: [{"function": {"name": n}} for n in _TOOLS]):
        yield
    authz._cache["mtime"] = None
    authz._cache["policy"] = None


def _names(uid, cfg=_CFG):
    agent = SimpleNamespace(_user_id=uid, quiet_mode=True, ephemeral_system_prompt="")
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        _load_tools(agent, None, None)
    return {t["function"]["name"] for t in agent.tools}


def test_owner_keeps_owner_only_mcp():
    assert set(_TOOLS) <= _names(OWNER)


@pytest.mark.parametrize("uid", [OTHER, EXEC])
def test_non_owner_loses_owner_only_mcp_reads_too(uid):
    names = _names(uid)
    assert "mcp__ms365_mcp__outlook_email_search" not in names
    assert "mcp__ms365_mcp__outlook_send_mail" not in names
    assert "mcp__dcsai__get_intents" in names
    assert "read_file" in names


def test_unreadable_config_fails_closed():
    agent = SimpleNamespace(_user_id=OTHER, quiet_mode=True, ephemeral_system_prompt="")
    with patch("hermes_cli.config.load_config_readonly", side_effect=RuntimeError("boom")):
        _load_tools(agent, None, None)
    names = {t["function"]["name"] for t in agent.tools}
    assert not any(n.startswith("mcp_") for n in names)
    assert "read_file" in names
