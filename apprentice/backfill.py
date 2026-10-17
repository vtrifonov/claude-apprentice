"""Backfill: reconstruct events from Claude Code transcript lines."""
from __future__ import annotations

import datetime
import json
import os
from typing import Dict, Iterator, List, Optional, Tuple

from apprentice import asks, config, events, filters, gitinfo
from apprentice.departure import find_ticket
from apprentice.scrub import scrub
from apprentice.transcript import LineStream, _anchor, message_text, parse_ask_answer

PENDING_CAP = 20
PRECLASSIFY_MARKER = "[apprentice-preclassify v1]"
_HEAD_BYTES = 64 * 1024
_USER_KEEP = 100
_INTERRUPT_PREFIX = "[Request interrupted by user"
_REJECT_PREFIXES = ("The user doesn't want to proceed", "The user wants to clarify these questions")
_ASK_TOOL = "AskUserQuestion"


class FileState(object):
    def __init__(self, offset: int = 0, lines_done: int = 0, pending_asks: Optional[List[dict]] = None,
                 last_assistant: Optional[str] = None, last_user: Optional[str] = None,
                 last_user_rejected: bool = False):
        self.offset = offset
        self.lines_done = lines_done
        self.pending_asks = pending_asks if pending_asks is not None else []  # type: List[dict]
        self.last_assistant = last_assistant
        self.last_user = last_user
        self.last_user_rejected = last_user_rejected

    def to_json(self) -> dict:
        return {
            "offset": self.offset,
            "lines_done": self.lines_done,
            "pending_asks": list(self.pending_asks),
            "last_assistant": self.last_assistant,
            "last_user": self.last_user,
            "last_user_rejected": self.last_user_rejected,
        }

    @classmethod
    def from_json(cls, data: dict) -> "FileState":
        pending = data.get("pending_asks")
        return cls(
            offset=int(data.get("offset") or 0),
            lines_done=int(data.get("lines_done") or 0),
            pending_asks=[p for p in pending if isinstance(p, dict)] if isinstance(pending, list) else [],
            last_assistant=data.get("last_assistant"),
            last_user=data.get("last_user"),
            last_user_rejected=bool(data.get("last_user_rejected")),
        )


def _head_lines(path: str) -> List[dict]:
    try:
        with open(path, "rb") as fh:
            data = fh.read(_HEAD_BYTES + 1)
    except OSError:
        return []
    pieces = data.split(b"\n")
    if len(data) > _HEAD_BYTES:
        pieces = pieces[:-1]  # the last piece is cut mid-line
    out = []
    for raw in pieces:
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def session_entrypoint_and_marker(path: str) -> Tuple[Optional[str], bool]:
    entrypoint = None
    marker = False
    seen_user = False
    for obj in _head_lines(path):
        if entrypoint is None and isinstance(obj.get("entrypoint"), str) and obj["entrypoint"]:
            entrypoint = obj["entrypoint"]
        if not seen_user and obj.get("type") == "user":
            seen_user = True
            marker = (message_text(obj) or "").startswith(PRECLASSIFY_MARKER)
    return entrypoint, marker


