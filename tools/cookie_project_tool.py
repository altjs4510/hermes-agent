"""Cookie-native project registry and project-agent tools for Hermes.

This is native absorption of cookie-alter project routing, not a bridge to
cookie-alter.  It reads project metadata from Cookie's existing JSON stores and
optionally queries a project's local ``agent_endpoint``.

Data boundary: this tool only reads project metadata files and talks to local
project agent endpoints. It does not read Notion/Outlook/Teams/Slack bodies.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tools.registry import registry, tool_error, tool_result


MY_ALTER_PROJECTS = Path(os.path.expanduser("~/.my-alter/projects"))
WORLD_PROJECTS = Path(os.path.expanduser("~/.claude/knowledge/world/projects.json"))


def _expand(path: str | None) -> str | None:
    if not path:
        return None
    return os.path.abspath(os.path.expanduser(path))


def _valid_cwd(path: str | None) -> str | None:
    expanded = _expand(path)
    if expanded and os.path.exists(expanded):
        return expanded
    return None


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _iter_my_alter_projects() -> Iterable[Tuple[Dict[str, Any], str]]:
    if not MY_ALTER_PROJECTS.exists():
        return
    for path in sorted(MY_ALTER_PROJECTS.glob("*.json")):
        try:
            data = _read_json(path)
            if isinstance(data, dict):
                yield data, str(path)
        except Exception:
            continue


def _iter_world_projects() -> Iterable[Tuple[Dict[str, Any], str]]:
    if not WORLD_PROJECTS.exists():
        return
    try:
        data = _read_json(WORLD_PROJECTS)
    except Exception:
        return
    projects = data.get("projects", []) if isinstance(data, dict) else []
    if isinstance(projects, dict):
        projects = list(projects.values())
    for item in projects:
        if isinstance(item, dict):
            yield item, str(WORLD_PROJECTS)


def _project_key(project: Dict[str, Any]) -> str:
    return str(project.get("id") or project.get("name") or project.get("project") or "unknown")


def _channels(project: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    for key in ("agent_slack_channel", "slack_channel", "channel"):
        val = project.get(key)
        if isinstance(val, str) and val:
            values.append(val)
    val = project.get("slack_channels")
    if isinstance(val, list):
        values.extend(str(x) for x in val if x)
    return sorted(set(values))


def _safe_project(project: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Return metadata safe to expose to the model."""
    cwd = _valid_cwd(project.get("local_path") or project.get("cwd"))
    endpoint = project.get("agent_endpoint")
    return {
        "id": project.get("id"),
        "name": project.get("name") or project.get("id"),
        "cwd": cwd,
        "local_path_exists": bool(cwd),
        "channels": _channels(project),
        "agent_endpoint": endpoint if isinstance(endpoint, str) else None,
        "source": source,
    }


def _load_projects() -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    def add(project: Dict[str, Any], source: str) -> None:
        safe = _safe_project(project, source)
        key = str(safe.get("id") or safe.get("name") or _project_key(project))
        if key not in merged:
            merged[key] = safe
            order.append(key)
            return
        # my-alter metadata wins for channels/agent_endpoint; world often fills cwd.
        prev = merged[key]
        for field in ("cwd", "agent_endpoint"):
            if not prev.get(field) and safe.get(field):
                prev[field] = safe[field]
        prev["channels"] = sorted(set(prev.get("channels", [])) | set(safe.get("channels", [])))
        prev["local_path_exists"] = bool(prev.get("cwd"))
        prev["source"] = f"{prev.get('source')}+{source}"

    for project, source in _iter_my_alter_projects() or []:
        add(project, source)
    for project, source in _iter_world_projects() or []:
        add(project, source)
    return [merged[k] for k in order]


def _match_name(project: Dict[str, Any], name: str) -> bool:
    return name in {str(project.get("id") or ""), str(project.get("name") or "")}


def _resolve(project_name: str | None = None, channel_id: str | None = None) -> Dict[str, Any] | None:
    projects = _load_projects()
    if project_name:
        for project in projects:
            if _match_name(project, project_name):
                return {**project, "resolved_by": "project_name"}
    if channel_id:
        for project in projects:
            if channel_id in project.get("channels", []):
                return {**project, "resolved_by": "channel_id"}
    return None


def _query_endpoint(endpoint: str, question: str, timeout: int = 120) -> str:
    sep = "&" if "?" in endpoint else "?"
    url = endpoint + sep + "q=" + urllib.parse.quote(question)
    req = urllib.request.Request(url, headers={"Accept": "application/json,text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(1_000_000)
        text = data.decode("utf-8", errors="replace")
        return text


COOKIE_PROJECT_SCHEMA = {
    "name": "cookie_project",
    "description": (
        "Cookie-native project registry and project-agent routing. Use this to list/resolve "
        "Cookie projects, map Slack channels to project cwd, and query a registered local "
        "project agent endpoint such as gtm-agent. This is native Hermes absorption, not a "
        "cookie-alter bridge. Reads metadata only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "resolve", "query_agent"],
                "description": "list projects, resolve by project/channel, or query the resolved project's agent_endpoint.",
            },
            "project": {
                "type": "string",
                "description": "Project id/name, e.g. gtm-agent or dcs-ai.",
            },
            "channel_id": {
                "type": "string",
                "description": "Slack channel ID used to resolve project context.",
            },
            "question": {
                "type": "string",
                "description": "Question to send to the resolved project's local agent_endpoint for query_agent.",
            },
            "timeout": {
                "type": "integer",
                "description": "HTTP timeout seconds for query_agent. Default 120.",
            },
        },
        "required": ["action"],
    },
}


def _handle_cookie_project(args: Dict[str, Any], **_: Any) -> str:
    action = args.get("action")
    if action == "list":
        return tool_result({"projects": _load_projects()})

    if action == "resolve":
        resolved = _resolve(args.get("project"), args.get("channel_id"))
        if not resolved:
            return tool_error("No matching Cookie project found")
        return tool_result({"project": resolved})

    if action == "query_agent":
        question = args.get("question")
        if not isinstance(question, str) or not question.strip():
            return tool_error("question is required for query_agent")
        resolved = _resolve(args.get("project"), args.get("channel_id"))
        if not resolved:
            return tool_error("No matching Cookie project found")
        endpoint = resolved.get("agent_endpoint")
        if not endpoint:
            return tool_error("Resolved project has no agent_endpoint", project=resolved)
        try:
            timeout = int(args.get("timeout") or 120)
            answer = _query_endpoint(str(endpoint), question, timeout=max(1, min(timeout, 300)))
        except Exception as exc:
            return tool_error(f"project agent query failed: {exc}", project=resolved)
        return tool_result({"project": resolved, "answer": answer})

    return tool_error(f"unknown action: {action}")


registry.register(
    name="cookie_project",
    toolset="cookie_project",
    schema=COOKIE_PROJECT_SCHEMA,
    handler=_handle_cookie_project,
    description="Cookie project registry, cwd resolver, and local project agent query",
    emoji="🍪",
)
