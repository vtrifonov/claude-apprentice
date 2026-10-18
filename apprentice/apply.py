"""Apply-side helpers."""
from __future__ import annotations

import difflib
import fcntl
import json
import os
import re
import stat
import subprocess
import tempfile
import time

from apprentice import config, gitinfo, ledger, scrub

if False:  # annotations only
    from typing import Callable, List, Optional, Tuple

START = "<!-- apprentice:start -->"
END = "<!-- apprentice:end -->"
HEADING = "## Learned from sessions"
LOCK_DEADLINE = 10.0
LOCK_POLL = 0.05

# the id comment is the last one on the line: rule text may hold "<!--" but never a raw "-->" (render_line)
_RULE_RE = re.compile(r"^- (.*?) ?<!-- apprentice:(r_[0-9a-f]+) n=\d+ [^<\n]*-->\s*$")
_NEWLINES_RE = re.compile(r"[ \t]*[\r\n]+[ \t]*")


def render_line(rule_id: str, text: str, n: int, band: str, date: str) -> str:
    one = _NEWLINES_RE.sub(" ", text.strip()).replace("-->", "--&gt;")
    return "- %s <!-- apprentice:%s n=%d %s %s -->" % (one, rule_id, n, band, date)


def split_rule_line(line: str) -> Optional[Tuple[str, str]]:
    match = _RULE_RE.match(line)
    if not match:
        return None
    return match.group(1), match.group(2)


def _section(lines: List[str]) -> Tuple[str, int, int]:
    """("none" | "malformed" | "ok", start index, end index) of the marked section in `lines`."""
    starts = [i for i, line in enumerate(lines) if line.strip() == START]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if not starts:
        return ("malformed" if ends else "none"), -1, -1
    if len(starts) > 1:
        return "malformed", -1, -1
    after = [i for i in ends if i > starts[0]]
    if not after:
        return "malformed", -1, -1
    return "ok", starts[0], after[0]


def _find_rule(lines: List[str], start: int, end: int, rule_id: str) -> Tuple[int, Optional[str]]:
    for i in range(start + 1, end):
        parsed = split_rule_line(lines[i])
        if parsed is not None and parsed[1] == rule_id:
            return i, parsed[0]
    return -1, None


def upsert_rule(content: str, line: str) -> Tuple[str, str]:
    parsed = split_rule_line(line)
    if parsed is None:
        raise ValueError("not a rule line")
    text, rule_id = parsed
    lines = content.splitlines(True)
    state, start, end = _section(lines)
    if state == "malformed":
        return content, "skipped:malformed-section"
    if state == "none":
        prefix = content
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"
        return prefix + START + "\n" + HEADING + "\n" + line + "\n" + END + "\n", "written"
    index, existing = _find_rule(lines, start, end, rule_id)
    if index >= 0:
        if existing == text:
            return content, "unchanged"
        old = lines[index]
        lines[index] = line + old[len(old.rstrip("\r\n")):]
    else:
        lines.insert(end, line + "\n")
    return "".join(lines), "written"


def remove_rule(content: str, rule_id: str) -> Tuple[str, str]:
    lines = content.splitlines(True)
    state, start, end = _section(lines)
    if state != "ok":
        return content, "unchanged"
    index, _ = _find_rule(lines, start, end, rule_id)
    if index < 0:
        return content, "unchanged"
    del lines[index]
    end -= 1
    rest = [line.strip() for line in lines[start + 1:end]]
    if all(not line or line == HEADING for line in rest):
        # nothing of value is left inside the markers: drop the section and the blank line we put before it
        first = start - 1 if start > 0 and not lines[start - 1].strip() else start
        del lines[first:end + 1]
    return "".join(lines), "removed"


def _acquire(fd: int) -> bool:
    deadline = time.monotonic() + LOCK_DEADLINE
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(LOCK_POLL)


def _read_target(real: str) -> Tuple[bytes, object]:
    with open(real, "rb") as fh:
        data = fh.read()
        st = os.fstat(fh.fileno())
    return data, st


def _same(real: str, data: bytes, st) -> bool:
    try:
        now = os.stat(real)
        if (now.st_mtime_ns, now.st_size) != (st.st_mtime_ns, st.st_size):
            return False
        with open(real, "rb") as fh:
            return fh.read() == data
    except OSError:
        return False


