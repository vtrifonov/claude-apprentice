"""One-off repair of stored events: re-run the scrubber over their free text.

Before the scrub-then-cut fix, prompt text was cut before it was scrubbed, so a secret that straddled the cut
kept a fragment the scrubber no longer recognised. The whole secret is gone, so such a fragment cannot be
re-scrubbed reliably: a prompt text that ends at its cut and whose last characters look like the start of a
secret is replaced as a whole. Event files are rewritten under the same lock appends take.
"""
import fcntl
import json
import os
import re
from typing import Dict, List, Optional

from apprentice import config, events, filters, scrub

CUT_PLACEHOLDER = "<redacted:cut-fragment>"
CUT_FLAG = "redacted:cut-fragment"
TAIL_CHARS = 40
_FRAGMENT_RES = [
    re.compile(r"AKIA[A-Z0-9]{4,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{2,}"),
    re.compile(r"\d(?:[ -]?\d){11,}"),
]
_LITERAL_RE = re.compile(r"^(?:\(\?i\))?([^\\.^$*+?{}\[\]|()]*)")
_MIN_PREFIX = 4
_FREE_TEXT_KEYS = ("text", "prev_assistant", "questions", "raw_response")


def _custom_prefixes(cfg: dict) -> List[str]:
    """The literal head of each extra_redact_patterns entry, when it is long enough to stand for the pattern."""
    out = []
    for raw in cfg.get("extra_redact_patterns") or []:
        if not isinstance(raw, str):
            continue
        literal = _LITERAL_RE.match(raw).group(1)
        if len(literal) >= _MIN_PREFIX:
            out.append(literal[:_MIN_PREFIX].lower())
    return out


def _was_cut(event: dict) -> bool:
    length = event.get("len")
    if not isinstance(length, int):
        return False
    pasted = "pasted" in (event.get("tags") or [])
    return length > (filters.PASTED_CAP if pasted else filters.TEXT_CAP)


def _fragment_at_cut(text: str, prefixes: List[str]) -> bool:
    tail = text[-TAIL_CHARS:]
    if any(regex.search(tail) for regex in _FRAGMENT_RES):
        return True
    lowered = tail.lower()
    return any(prefix in lowered for prefix in prefixes)


def _scrub_tree(value, cfg: dict, flags: List[str]):
    if isinstance(value, str):
        out, got = scrub.scrub(value, cfg)
        flags.extend(f for f in got if f.startswith("redacted:") and f not in flags)
        return out
    if isinstance(value, list):
        return [_scrub_tree(v, cfg, flags) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_tree(v, cfg, flags) for k, v in value.items()}
    return value


def rescrub_event(event: dict, cfg: dict, prefixes: List[str], counts: Dict[str, int]) -> Optional[dict]:
    """The repaired event, or None when nothing changed."""
    out = dict(event)
    flags = []  # type: List[str]
    for key in _FREE_TEXT_KEYS:
        if key in out:
            out[key] = _scrub_tree(out[key], cfg, flags)
    text = out.get("text")
    if out.get("kind") == "prompt" and isinstance(text, str) and _was_cut(out) and _fragment_at_cut(text, prefixes):
        out["text"] = CUT_PLACEHOLDER
        flags.append(CUT_FLAG)
        counts["cut_redacted"] += 1
    if out == event:
        return None
    merged = list(event.get("flags") or [])
    merged.extend(f for f in flags if f not in merged)
    out["flags"] = merged
    return out


def _rewrite_lines(data: bytes, cfg: dict, prefixes: List[str], counts: Dict[str, int]) -> bytes:
    out = []  # type: List[bytes]
    for line in data.splitlines(True):
        try:
            event = json.loads(line.decode("utf-8"))
        except ValueError:
            out.append(line)
            continue
        fixed = rescrub_event(event, cfg, prefixes, counts) if isinstance(event, dict) else None
        counts["events"] += 1
        if fixed is None:
            out.append(line)
        else:
            counts["events_changed"] += 1
            out.append(events.serialize(fixed))
    return b"".join(out)


def _rewrite_file(path: str, cfg: dict, prefixes: List[str], counts: Dict[str, int], apply: bool) -> None:
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            data = b""
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                data += chunk
            changed_before = counts["events_changed"]
            new = _rewrite_lines(data, cfg, prefixes, counts)
            if not apply or counts["events_changed"] == changed_before:
                return
            # an append that gave up waiting for the lock writes unlocked: carry it over too
            extra = b""
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                extra += chunk
            config.write_private(path, new + _rewrite_lines(extra, cfg, prefixes, counts))
            counts["files_rewritten"] += 1
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def run(home: str, cfg: dict, apply: bool = False) -> Dict[str, int]:
    counts = {"files": 0, "files_rewritten": 0, "events": 0, "events_changed": 0, "cut_redacted": 0}
    prefixes = _custom_prefixes(cfg)
    for path in events.month_files(home):
        counts["files"] += 1
        _rewrite_file(path, cfg, prefixes, counts, apply)
    return counts
