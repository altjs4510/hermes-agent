"""Slack media delivery for send_message.

Covers ``plugins/platforms/slack/adapter.py::_standalone_send`` media path:
text+file, media-only, caption-on-upload, missing-file warnings.

``slack_sdk`` is optional in CI, so tests inject a fake module into
``sys.modules`` (same pattern as ``tests/gateway/test_slack.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.platforms.slack.adapter import _standalone_send


def _pconfig(token: str = "xoxb-test"):
    return SimpleNamespace(token=token, extra={})


def _tmpfile(suffix: str) -> str:
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.write(b"%PDF-1.4 test")
    f.close()
    return f.name


def _mock_client(*, post_ok=True, upload_ok=True):
    client = MagicMock()
    client.chat_postMessage = AsyncMock(
        return_value={
            "ok": post_ok,
            "ts": "111.222",
            "error": None if post_ok else "channel_not_found",
        }
    )
    if upload_ok:
        client.files_upload_v2 = AsyncMock(
            return_value={
                "ok": True,
                "file": {
                    "id": "F123",
                    "timestamp": 1234567890,
                    "shares": {"public": {"C012AB3CD": [{"ts": "333.444"}]}},
                },
            }
        )
    else:
        client.files_upload_v2 = AsyncMock(
            return_value={"ok": False, "error": "not_in_channel"}
        )
    return client


@contextlib.contextmanager
def _fake_slack_sdk(client):
    """Make ``from slack_sdk.web.async_client import AsyncWebClient`` resolve to a factory."""
    sdk = ModuleType("slack_sdk")
    web = ModuleType("slack_sdk.web")
    async_client = ModuleType("slack_sdk.web.async_client")
    async_client.AsyncWebClient = MagicMock(return_value=client)
    sdk.web = web
    web.async_client = async_client

    modules = {
        "slack_sdk": sdk,
        "slack_sdk.web": web,
        "slack_sdk.web.async_client": async_client,
    }
    old = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name, prev in old.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


def test_text_plus_pdf_uploads_via_files_upload_v2():
    pdf = _tmpfile(".pdf")
    client = _mock_client()
    try:
        with _fake_slack_sdk(client):
            result = asyncio.run(
                _standalone_send(
                    _pconfig(),
                    "C012AB3CD",
                    "Here is the report",
                    media_files=[(pdf, False)],
                )
            )
        assert result["success"] is True
        assert result["platform"] == "slack"
        client.chat_postMessage.assert_awaited_once()
        client.files_upload_v2.assert_awaited_once()
        upload_kwargs = client.files_upload_v2.await_args.kwargs
        assert upload_kwargs["channel"] == "C012AB3CD"
        assert upload_kwargs["file"] == pdf
        assert upload_kwargs["filename"] == os.path.basename(pdf)
        assert upload_kwargs["initial_comment"] == ""
    finally:
        os.unlink(pdf)


def test_media_only_skips_text_post():
    pdf = _tmpfile(".pdf")
    client = _mock_client()
    try:
        with _fake_slack_sdk(client):
            result = asyncio.run(
                _standalone_send(
                    _pconfig(),
                    "C012AB3CD",
                    "",
                    media_files=[(pdf, False)],
                )
            )
        assert result["success"] is True
        client.chat_postMessage.assert_not_awaited()
        client.files_upload_v2.assert_awaited_once()
    finally:
        os.unlink(pdf)


def test_send_to_platform_routes_slack_media():
    """_send_to_platform must call Slack standalone_sender with media_files."""
    import httpx

    if not hasattr(httpx, "Proxy") or not hasattr(httpx, "URL"):
        pytest.skip("httpx type annotations incompatible with telegram library")

    from gateway.config import Platform
    from hermes_cli.plugins import discover_plugins
    from gateway.platform_registry import platform_registry
    from tools.send_message_tool import _send_to_platform

    pdf = _tmpfile(".pdf")
    discover_plugins()
    entry = platform_registry.get("slack")
    assert entry is not None and entry.standalone_sender_fn is not None
    original = entry.standalone_sender_fn
    mock_sender = AsyncMock(
        return_value={"success": True, "platform": "slack", "message_id": "1.2"}
    )
    entry.standalone_sender_fn = mock_sender
    try:
        result = asyncio.run(
            _send_to_platform(
                Platform.SLACK,
                _pconfig(),
                "C012AB3CD",
                "Here is the report",
                media_files=[(pdf, False)],
            )
        )
        assert result["success"] is True
        mock_sender.assert_awaited()
        call_kwargs = mock_sender.await_args.kwargs
        assert call_kwargs.get("media_files") == [(pdf, False)]
        # Single captionable file + short text → caption rides the upload.
        assert call_kwargs.get("caption") == "Here is the report"
        assert not result.get("warnings")
    finally:
        entry.standalone_sender_fn = original
        os.unlink(pdf)


def test_multiple_files_go_out_as_one_message_with_text():
    """Cookie: 2+ files → one files_upload_v2(file_uploads=...) carrying the text as
    initial_comment — not a text post followed by one message per file."""
    paths = [_tmpfile(ext) for ext in (".csv", ".csv", ".json")]
    client = _mock_client()
    client.files_upload_v2 = AsyncMock(return_value={
        "ok": True, "files": [{"id": "F1", "shares": {"private": {"C012AB3CD": [{"ts": "555.666"}]}}}]})
    try:
        with _fake_slack_sdk(client):
            result = asyncio.run(_standalone_send(
                _pconfig(), "C012AB3CD", "<@U123> 파일 공유", media_files=[(p, False) for p in paths]))
        assert result["success"] is True
        assert result["message_id"] == "555.666"
        client.chat_postMessage.assert_not_awaited()
        client.files_upload_v2.assert_awaited_once()
        kwargs = client.files_upload_v2.await_args.kwargs
        assert [u["file"] for u in kwargs["file_uploads"]] == paths
        assert [u["filename"] for u in kwargs["file_uploads"]] == [os.path.basename(p) for p in paths]
        assert kwargs["initial_comment"] == "<@U123> 파일 공유"
    finally:
        for p in paths:
            os.unlink(p)


def test_multiple_files_batches_of_ten_comment_only_on_first():
    paths = [_tmpfile(".csv") for _ in range(12)]
    client = _mock_client()
    try:
        with _fake_slack_sdk(client):
            result = asyncio.run(_standalone_send(
                _pconfig(), "C012AB3CD", "hello", media_files=[(p, False) for p in paths],
                thread_id="999.000"))
        assert result["success"] is True
        calls = client.files_upload_v2.await_args_list
        assert [len(c.kwargs["file_uploads"]) for c in calls] == [10, 2]
        assert [c.kwargs["initial_comment"] for c in calls] == ["hello", ""]
        assert all(c.kwargs["thread_ts"] == "999.000" for c in calls)
    finally:
        for p in paths:
            os.unlink(p)


def test_multiple_files_batch_failure_falls_back_per_file():
    paths = [_tmpfile(".csv") for _ in range(2)]
    client = _mock_client()
    single = {"ok": True, "file": {"id": "F1", "shares": {"public": {"C012AB3CD": [{"ts": "333.444"}]}}}}

    async def upload(**kwargs):
        if "file_uploads" in kwargs:
            raise RuntimeError("batch boom")
        return single
    client.files_upload_v2 = AsyncMock(side_effect=upload)
    try:
        with _fake_slack_sdk(client):
            result = asyncio.run(_standalone_send(
                _pconfig(), "C012AB3CD", "hello", media_files=[(p, False) for p in paths]))
        assert result["success"] is True
        client.chat_postMessage.assert_awaited_once()  # legacy path: text post + per-file
        assert client.files_upload_v2.await_count == 3
    finally:
        for p in paths:
            os.unlink(p)


def test_multiple_files_missing_one_is_warned_and_rest_batched():
    paths = [_tmpfile(".csv") for _ in range(2)]
    client = _mock_client()
    try:
        with _fake_slack_sdk(client):
            result = asyncio.run(_standalone_send(
                _pconfig(), "C012AB3CD", "", media_files=[(p, False) for p in paths] + [("/nope.csv", False)]))
        assert result["success"] is True
        client.files_upload_v2.assert_awaited_once()
        assert len(client.files_upload_v2.await_args.kwargs["file_uploads"]) == 2
        assert any("/nope.csv" in w for w in result["warnings"])
    finally:
        for p in paths:
            os.unlink(p)
