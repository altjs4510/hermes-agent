"""Slack List todo tool — native wrapper over the Slack Lists API for 쿠키's
single-shot todo SoT ("쿠키 todo" list).

Background: as of the 2026-05-29 cutover, 쿠키's single-shot todos live in a
Slack List (the source of truth), NOT the internal `todo` scratchpad and NOT
Notion. Until now the only way to mutate that list was the slack-lists-todo
*skill* driving raw `curl` in a terminal — which needs the `terminal` tool
(capability `exec`, owner-only). That made the SoT unreachable for executives
even though "Todo 등록" is an allowed action (박봉섭 이사 지침 / SOUL §0).

This tool absorbs the verified curl recipes (skills/slack-lists-todo/references
/api-recipes.md — the rich_text/select/date payload traps) into a deterministic
in-process tool. Its capability is `task_ops` (see agent/tool_capabilities.py),
so executives can register todos without raw shell access.

Env (all in ~/.hermes/.env, set at the cutover):
  HERMES_SLACK_BOT_TOKEN              bot token with lists:read + lists:write
  COOKIE_TODO_LIST_ID                 the "쿠키 todo" list id (F0B6K89078V)
  COOKIE_TODO_COL_{TITLE,STATUS,PROJECT,DUE,PRIORITY,KIND,COMPLETED}
                                      stable column PKs (survive UI renames)
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.parse

from tools.registry import registry, tool_error

_API = "https://slack.com/api"

# Valid select values (label is Korean in the UI; the API takes the value).
_STATUS_VALUES = {"not_started", "in_progress", "done", "stopped"}
_PRIORITY_VALUES = {"high", "med", "low"}
_KIND_VALUES = {"project", "single"}


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _token() -> str:
    # Skill docs reference HERMES_SLACK_BOT_TOKEN, but the live gateway env only
    # defines SLACK_BOT_TOKEN (the @cookiealterdev bot, which holds lists:read/
    # write and can reach the list). Prefer the namespaced one if ever set.
    return _env("HERMES_SLACK_BOT_TOKEN") or _env("SLACK_BOT_TOKEN")


def _rich_text(text: str) -> list:
    """Wrap a plain string as the Block Kit rich_text array the title column
    requires (api-recipes.md 함정-1 — plain string / "rich_text":string both fail)."""
    return [{
        "type": "rich_text",
        "elements": [{
            "type": "rich_text_section",
            "elements": [{"type": "text", "text": text}],
        }],
    }]


def _post(method: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{_API}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(method: str, params: dict) -> dict:
    url = f"{_API}/{method}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_token()}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _add(args: dict) -> str:
    title = (args.get("title") or "").strip()
    if not title:
        return tool_error("title is required for action=add")
    status = (args.get("status") or "not_started").strip()
    if status not in _STATUS_VALUES:
        return tool_error(f"invalid status '{status}' (one of {sorted(_STATUS_VALUES)})")

    fields = [
        {"column_id": _env("COOKIE_TODO_COL_TITLE"), "rich_text": _rich_text(title)},
        {"column_id": _env("COOKIE_TODO_COL_STATUS"), "select": [status]},
    ]
    project = (args.get("project") or "").strip()
    if project:
        fields.append({"column_id": _env("COOKIE_TODO_COL_PROJECT"), "select": [project]})
    due = (args.get("due") or "").strip()
    if due:
        fields.append({"column_id": _env("COOKIE_TODO_COL_DUE"), "date": [due]})
    priority = (args.get("priority") or "").strip()
    if priority:
        if priority not in _PRIORITY_VALUES:
            return tool_error(f"invalid priority '{priority}' (one of {sorted(_PRIORITY_VALUES)})")
        fields.append({"column_id": _env("COOKIE_TODO_COL_PRIORITY"), "select": [priority]})
    kind = (args.get("kind") or "").strip()
    if kind:
        if kind not in _KIND_VALUES:
            return tool_error(f"invalid kind '{kind}' (one of {sorted(_KIND_VALUES)})")
        fields.append({"column_id": _env("COOKIE_TODO_COL_KIND"), "select": [kind]})

    r = _post("slackLists.items.create", {
        "list_id": _env("COOKIE_TODO_LIST_ID"),
        "initial_fields": fields,
    })
    if not r.get("ok"):
        return tool_error(f"slackLists.items.create failed: {r.get('error')}")
    item = r.get("item") or {}
    return json.dumps({
        "ok": True, "action": "add", "title": title,
        "id": item.get("id"), "status": status,
        "project": project or None, "due": due or None,
    }, ensure_ascii=False)


def _list(args: dict) -> str:
    r = _get("slackLists.items.list", {"list_id": _env("COOKIE_TODO_LIST_ID")})
    if not r.get("ok"):
        return tool_error(f"slackLists.items.list failed: {r.get('error')}")
    col_status = _env("COOKIE_TODO_COL_STATUS")
    col_completed = _env("COOKIE_TODO_COL_COMPLETED")
    include_done = bool(args.get("include_done"))
    out = []
    for it in r.get("items", []):
        fields = {f.get("column_id"): f for f in it.get("fields", [])}
        sel = (fields.get(col_status, {}).get("select") or ["not_started"])
        status = sel[0] if sel else "not_started"
        done = bool(fields.get(col_completed, {}).get("checkbox"))
        if not include_done and (done or status == "done"):
            continue
        title_f = fields.get(_env("COOKIE_TODO_COL_TITLE"), {})
        out.append({
            "id": it.get("id"),
            "title": title_f.get("text") or "",
            "status": status,
        })
    return json.dumps({"ok": True, "action": "list", "open": len(out), "items": out},
                      ensure_ascii=False)


def _complete(args: dict) -> str:
    item_id = (args.get("item_id") or "").strip()
    if not item_id:
        return tool_error("item_id is required for action=complete (get it from action=list)")
    cells = [
        {"row_id": item_id, "column_id": _env("COOKIE_TODO_COL_STATUS"), "select": ["done"]},
        {"row_id": item_id, "column_id": _env("COOKIE_TODO_COL_COMPLETED"), "checkbox": True},
    ]
    r = _post("slackLists.items.update", {
        "list_id": _env("COOKIE_TODO_LIST_ID"),
        "cells": cells,
    })
    if not r.get("ok"):
        return tool_error(f"slackLists.items.update failed: {r.get('error')}")
    return json.dumps({"ok": True, "action": "complete", "id": item_id}, ensure_ascii=False)


def slack_list_todo_tool(args: dict) -> str:
    """Dispatch by action. Returns a JSON string (tool_error on failure)."""
    if not _token():
        return tool_error("HERMES_SLACK_BOT_TOKEN not set")
    if not _env("COOKIE_TODO_LIST_ID"):
        return tool_error("COOKIE_TODO_LIST_ID not set")
    action = (args.get("action") or "add").strip()
    if action == "add":
        return _add(args)
    if action == "list":
        return _list(args)
    if action == "complete":
        return _complete(args)
    return tool_error(f"unknown action '{action}' (add | list | complete)")


def check_slack_list_todo_requirements() -> bool:
    """Available only when the bot token + list id are configured."""
    return bool(_token() and _env("COOKIE_TODO_LIST_ID"))


SLACK_LIST_TODO_SCHEMA = {
    "name": "slack_list_todo",
    "description": (
        "Manage 쿠키's single-shot todo list (the Slack List that is the source "
        "of truth since the 2026-05-29 cutover). Use this for '쿠키 todo 등록/조회/완료' "
        "— NOT the internal `todo` tool (that is a per-session scratchpad, a "
        "different store). action=add registers a new todo; action=list shows open "
        "todos with ids; action=complete marks one done (needs item_id from list)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "list", "complete"],
                "description": "add (default), list, or complete",
                "default": "add",
            },
            "title": {"type": "string", "description": "Todo title (required for add)"},
            "status": {
                "type": "string",
                "enum": ["not_started", "in_progress", "done", "stopped"],
                "description": "Status for add (default not_started)",
            },
            "project": {
                "type": "string",
                "description": "Optional project tag value (e.g. dcs_ai, biz_support, alter_tools, robot, intern, etc)",
            },
            "due": {"type": "string", "description": "Optional due date YYYY-MM-DD"},
            "priority": {
                "type": "string",
                "enum": ["high", "med", "low"],
                "description": "Optional priority",
            },
            "kind": {
                "type": "string",
                "enum": ["project", "single"],
                "description": "Optional kind",
            },
            "item_id": {"type": "string", "description": "Row id (required for action=complete)"},
            "include_done": {
                "type": "boolean",
                "description": "action=list only: include completed items (default false)",
                "default": False,
            },
        },
        "required": [],
    },
}


registry.register(
    name="slack_list_todo",
    toolset="todo",
    schema=SLACK_LIST_TODO_SCHEMA,
    handler=lambda args, **kw: slack_list_todo_tool(args),
    check_fn=check_slack_list_todo_requirements,
    emoji="🗒️",
)