def _scrub_tree(value, cfg: dict, flags: List[str]):
    if isinstance(value, str):
        out, got = scrub(value, cfg)
        for flag in got:
            if flag not in flags:
                flags.append(flag)
        return out
    if isinstance(value, list):
        return [_scrub_tree(v, cfg, flags) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_tree(v, cfg, flags) for k, v in value.items()}
    return value


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _local_time(epoch: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(epoch)


def _profile_for(cwd: Optional[str], cfg: dict) -> str:
    if cwd:
        for rule in cfg.get("profile_rules") or []:
            prefix = rule.get("cwd_prefix") if isinstance(rule, dict) else None
            if isinstance(prefix, str) and prefix and cwd.startswith(prefix):
                return rule.get("profile") or "unknown"
    return "unknown"


def _repo_for(cwd: Optional[str], cfg: dict, repo_cache: dict) -> Optional[str]:
    if not cwd:
        return None
    if cwd not in repo_cache:
        main = gitinfo.resolve_main_for_cwd(cwd, cfg["worktree_suffix"])
        repo_cache[cwd] = gitinfo.origin(main) if main else None
    return repo_cache[cwd]


class _Line(object):
    """Per-line context shared by the event builders."""

    def __init__(self, obj: dict, path: str, cfg: dict, repo_cache: dict, entrypoint: Optional[str],
                 emit: bool):
        self.obj = obj
        self.path = path
        self.cfg = cfg
        self.entrypoint = entrypoint
        self.emit = emit
        self.ts = obj["timestamp"] if isinstance(obj.get("timestamp"), str) else None
        cwd = obj.get("cwd")
        self.cwd = cwd if isinstance(cwd, str) else None
        self.branch = obj["gitBranch"] if isinstance(obj.get("gitBranch"), str) and obj["gitBranch"] else None
        self.profile = _profile_for(self.cwd, cfg)
        self.repo = _repo_for(self.cwd, cfg, repo_cache)
        stem = os.path.splitext(os.path.basename(path))[0]
        sid = obj.get("sessionId")
        self.session_id = sid if isinstance(sid, str) and sid else stem

    def envelope(self, kind: str, flags: List[str], *texts: Optional[str]) -> dict:
        flags = list(flags)
        if self.entrypoint is None:
            flags.append("entrypoint_unknown")
        return events.envelope(
            kind, "backfill", self.session_id, self.profile, self.entrypoint, self.cwd, self.repo,
            self.branch, find_ticket(self.branch, self.cwd, *texts), self.path, ts=self.ts, flags=flags)

    def when_local(self) -> datetime.datetime:
        return _local_time(config.ts_epoch(self.ts))


def _ask_event(line: _Line, pending: dict, answers: Optional[Dict[str, str]], rejected: bool) -> dict:
    flags = list(pending.get("flags") or [])
    questions = pending["questions"]
    if answers:
        answers = {_scrub_tree(k, line.cfg, flags): _scrub_tree(v, line.cfg, flags) for k, v in answers.items()}
    payload, got, partial = asks.ask_payload(pending["tool_use_id"], questions, None if rejected else answers,
                                             None, line.cfg, line.when_local())
    for flag in got:
        if flag not in flags:
            flags.append(flag)
    texts = [q["question"] for q in questions if isinstance(q.get("question"), str)]
    record = line.envelope("ask_rejected" if rejected else "ask", flags, *texts)
    if partial and not rejected:
        record["partial"] = True
    record.update(payload)
    return record


def _prompt_event(line: _Line, text: str, state: FileState, was_rejected: bool) -> Optional[dict]:
    if text.startswith(_INTERRUPT_PREFIX):
        return None
    result = filters.classify(text)
    if result["outcome"] == "drop":
        return None
    if result["outcome"] == "machine":
        record = line.envelope("machine", [], text)
        record["command"] = result["command"]
        return record
    stored, flags = scrub(result["text"], line.cfg)
    record = line.envelope("prompt", flags, text)
    record.update({
        "text": stored,
        "sha1": result["sha1"],
        "len": result["len"],
        "words": result["words"],
        "tags": result["tags"],
        "register": result["register"],
        "strong_marker": result["strong_marker"],
        "after_interrupt": (state.last_user or "").startswith(_INTERRUPT_PREFIX),
        "after_rejected_ask": was_rejected,
        "prev_assistant": state.last_assistant,
        "duplicate_of": None,
    })
    return record


def _handle_assistant(line: _Line, state: FileState) -> None:
    message = line.obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == _ASK_TOOL):
                continue
            tool_input = block.get("input")
            questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
            if not isinstance(block.get("id"), str) or not isinstance(questions, list):
                continue
            flags = []  # type: List[str]
            state.pending_asks.append({
                "tool_use_id": block["id"],
                "questions": _scrub_tree([q for q in questions if isinstance(q, dict)], line.cfg, flags),
                "ts": line.ts,
                "flags": flags,
            })
        del state.pending_asks[:-PENDING_CAP]
    text = message_text(line.obj)
    anchor = _anchor(text)
    if anchor is not None:
        state.last_assistant = anchor


def _answers_for(block: dict, tool_use_result) -> Optional[Dict[str, str]]:
    if isinstance(tool_use_result, dict) and isinstance(tool_use_result.get("answers"), dict):
        return parse_ask_answer(tool_use_result)
    return parse_ask_answer(block.get("content"))


def _handle_user(line: _Line, state: FileState) -> List[dict]:
    out = []  # type: List[dict]
    message = line.obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    was_rejected = state.last_user_rejected
    rejected_here = False
    if isinstance(content, list):
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            index = next((i for i, p in enumerate(state.pending_asks)
                          if p.get("tool_use_id") == block.get("tool_use_id")), None)
            if index is None:
                continue
            pending = state.pending_asks.pop(index)
            rejected = bool(block.get("is_error")) or _result_text(block.get("content")).startswith(_REJECT_PREFIXES)
            rejected_here = rejected_here or rejected
            if line.emit:
                out.append(_ask_event(line, pending, _answers_for(block, line.obj.get("toolUseResult")), rejected))
    text = message_text(line.obj)
    if text is not None:
        if line.emit:
            record = _prompt_event(line, text, state, was_rejected)
            if record is not None:
                out.append(record)
        state.last_user = text[:_USER_KEEP]
    state.last_user_rejected = rejected_here
    return out


def extract_file(path: str, state: FileState, cfg: dict, cutoff_ts: Optional[str], since_ts: Optional[str],
                 repo_cache: dict, entrypoint: Optional[str] = None) -> Iterator[dict]:
    floors = [e for e in (config.ts_epoch(cutoff_ts), config.ts_epoch(since_ts)) if e is not None]
    floor = max(floors) if floors else None
    stream = LineStream(path, state.offset)
    for pos, raw in stream:
        state.offset = pos
        state.lines_done += 1
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        epoch = config.ts_epoch(obj.get("timestamp"))
        emit = epoch is not None and (floor is None or epoch >= floor)
        line = _Line(obj, path, cfg, repo_cache, entrypoint, emit)
        kind = obj.get("type")
        if kind == "assistant":
            _handle_assistant(line, state)
            continue
        if kind != "user":
            continue
        for record in _handle_user(line, state):
            yield record
    state.offset = stream.offset
