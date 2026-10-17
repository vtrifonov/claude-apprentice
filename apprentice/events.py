import copy
import fcntl
import json
import os
import re
import time
from typing import Callable, Dict, Iterator, List, Optional

from apprentice import SCHEMA_VERSION, config

MAX_LINE_BYTES = 32768
QUESTION_CAP = 2000
_FIDELITY = {"hook": "live", "backfill": "transcript"}
_MONTH_FILE_RE = re.compile(r"^\d{4}-\d{2}\.jsonl$")
_LOCK_WAIT = 0.05
_LOCK_SLEEP = 0.002

# Maps an older schema version to a function returning the record at the next version.
UPGRADERS: Dict[int, Callable[[dict], dict]] = {}


def new_id() -> str:
    raw = bytearray(os.urandom(16))
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    h = raw.hex()
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])


def envelope(kind: str, source: str, session_id: str, profile: str, entrypoint: Optional[str],
             cwd: Optional[str], repo: Optional[str], branch: Optional[str], ticket: Optional[str],
             transcript_path: Optional[str], ts: Optional[str] = None,
             flags: Optional[List[str]] = None) -> dict:
    if source not in _FIDELITY:
        raise ValueError("unknown source: %s" % source)
    out = {
        "v": SCHEMA_VERSION,
        "id": new_id(),
        "ts": ts if ts is not None else config.utc_now_iso(),
        "kind": kind,
        "source": source,
        "fidelity": _FIDELITY[source],
        "session_id": session_id,
        "profile": profile,
        "entrypoint": entrypoint,
        "cwd": cwd,
        "repo": repo,
        "branch": branch,
        "ticket": ticket,
        "transcript_path": transcript_path,
    }
    if flags:
        out["flags"] = list(flags)
    return out


def month_of(ts: str) -> str:
    parsed = config.parse_iso(ts)
    if parsed is None:
        parsed = config.parse_iso(config.utc_now_iso())
    return parsed.strftime("%Y-%m")


def events_path(home_dir: str, ts: str) -> str:
    return os.path.join(home_dir, "events", month_of(ts) + ".jsonl")


def month_files(home_dir: str) -> List[str]:
    folder = os.path.join(home_dir, "events")
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return [os.path.join(folder, n) for n in sorted(names) if _MONTH_FILE_RE.match(n)]


def _dump(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _questions(obj: dict) -> list:
    questions = obj.get("questions")
    if not isinstance(questions, list):
        return []
    return [q for q in questions if isinstance(q, dict)]


def _drop_raw_response(obj: dict) -> None:
    obj.pop("raw_response", None)


def _drop_option_descriptions(obj: dict) -> None:
    for q in _questions(obj):
        options = q.get("options")
        if isinstance(options, list):
            for opt in options:
                if isinstance(opt, dict):
                    opt.pop("description", None)


def _truncate_questions(obj: dict) -> None:
    for q in _questions(obj):
        if isinstance(q.get("question"), str):
            q["question"] = q["question"][:QUESTION_CAP]
    obj["truncated"] = True


def serialize(obj: dict) -> bytes:
    out = _dump(obj)
    if len(out) <= MAX_LINE_BYTES:
        return out
    obj = copy.deepcopy(obj)
    for step in (_drop_raw_response, _drop_option_descriptions, _truncate_questions):
        step(obj)
        out = _dump(obj)
        if len(out) <= MAX_LINE_BYTES:
            break
    return out


def _try_lock(fd: int) -> bool:
    deadline = time.monotonic() + _LOCK_WAIT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(_LOCK_SLEEP)


def append(path: str, obj: dict) -> None:
    data = serialize(obj)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        locked = _try_lock(fd)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def append_event(home_dir: str, obj: dict) -> str:
    path = events_path(home_dir, obj["ts"])
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    append(path, obj)
    return path


def _bump(counters: Optional[dict], key: str) -> None:
    if counters is not None:
        counters[key] = counters.get(key, 0) + 1


def _parse_line(line: bytes, counters: Optional[dict]) -> Optional[dict]:
    try:
        obj = json.loads(line.decode("utf-8"))
    except ValueError:
        _bump(counters, "bad")
        return None
    if not isinstance(obj, dict) or not all(k in obj for k in ("v", "id", "kind")):
        _bump(counters, "bad")
        return None
    v = obj["v"]
    if not isinstance(v, int) or isinstance(v, bool):
        _bump(counters, "bad")
        return None
    if v > SCHEMA_VERSION:
        _bump(counters, "future")
        return None
    while obj["v"] < SCHEMA_VERSION:
        upgrade = UPGRADERS.get(obj["v"])
        if upgrade is None:
            _bump(counters, "bad")
            return None
        obj = upgrade(obj)
    return obj


def iter_events(home_dir: str, months: Optional[List[str]] = None,
                counters: Optional[dict] = None) -> Iterator[dict]:
    wanted = set(months) if months is not None else None
    for path in month_files(home_dir):
        if wanted is not None and os.path.basename(path)[:-len(".jsonl")] not in wanted:
            continue
        try:
            fh = open(path, "rb")
        except OSError:
            continue
        with fh:
            for line in fh:
                if not line.strip():
                    continue
                obj = _parse_line(line, counters)
                if obj is not None:
                    yield obj


def tail_events(path: str, nbytes: int = 65536) -> List[dict]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - nbytes - 1)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    if start > 0:
        # The extra leading byte tells whether the window begins on a line boundary.
        cut = data.find(b"\n")
        data = data[cut + 1:] if cut >= 0 else b""
    out = []
    for line in data.split(b"\n"):
        if line.strip():
            obj = _parse_line(line, None)
            if obj is not None:
                out.append(obj)
    return out
