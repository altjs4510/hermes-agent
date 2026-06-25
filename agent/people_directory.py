"""People directory — deterministic sender→identity/group resolution.

Single source of truth for "who is this requester" is a people file (YAML),
pointed to by HERMES_PEOPLE_FILE. This module joins an inbound sender's
platform id against it so the agent NEVER has to search_files/grep to learn who
someone is or which part they belong to — the info is handed in for free.

Two consumers:
  - identity injection (gateway turn context): resolve_person(uid) → a one-line
    "요청자: <name> / <part> / <title>" so the agent knows the requester.
  - governed-skill tool re-grant (skills_tool): in_group(uid, group) for the
    skill's governance.triggerers (e.g. app_part_members).

Generic by design: core ships no org specifics. HERMES_PEOPLE_FILE supplies the
file; GROUP_SECTIONS maps a logical group name to a people-file section. owner /
executive stay env-driven (HERMES_OWNER_IDS / HERMES_EXECUTIVE_IDS) to match the
agent_init access gate. If no people file, custom groups fall back to
HERMES_GROUP_<NAME> env lists, so nothing breaks when unconfigured.
"""
import os
import threading

# Logical group → people-file section holding its members. Deployment convention.
GROUP_SECTIONS = {
    "app_part_members": "ai_app_part",
}

_lock = threading.Lock()
_cache = {"path": None, "mtime": None, "by_id": {}, "group_ids": {}}


def _people_path():
    p = os.getenv("HERMES_PEOPLE_FILE", "")
    return os.path.expanduser(p) if p else ""


def _walk_people(obj, out):
    """Index every dict that has a slack_id by slack_id, MERGING fields across
    occurrences (a person may appear in multiple sections — e.g. ai_app_part has
    nickname, the flat roster has part/title). First non-empty value per key wins."""
    if isinstance(obj, dict):
        sid = obj.get("slack_id")
        if isinstance(sid, str) and sid.startswith("U"):
            cur = out.setdefault(sid, {})
            for k, v in obj.items():
                if k not in cur or cur[k] in (None, "", []):
                    cur[k] = v
        for v in obj.values():
            _walk_people(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_people(v, out)


def _section_ids(doc, section):
    out = {}
    _walk_people(doc.get(section) if isinstance(doc, dict) else None, out)
    return set(out.keys())


def _load():
    """(Re)load the people file into cache if path/mtime changed. Best-effort."""
    path = _people_path()
    if not path or not os.path.exists(path):
        _cache.update(path=path, mtime=None, by_id={}, group_ids={})
        return
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if _cache["path"] == path and _cache["mtime"] == mtime and _cache["by_id"]:
        return
    try:
        import yaml
        doc = yaml.safe_load(open(path, encoding="utf-8"))
    except Exception:
        return  # keep stale cache rather than wipe on a transient read error
    if not isinstance(doc, dict):
        return
    by_id = {}
    _walk_people(doc, by_id)
    group_ids = {g: _section_ids(doc, sec) for g, sec in GROUP_SECTIONS.items()}
    _cache.update(path=path, mtime=mtime, by_id=by_id, group_ids=group_ids)


def resolve_person(slack_id):
    """Return the person dict for slack_id (name/nickname/part/title/...), or None."""
    if not slack_id:
        return None
    with _lock:
        _load()
        return _cache["by_id"].get(slack_id)


def identity_line(slack_id):
    """One-line requester identity for context injection, or '' if unknown."""
    p = resolve_person(slack_id)
    if not p:
        return ""
    name = p.get("name") or p.get("nickname") or slack_id
    bits = [str(name)]
    if p.get("part"):
        bits.append(str(p["part"]))
    if p.get("title"):
        bits.append(str(p["title"]))
    return " / ".join(bits)


def _env_ids(env):
    return {x.strip() for x in os.getenv(env, "").split(",") if x.strip()}


def in_group(slack_id, group):
    """True iff slack_id belongs to the logical group.

    owner/executive resolve via env (matches agent_init). Custom groups resolve
    from the people file section (GROUP_SECTIONS); if the file is absent, fall
    back to HERMES_GROUP_<NAME> env list."""
    if not slack_id:
        return False
    if group == "owner":
        return slack_id in _env_ids("HERMES_OWNER_IDS")
    if group == "executive":
        return slack_id in _env_ids("HERMES_EXECUTIVE_IDS")
    with _lock:
        _load()
        ids = _cache["group_ids"].get(group)
    if ids:
        return slack_id in ids
    # No people-file membership for this group → env fallback.
    return slack_id in _env_ids("HERMES_GROUP_" + str(group).upper())
