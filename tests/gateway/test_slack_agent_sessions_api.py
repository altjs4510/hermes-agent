"""Slack adapter routing onto the slack-sdk Agent Sessions API (agents.sessions.*)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from tests.gateway.test_slack import SlackAdapter, _slack_mod


@pytest.fixture(autouse=True)
def _pin_legacy_assistant_threads_api():
    """Pin the SDK capability probe to the legacy assistant.threads API and
    restore it afterwards, so tests that flip the cached flag on don't leak."""
    prev = _slack_mod._AGENT_SESSIONS_SUPPORTED
    _slack_mod._AGENT_SESSIONS_SUPPORTED = False
    yield
    _slack_mod._AGENT_SESSIONS_SUPPORTED = prev


class TestAgentSessionsApiRouting:
    """slack-sdk 3.44.0 Agent Sessions API (assistant_view deprecation Feb 2027).

    When the installed slack-sdk ships agents.sessions.* typed methods, status
    and title calls route through them; older SDKs keep using the legacy
    assistant.threads.* methods (compat bridge on Slack's side).
    """

    def _adapter(self):
        config = PlatformConfig(enabled=True, token="xoxb-fake-token")
        a = SlackAdapter(config)
        a._app = MagicMock()
        a._app.client = AsyncMock()
        return a

    @pytest.mark.asyncio
    async def test_typing_stays_on_legacy_status_even_when_agent_sessions_supported(self):
        # Cookie override (4c8c6c0): agents.sessions.setStatus takes an ENUM status and rejects our
        # free-text phrases ("is thinking..."), which silently killed the typing indicator — so the
        # status setter stays pinned to legacy assistant.threads.setStatus even on a new SDK.
        # Revisit before the Assistant API sunsets (2027-02). Titles still use agents.sessions.rename
        # (see the title test below), because rename takes free text.
        _slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a._app.client.agents_sessions_setStatus = AsyncMock()
        a._app.client.assistant_threads_setStatus = AsyncMock()
        await a.send_typing("C123", metadata={"thread_id": "parent_ts"})
        a._app.client.assistant_threads_setStatus.assert_called_once_with(
            channel_id="C123",
            thread_ts="parent_ts",
            status="is thinking...",
        )
        a._app.client.agents_sessions_setStatus.assert_not_called()


    @pytest.mark.asyncio
    async def test_stop_typing_clears_via_legacy_status(self):
        # Same Cookie override as above: clearing also goes through the legacy method.
        _slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a._app.client.agents_sessions_setStatus = AsyncMock()
        a._app.client.assistant_threads_setStatus = AsyncMock()
        await a.send_typing("C123", metadata={"thread_id": "parent_ts"})
        a._app.client.assistant_threads_setStatus.reset_mock()
        await a.stop_typing("C123", metadata={"thread_id": "parent_ts"})
        a._app.client.assistant_threads_setStatus.assert_called_once_with(
            channel_id="C123",
            thread_ts="parent_ts",
            status="",
        )
        a._app.client.agents_sessions_setStatus.assert_not_called()

    @pytest.mark.asyncio
    async def test_thread_title_uses_agents_sessions_rename(self):
        _slack_mod._AGENT_SESSIONS_SUPPORTED = True
        a = self._adapter()
        a.config.extra["assistant_thread_titles"] = True
        a._app.client.agents_sessions_rename = AsyncMock()
        a._app.client.assistant_threads_setTitle = AsyncMock()
        await a._set_assistant_thread_title("D123", "171234.0001", "Summarize the incident")
        a._app.client.agents_sessions_rename.assert_called_once_with(
            channel_id="D123",
            thread_ts="171234.0001",
            title="Summarize the incident",
        )
        a._app.client.assistant_threads_setTitle.assert_not_called()
