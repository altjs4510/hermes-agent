"""Tests for agent/identity.py (W7 P0 — actor identity SoT)."""

from unittest.mock import patch

from agent.identity import ActorId, get_current_actor


def test_explicit_hint_wins():
    a = get_current_actor(user_id_hint="U123")
    assert a == ActorId("user", "U123")
    assert a.is_user


def test_session_user_id_resolves_to_user():
    with patch("gateway.session_context.get_session_env", return_value="U_SESS"):
        a = get_current_actor()
    assert a == ActorId("user", "U_SESS")


def test_cron_flag_resolves_to_system():
    with patch("gateway.session_context.get_session_env", return_value=""), \
         patch.dict("os.environ", {"HERMES_CRON_SESSION": "1"}, clear=False):
        a = get_current_actor()
    assert a == ActorId("system", "cron")
    assert a.is_system


def test_sessionless_resolves_to_local():
    with patch("gateway.session_context.get_session_env", return_value=""), \
         patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("HERMES_CRON_SESSION", None)
        a = get_current_actor()
    assert a == ActorId("local", "cli")
    assert a.is_local


def test_hint_beats_cron():
    with patch.dict("os.environ", {"HERMES_CRON_SESSION": "1"}, clear=False):
        a = get_current_actor(user_id_hint="U_REAL")
    assert a == ActorId("user", "U_REAL")


def test_str_form():
    assert str(ActorId("user", "U1")) == "user:U1"
