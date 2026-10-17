"""Hook handlers for PreToolUse, PostToolUse, UserPromptSubmit and SessionStart."""
import copy
import datetime
import json
import os
import re
from typing import List, Mapping, Optional, Tuple

from apprentice import asks, config, events, filters, transcript, worktrees
from apprentice.scrub import scrub

MARKER_CAP = 16384
REJECT_AFTER_SECONDS = 1.0
DUPLICATE_WINDOW_SECONDS = 120.0
RECENT_BYTES = 65536
POST_TAIL_BYTES = 8192
_OBSERVER = "claude-mem-observer"
_SESSION_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _home(env: Mapping) -> str:
    raw = env.get("APPRENTICE_HOME")
    return os.path.expanduser(raw) if raw else config.home()


def pending_path(home: str, session_id: str) -> str:
    name = _SESSION_SAFE_RE.sub("_", str(session_id or "")) or "_"
    return os.path.join(home, "state", "pending-ask", name + ".json")


def resolve_entrypoint(payload: dict, env: Mapping, transcript_path: Optional[str],
                       read_tail: bool) -> Optional[str]:
    value = payload.get("entrypoint")
    if isinstance(value, str) and value:
        return value
    value = env.get("CLAUDE_CODE_ENTRYPOINT")
    if value:
        return value
    if read_tail and transcript_path:
        return transcript.tail_context(transcript_path, None, POST_TAIL_BYTES)["entrypoint"]
    return None


def should_capture(entrypoint: Optional[str], payload: dict, cfg: dict) -> Tuple[bool, List[str]]:
    for key in ("cwd", "transcript_path"):
        value = payload.get(key)
        if isinstance(value, str) and _OBSERVER in value:
            return False, []
    if entrypoint is None:
        return True, ["entrypoint_unknown"]
    return entrypoint in cfg["capture_entrypoints"], []


def _questions(payload: dict) -> list:
    tool_input = payload.get("tool_input")
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if not isinstance(questions, list):
        return []
    return [q for q in questions if isinstance(q, dict)]


def _question_texts(questions: list) -> List[str]:
    return [q["question"] for q in questions if isinstance(q.get("question"), str)]


def _scrub_value(value, cfg: dict):
    if isinstance(value, str):
        return scrub(value, cfg)[0]
    if isinstance(value, list):
        return [_scrub_value(v, cfg) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_value(v, cfg) for k, v in value.items()}
    return value


def _capped_marker(marker: dict) -> bytes:
    data = json.dumps(marker).encode("utf-8")
    if len(data) <= MARKER_CAP:
        return data
    marker = copy.deepcopy(marker)
    questions = marker["questions"]
    for q in questions:
        for opt in q.get("options") or []:
            if isinstance(opt, dict):
                opt.pop("description", None)
    cap = events.QUESTION_CAP
    while True:
        data = json.dumps(marker).encode("utf-8")
        if len(data) <= MARKER_CAP or cap == 0:
            return data
        for q in questions:
            if isinstance(q.get("question"), str):
                q["question"] = q["question"][:cap]
        cap //= 2