def _backup(home: str, real: str, data: bytes) -> str:
    directory = os.path.join(home, "backups", real.replace("/", "__"))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    ts = config.utc_now_iso().replace(":", "").replace("-", "")
    base = os.path.join(directory, "%s-%d" % (ts, os.getpid()))
    path, suffix = base, 0
    while True:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            suffix += 1
            path = "%s-%d" % (base, suffix)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def _unlink(path: Optional[str]) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _write_locked(path: str, transform: Callable[[str], Tuple[str, str]], home: str, dry_run: bool) -> dict:
    real = os.path.realpath(path)
    try:
        st = os.stat(real)
    except OSError:
        return {"result": "skipped:missing", "backup": None, "diff": None}
    if not stat.S_ISREG(st.st_mode):
        return {"result": "skipped:missing", "backup": None, "diff": None}
    if st.st_uid != os.getuid():
        return {"result": "skipped:not-owned", "backup": None, "diff": None}
    for _attempt in range(2):
        data, st = _read_target(real)
        content = data.decode("utf-8", "surrogateescape")
        new, result = transform(content)
        if result == "unchanged" or result.startswith("skipped:") or new == content:
            return {"result": result, "backup": None, "diff": None}
        if dry_run:
            diff = "".join(difflib.unified_diff(content.splitlines(True), new.splitlines(True), real, real))
            return {"result": result, "backup": None, "diff": diff}
        backup = _backup(home, real, data)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix=".apprentice-tmp-")
        try:
            try:
                os.write(fd, new.encode("utf-8", "surrogateescape"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
            if not _same(real, data, st):
                # someone edited the file since we read it: drop this attempt (and its backup) and redo it
                _unlink(tmp)
                _unlink(backup)
                continue
            os.replace(tmp, real)
        except BaseException:
            _unlink(tmp)
            raise
        return {"result": result, "backup": backup, "diff": None}
    return {"result": "skipped:concurrent-edit", "backup": None, "diff": None}


def write_target(path: str, transform: Callable[[str], Tuple[str, str]], home: str, dry_run: bool = False) -> dict:
    """Rewrite `path` (through symlinks) with `transform`, serialized on <home>/state/apply.lock.

    The target is backed up first, written to a temp file beside it, and only swapped in with os.replace when
    the file still matches what was read; one concurrent edit is retried, a second refuses.
    """
    lock = os.path.join(home, "state", "apply.lock")
    os.makedirs(os.path.dirname(lock), mode=0o700, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _acquire(fd):
            return {"result": "skipped:lock-timeout", "backup": None, "diff": None}
        try:
            return _write_locked(path, transform, home, dry_run)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def apply_rule_to_file(path: str, rule_id: str, text: str, n: int, band: str, date: str, home: str,
                       dry_run: bool = False, cfg: Optional[dict] = None) -> dict:
    clean = scrub.scrub(text, cfg if cfg is not None else config.load_config(home))[0] or ""
    line = render_line(rule_id, clean, n, band, date)
    out = write_target(path, lambda content: upsert_rule(content, line), home, dry_run=dry_run)
    if not dry_run:
        ledger.append_applied(home, {"action": "apply", "id": rule_id, "file": path, "line": line,
                                     "backup": out["backup"], "result": out["result"], "tracked": False,
                                     "remote_visibility": None})
    return out


def _find_repo_root(path: str) -> Optional[str]:
    directory = os.path.dirname(path)
    while True:
        if os.path.lexists(os.path.join(directory, ".git")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def _visibility(remote: Optional[str], timeout: float) -> str:
    if not remote:
        return "unknown"
    binary = os.environ.get("APPRENTICE_GH_BIN") or "gh"
    try:
        result = subprocess.run([binary, "repo", "view", remote, "--json", "visibility"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=timeout)
        if result.returncode != 0:
            return "unknown"
        value = json.loads(result.stdout.decode("utf-8", "replace")).get("visibility")
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return "unknown"
    value = value.lower() if isinstance(value, str) else None
    return value if value in ("public", "private") else "unknown"


def _excluded(root: str, path: str) -> Optional[bool]:
    if os.path.basename(path) != "CLAUDE.local.md" or os.path.dirname(path) != root:
        return None
    if gitinfo.main_checkout(root) != root:
        return None
    exclude = gitinfo.exclude_path(root)
    text = gitinfo._read(exclude) if exclude else None
    lines = [line.strip() for line in (text or "").splitlines()]
    return "CLAUDE.local.md" in lines or "/CLAUDE.local.md" in lines


def inspect_target(path: str, cfg: dict, timeout: float = 5.0) -> dict:
    """Describe where a target file lives.

    `tracked` here means the file sits inside a git work tree (a `.git` is found above its realpath), the
    "tracked in <root>" sense used for skills. It is not `gitinfo.is_tracked`, which asks whether a file is in
    the index and is what decides CLAUDE.local.md. Public-mirror skills are also reported as tracked.
    """
    real = os.path.realpath(path)
    root = _find_repo_root(real)
    info = {"tracked": False, "root": None, "remote": None, "visibility": None,
            "public_mirror": False, "excluded": None}
    if root is not None:
        info["tracked"] = True
        info["root"] = root
        info["remote"] = gitinfo.origin(root)
        info["visibility"] = _visibility(info["remote"], timeout)
        info["excluded"] = _excluded(root, real)
    if os.path.basename(real) == "SKILL.md" and os.path.basename(os.path.dirname(path)) in (cfg.get("public_skills") or []):
        info["tracked"] = True
        info["visibility"] = "public"
        info["public_mirror"] = True
    return info
