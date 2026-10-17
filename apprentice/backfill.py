"""Backfill: reconstruct events from Claude Code transcript lines."""
from __future__ import annotations

import calendar
import datetime
import fcntl
import json
import os
import signal
import time
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple

from apprentice import asks, config, events, filters, gitinfo
from apprentice.departure import find_ticket
from apprentice.scrub import scrub
from apprentice.transcript import LineStream, _anchor, message_text, parse_ask_answer

CLOCK = time.monotonic
SAVE_EVERY_FILES = 50
SAVE_EVERY_SECONDS = 5.0
WATERMARK_SLACK = 5.0
DUPLICATE_WINDOW = 120.0
PROGRESS_EVERY = 100
CURSOR_FILE = "backfill-cursor.json"
LOCK_FILE = "backfill.lock"
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


# Run model: discovery, cursor, dedup index and the locked run loop.

_ASK_KINDS = ("ask", "ask_rejected")
_KEYED_KINDS = ("prompt", "machine")
_OBSERVER_SLUG = "claude-mem-observer"
_SUBAGENTS_DIR = os.sep + "subagents" + os.sep
_RECENT_PER_TEXT = 20


def _project_roots(env) -> List[str]:
    home_dir = env.get("HOME") or os.path.expanduser("~")
    candidates = [os.path.join(home_dir, ".claude", "projects")]
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        candidates.append(os.path.join(os.path.expanduser(config_dir), "projects"))
    roots = []  # type: List[str]
    for root in candidates:
        real = os.path.realpath(root)
        if real not in roots and os.path.isdir(real):
            roots.append(real)
    return roots


def _month_start(month: str) -> float:
    year, mon = month.split("-")
    return float(calendar.timegm((int(year), int(mon), 1, 0, 0, 0, 0, 0, 0)))


def discover(cfg: dict, since: Optional[str], project: Optional[str], env) -> Tuple[List[str], dict]:
    stats = {"skipped_subagents": 0, "skipped_observer": 0, "skipped_entrypoint": 0, "skipped_marker": 0,
             "skipped_since": 0, "entrypoints": {}}  # type: dict
    floor = _month_start(since) if since else None
    found = []  # type: List[Tuple[float, str]]
    for root in _project_roots(env):
        try:
            slugs = sorted(os.listdir(root))
        except OSError:
            continue
        for slug in slugs:
            folder = os.path.join(root, slug)
            if (project is not None and slug != project) or not os.path.isdir(folder):
                continue
            for dirpath, _dirnames, filenames in os.walk(folder):
                for name in filenames:
                    if not name.endswith(".jsonl"):
                        continue
                    path = os.path.join(dirpath, name)
                    if _OBSERVER_SLUG in slug:
                        stats["skipped_observer"] += 1
                    elif _SUBAGENTS_DIR in path[len(root):]:
                        stats["skipped_subagents"] += 1
                    elif dirpath == folder:
                        try:
                            mtime = os.stat(path).st_mtime
                        except OSError:
                            continue
                        if floor is not None and mtime < floor:
                            stats["skipped_since"] += 1
                        else:
                            found.append((mtime, path))
    found.sort()
    allowed = cfg.get("capture_entrypoints") or []
    paths = []  # type: List[str]
    for _mtime, path in found:
        entrypoint, marker = session_entrypoint_and_marker(path)
        if entrypoint is not None and entrypoint not in allowed:
            stats["skipped_entrypoint"] += 1
        elif marker:
            stats["skipped_marker"] += 1
        else:
            stats["entrypoints"][path] = entrypoint
            paths.append(path)
    return paths, stats


def _write_private(path: str, data: bytes) -> None:
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)
    os.replace(tmp, path)