def _write_atomic(path: str, data: bytes) -> None:
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _read_marker(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _envelope(kind: str, payload: dict, env: Mapping, entrypoint: Optional[str],
              flags: List[str], *texts: Optional[str]) -> dict:
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    ctx = asks.departure_context(cwd, *texts)
    return events.envelope(kind, "hook", str(payload.get("session_id") or ""), config.current_profile(env),
                           entrypoint, cwd, ctx["repo"], ctx["branch"], ctx["ticket"],
                           payload.get("transcript_path"), flags=flags)


def _merge_flags(target: List[str], extra: List[str]) -> None:
    for flag in extra:
        if flag not in target:
            target.append(flag)


def handle_pre_tool(payload: dict, env: Mapping, home_dir: str, cfg: dict) -> Optional[str]:
    entrypoint = resolve_entrypoint(payload, env, payload.get("transcript_path"), False)
    capture, _ = should_capture(entrypoint, payload, cfg)
    if not capture:
        return None
    config.ensure_dirs(home_dir)
    marker = {
        "tool_use_id": payload.get("tool_use_id"),
        "ts": config.utc_now_iso(),
        "questions": _scrub_value(_questions(payload), cfg),
    }
    _write_atomic(pending_path(home_dir, payload.get("session_id")), _capped_marker(marker))
    return None


def handle_post_tool(payload: dict, env: Mapping, home_dir: str, cfg: dict) -> Optional[str]:
    path = payload.get("transcript_path")
    entrypoint = resolve_entrypoint(payload, env, path, True)
    capture, flags = should_capture(entrypoint, payload, cfg)
    if not capture:
        return None
    config.ensure_dirs(home_dir)
    tool_use_id = payload.get("tool_use_id")
    questions = _questions(payload)
    response = payload.get("tool_response")
    answers = transcript.parse_ask_answer(response)
    ask, scrub_flags, partial = asks.ask_payload(tool_use_id, questions, answers, response, cfg,
                                                 datetime.datetime.now())
    _merge_flags(flags, scrub_flags)
    record = _envelope("ask", payload, env, entrypoint, flags, *_question_texts(questions))
    if partial:
        record["partial"] = True
    record.update(ask)
    events.append_event(home_dir, record)
    marker_path = pending_path(home_dir, payload.get("session_id"))
    marker = _read_marker(marker_path)
    if marker is not None and marker.get("tool_use_id") == tool_use_id:
        _remove(marker_path)
    return None


def _age_seconds(ts, now: datetime.datetime) -> Optional[float]:
    parsed = config.parse_iso(ts) if isinstance(ts, str) else None
    if parsed is None:
        return None
    return (now - parsed).total_seconds()


def _check_rejected(payload: dict, env: Mapping, home_dir: str, cfg: dict, entrypoint: Optional[str],
                    flags: List[str], recent: List[dict], now: datetime.datetime) -> bool:
    marker_path = pending_path(home_dir, payload.get("session_id"))
    marker = _read_marker(marker_path)
    if marker is None:
        return False
    age = _age_seconds(marker.get("ts"), now)
    if age is not None and age <= REJECT_AFTER_SECONDS:
        return False
    tool_use_id = marker.get("tool_use_id")
    if any(e.get("kind") == "ask" and e.get("tool_use_id") == tool_use_id for e in recent):
        _remove(marker_path)
        return False
    questions = [q for q in marker.get("questions") or [] if isinstance(q, dict)]
    body, scrub_flags, _ = asks.ask_payload(tool_use_id, questions, None, None, cfg, datetime.datetime.now())
    record_flags = list(flags)
    _merge_flags(record_flags, scrub_flags)
    record = _envelope("ask_rejected", payload, env, entrypoint, record_flags, *_question_texts(questions))
    record.update(body)
    events.append_event(home_dir, record)
    _remove(marker_path)
    return True


def _duplicate_of(text: Optional[str], session_id, recent: List[dict], now: datetime.datetime) -> Optional[str]:
    if text is None:
        return None
    wanted = filters.normalized_sha1(text)
    for event in reversed(recent):
        if event.get("kind") != "prompt" or event.get("session_id") == session_id:
            continue
        other = event.get("text")
        if not isinstance(other, str) or filters.normalized_sha1(other) != wanted:
            continue
        age = _age_seconds(event.get("ts"), now)
        if age is not None and abs(age) <= DUPLICATE_WINDOW_SECONDS:
            return event.get("id")
    return None


def handle_user_prompt(payload: dict, env: Mapping, home_dir: str, cfg: dict) -> Optional[str]:
    path = payload.get("transcript_path")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        prompt = ""
    tail = transcript.tail_context(path or "", prompt)
    entrypoint = resolve_entrypoint(payload, env, path, False) or tail["entrypoint"]
    capture, flags = should_capture(entrypoint, payload, cfg)
    if not capture:
        return None
    config.ensure_dirs(home_dir)
    now = datetime.datetime.now(datetime.timezone.utc)
    recent = events.tail_events(events.events_path(home_dir, config.utc_now_iso()), RECENT_BYTES)
    after_rejected = _check_rejected(payload, env, home_dir, cfg, entrypoint, flags, recent, now)
    result = filters.classify(prompt)
    if result["outcome"] == "drop":
        return None
    if result["outcome"] == "machine":
        record = _envelope("machine", payload, env, entrypoint, flags, prompt)
        record["command"] = result["command"]
        events.append_event(home_dir, record)
        return None
    text, scrub_flags = scrub(result["text"], cfg)
    _merge_flags(flags, scrub_flags)
    record = _envelope("prompt", payload, env, entrypoint, flags, prompt)
    record.update({
        "text": text,
        "sha1": result["sha1"],
        "len": result["len"],
        "words": result["words"],
        "tags": result["tags"],
        "register": result["register"],
        "strong_marker": result["strong_marker"],
        "after_interrupt": tail["after_interrupt"],
        "after_rejected_ask": after_rejected,
        "prev_assistant": tail["prev_assistant"],
        "duplicate_of": _duplicate_of(text, record["session_id"], recent, now),
    })
    events.append_event(home_dir, record)
    return None


_ERROR_LOG_MAX = 262144
_ERROR_LOG_KEEP = 131072
_MARKER_MAX_AGE = 3600


def _utc_month_index() -> int:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.year * 12 + now.month - 1


def _prune_events(home_dir: str, retention_months: int) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    current = os.path.join(home_dir, "events", "%04d-%02d.jsonl" % (now.year, now.month))
    if not os.path.exists(current):
        fd = os.open(current, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
    if retention_months <= 0:
        return
    cutoff = _utc_month_index() - retention_months
    for path in events.month_files(home_dir):
        name = os.path.basename(path)
        index = int(name[:4]) * 12 + int(name[5:7]) - 1
        if index < cutoff:
            _remove(path)


def _truncate_error_log(home_dir: str) -> None:
    path = os.path.join(home_dir, "hook-errors.log")
    try:
        if os.path.getsize(path) <= _ERROR_LOG_MAX:
            return
        with open(path, "rb") as fh:
            fh.seek(-_ERROR_LOG_KEEP, os.SEEK_END)
            tail = fh.read()
    except OSError:
        return
    cut = tail.find(b"\n")
    _write_atomic(path, tail[cut + 1:] if cut >= 0 else b"")


def _prune_markers(home_dir: str) -> None:
    folder = os.path.join(home_dir, "state", "pending-ask")
    limit = datetime.datetime.now().timestamp() - _MARKER_MAX_AGE
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            if os.stat(path).st_mtime < limit:
                _remove(path)
        except OSError:
            continue


def handle_session_start(payload: dict, env: Mapping, home_dir: str, cfg: dict) -> Optional[str]:
    config.ensure_dirs(home_dir)
    _prune_events(home_dir, cfg["retention_months"])
    _truncate_error_log(home_dir)
    _prune_markers(home_dir)
    plugin_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    _write_atomic(os.path.join(home_dir, "state", "plugin-root"), plugin_root.encode("utf-8"))
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") else os.getcwd()
    status, content = worktrees.ensure_link(cwd)
    group = config.group_of_profile(config.current_profile(env), cfg, config.read_group_labels(home_dir))
    tone_path = None
    if group is not None:
        candidate = os.path.join(home_dir, "profile", "tone-%s.md" % config.group_slug(group))
        if os.path.isfile(candidate):
            tone_path = candidate
    info = {"ts": config.utc_now_iso(), "cwd": cwd, "is_worktree": worktrees.is_worktree(cwd),
            "link": status, "group": group, "tone_line": tone_path is not None}
    _write_atomic(os.path.join(home_dir, "state", "last-sessionstart.json"), json.dumps(info).encode("utf-8"))
    out = ""
    if status == "created" and content is not None:
        out += "Personal repo rules from CLAUDE.local.md (linked by apprentice):\n%s\n" % content
    if tone_path is not None:
        out += "apprentice tone file for this session (account group %s): %s\n" % (group, tone_path)
    return out or None


_HANDLERS = {
    "pre-tool": handle_pre_tool,
    "post-tool": handle_post_tool,
    "user-prompt": handle_user_prompt,
    "session-start": handle_session_start,
}


def handle(event: str, payload: dict, env: Mapping) -> Optional[str]:
    handler = _HANDLERS.get(event)
    if handler is None:
        return None
    home_dir = _home(env)
    return handler(payload, env, home_dir, config.load_config(home_dir))