class Cursor(object):
    """Per-transcript progress: size and mtime seen, byte offset, lines done and the extractor state."""

    def __init__(self, path: str, files: Dict[str, dict]):
        self.path = path
        self.files = files
        self.unsaved = 0
        self.last_save = CLOCK()

    @classmethod
    def load(cls, home_dir: str) -> "Cursor":
        path = os.path.join(home_dir, "state", CURSOR_FILE)
        files = {}  # type: Dict[str, dict]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("files"), dict):
                files = {k: v for k, v in data["files"].items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass
        return cls(path, files)

    def get(self, path: str) -> Optional[dict]:
        return self.files.get(path)

    def set(self, path: str, size: int, mtime: float, state: FileState) -> None:
        self.files[path] = {"size": size, "mtime": mtime, "offset": state.offset, "lines_done": state.lines_done,
                            "state": state.to_json()}
        self.unsaved += 1

    def plan(self, path: str, size: int, mtime: float) -> Optional[FileState]:
        """None when the file is unchanged; else the state to extract from."""
        entry = self.get(path)
        if entry is None:
            return FileState()
        if entry.get("size") == size and entry.get("mtime") == mtime:
            return None
        saved = entry.get("state")
        if isinstance(entry.get("size"), int) and size > entry["size"] and isinstance(saved, dict):
            state = FileState.from_json(saved)
            if state.offset <= size:
                return state
        return FileState()

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        data = json.dumps({"v": 1, "files": self.files}, separators=(",", ":"), ensure_ascii=False)
        _write_private(self.path, data.encode("utf-8", "replace"))
        self.unsaved = 0
        self.last_save = CLOCK()

    def maybe_save(self) -> None:
        if self.unsaved >= SAVE_EVERY_FILES or CLOCK() - self.last_save >= SAVE_EVERY_SECONDS:
            self.save()


def _next_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    return "%04d-%02d" % (year + mon // 12, mon % 12 + 1)


def _text_was_cut(event: dict) -> bool:
    tags = event.get("tags")
    length = event.get("len")
    return (isinstance(tags, list) and "pasted" in tags) or (isinstance(length, int) and length > filters.TEXT_CAP)


def _prompt_key(event: dict) -> tuple:
    if event.get("kind") == "prompt":
        tail = event.get("sha1")
    else:
        tail = "machine:%s" % event.get("command")
    return (event.get("session_id"), event.get("ts"), tail)


class DedupIndex(object):
    """What the store already holds, so backfill never writes an event twice or over live capture."""

    def __init__(self, home_dir: str):
        self.home = home_dir
        self.watermark = {}  # type: Dict[str, float]
        self.ask_keys = set()  # type: Set[tuple]
        self.prompt_keys = set()  # type: Set[tuple]
        # normalized sha1 -> [(id, epoch, session_id, sha1, cut)], newest last
        self.recent_prompts = {}  # type: Dict[str, List[tuple]]
        self.dropped = {"watermark": 0, "ask_key": 0, "prompt_key": 0}
        self._months = set()  # type: Set[str]
        self._ids = set()  # type: Set[str]

    @classmethod
    def load(cls, home_dir: str, months: List[str]) -> "DedupIndex":
        index = cls(home_dir)
        for month in months:
            index._ensure(month)
        return index

    def _ensure(self, month: str) -> None:
        # each month touched loads with the next: a session's live capture can start after the month turns
        wanted = [m for m in (month, _next_month(month)) if m not in self._months]
        if not wanted:
            return
        self._months.update(wanted)
        for event in events.iter_events(self.home, wanted):
            self._index(event)

    def _index(self, event: dict) -> None:
        sid = event.get("session_id")
        kind = event.get("kind")
        epoch = config.ts_epoch(event.get("ts"))
        if event.get("source") == "hook" and isinstance(sid, str) and epoch is not None:
            if sid not in self.watermark or epoch < self.watermark[sid]:
                self.watermark[sid] = epoch
        if kind in _ASK_KINDS:
            self.ask_keys.add((sid, event.get("tool_use_id")))
        elif kind in _KEYED_KINDS and event.get("source") == "backfill":
            self.prompt_keys.add(_prompt_key(event))
        if kind == "prompt":
            self._remember(event, epoch)

    def _remember(self, event: dict, epoch: Optional[float]) -> None:
        text = event.get("text")
        event_id = event.get("id")
        if not isinstance(text, str) or event_id in self._ids:
            return
        self._ids.add(event_id)
        bucket = self.recent_prompts.setdefault(filters.normalized_sha1(text), [])
        bucket.append((event_id, epoch, event.get("session_id"), event.get("sha1"), _text_was_cut(event)))
        del bucket[:-_RECENT_PER_TEXT]

    def _drop(self, rule: str) -> bool:
        self.dropped[rule] += 1
        return False

    def accept(self, event: dict) -> bool:
        ts = event.get("ts")
        self._ensure(events.month_of(ts))
        sid = event.get("session_id")
        epoch = config.ts_epoch(ts)
        mark = self.watermark.get(sid)
        if mark is not None and epoch is not None and epoch >= mark - WATERMARK_SLACK:
            return self._drop("watermark")
        kind = event.get("kind")
        if kind in _ASK_KINDS:
            key = (sid, event.get("tool_use_id"))
            if key in self.ask_keys:
                return self._drop("ask_key")
            self.ask_keys.add(key)
        elif kind in _KEYED_KINDS:
            key = _prompt_key(event)
            if key in self.prompt_keys:
                return self._drop("prompt_key")
            self.prompt_keys.add(key)
        if kind == "prompt":
            self._remember(event, epoch)
        return True

    def duplicate_of(self, event: dict) -> Optional[str]:
        text = event.get("text")
        epoch = config.ts_epoch(event.get("ts"))
        if event.get("kind") != "prompt" or not isinstance(text, str) or epoch is None:
            return None
        cut = _text_was_cut(event)
        for other_id, other_epoch, other_sid, other_sha1, other_cut in reversed(
                self.recent_prompts.get(filters.normalized_sha1(text), [])):
            if other_sid == event.get("session_id") or other_epoch is None:
                continue
            # a cut text is a lossy view, so only the raw full-text hash can confirm the match
            if (cut or other_cut) and other_sha1 != event.get("sha1"):
                continue
            if abs(epoch - other_epoch) <= DUPLICATE_WINDOW:
                return other_id
        return None


def _retention_cutoff(months: int) -> Optional[str]:
    if months <= 0:
        return None
    now = time.gmtime()
    index = now.tm_year * 12 + now.tm_mon - 1 - months
    return "%04d-%02d-01T00:00:00Z" % (index // 12, index % 12 + 1)


def _acquire_lock(home_dir: str) -> Tuple[int, str]:
    """(fd, "") with the kernel lock held, or (-1, holder pid text) when another run holds it."""
    fd = os.open(os.path.join(home_dir, "state", LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            holder = os.read(fd, 32).decode("ascii", "replace").strip()
        finally:
            os.close(fd)
        return -1, holder or "?"
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(os.getpid()).encode("ascii"))
    return fd, ""


def _pairs(counts: Dict[str, int]) -> str:
    return ", ".join("%s %d" % (k, counts[k]) for k in sorted(counts)) or "none"


def run(args, env, log: Callable[[str], None]) -> dict:
    started = time.monotonic()
    home_dir = config.home()
    config.ensure_dirs(home_dir)
    fd, holder = _acquire_lock(home_dir)
    if fd < 0:
        print("backfill already running (pid %s)" % holder, flush=True)
        return {"exit": 1}
    stop = []  # type: List[int]
    previous = {}

    def on_signal(signum, _frame):
        stop.append(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, on_signal)
        except ValueError:  # not the main thread: run without handlers
            pass
    try:
        return _run_locked(args, env, log, home_dir, started, stop)
    finally:
        for signum, handler in previous.items():
            if handler is not None:
                signal.signal(signum, handler)
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        os.close(fd)


def _file_months(paths: List[str]) -> List[str]:
    months = set()
    for path in paths:
        try:
            months.add(time.strftime("%Y-%m", time.gmtime(os.stat(path).st_mtime)))
        except OSError:
            continue
    return sorted(months)


def _run_locked(args, env, log: Callable[[str], None], home_dir: str, started: float, stop: List[int]) -> dict:
    cfg = config.load_config(home_dir)
    dry = bool(getattr(args, "dry_run", False))
    since = getattr(args, "since", None)
    since_ts = since + "-01T00:00:00Z" if since else None
    cutoff_ts = _retention_cutoff(cfg["retention_months"])
    paths, stats = discover(cfg, since, getattr(args, "project", None), env)
    cursor = Cursor.load(home_dir)
    index = DedupIndex.load(home_dir, _file_months(paths))
    repo_cache = {}  # type: dict
    by_kind = {}  # type: Dict[str, int]
    written = duplicates = bytes_read = scanned = unchanged = errors = 0
    interrupted = False
    for path in paths:
        if stop:
            interrupted = True
            break
        scanned += 1
        try:
            st = os.stat(path)
            state = cursor.plan(path, st.st_size, st.st_mtime)
            if state is None:
                unchanged += 1
            else:
                start = state.offset
                for event in extract_file(path, state, cfg, cutoff_ts, since_ts, repo_cache,
                                          entrypoint=stats["entrypoints"].get(path)):
                    if index.accept(event):
                        if event["kind"] == "prompt":
                            event["duplicate_of"] = index.duplicate_of(event)
                            duplicates += event["duplicate_of"] is not None
                        if not dry:
                            events.append_event(home_dir, event)
                            written += 1
                        by_kind[event["kind"]] = by_kind.get(event["kind"], 0) + 1
                    if stop:
                        break
                if stop:
                    # the file's progress is not recorded; the next run redoes it and dedup drops what was written
                    interrupted = True
                    break
                bytes_read += state.offset - start
                cursor.set(path, st.st_size, st.st_mtime, state)
        except OSError:
            errors += 1
            continue
        if not dry:
            cursor.maybe_save()
        if scanned % PROGRESS_EVERY == 0:
            log("progress: files %d/%d, events written %d, bytes read %d, elapsed %.1fs"
                % (scanned, len(paths), written, bytes_read, time.monotonic() - started))
    if not dry:
        cursor.save()
    elapsed = time.monotonic() - started
    skipped = {k: stats["skipped_" + k] for k in ("subagents", "observer", "entrypoint", "marker", "since")}
    line = ("backfill %s: files scanned %d, skipped (subagents %d, observer %d, entrypoint %d, marker %d, since %d), "
            "unchanged %d, errors %d, events by kind (%s), dropped by rule (%s), duplicates %d, elapsed %.1fs%s"
            % ("interrupted" if interrupted else "done", scanned, skipped["subagents"], skipped["observer"],
               skipped["entrypoint"], skipped["marker"], skipped["since"], unchanged, errors, _pairs(by_kind),
               _pairs(index.dropped), duplicates, elapsed, " (dry run, nothing written)" if dry else ""))
    log(line)
    if not getattr(log, "to_stdout", False):
        print(line, flush=True)
    return {"exit": 0, "files_scanned": scanned, "files_total": len(paths), "skipped": skipped,
            "unchanged": unchanged, "errors": errors, "events_by_kind": by_kind, "events_written": written,
            "dropped": dict(index.dropped), "duplicates": duplicates, "bytes_read": bytes_read,
            "elapsed": elapsed, "interrupted": interrupted, "dry_run": dry, "line": line}
