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
                       dry_run: bool = False, cfg: Optional[dict] = None, info: Optional[dict] = None) -> dict:
    clean = scrub.scrub(text, cfg if cfg is not None else config.load_config(home))[0] or ""
    line = render_line(rule_id, clean, n, band, date)
    out = write_target(path, lambda content: upsert_rule(content, line), home, dry_run=dry_run)
    out["line"] = line
    if not dry_run:
        ledger.append_applied(home, {"action": "apply", "id": rule_id, "file": path, "line": line,
                                     "backup": out["backup"], "result": out["result"],
                                     "tracked": bool(info and info.get("tracked")),
                                     "remote_visibility": info.get("visibility") if info else None})
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


def _whole_word(word: str, text: str) -> bool:
    return re.search(r"(?<!\w)%s(?!\w)" % re.escape(word), text, re.IGNORECASE) is not None


def sensitive_check(text: str, tracked: bool, repos: List[str], cfg: Optional[dict]) -> Optional[str]:
    """`sensitive-text:<kind>` when `text` must not be written to the target, else None."""
    for flag in scrub.scrub(text, cfg)[1]:
        if flag.startswith("redacted:"):
            return "sensitive-text:" + flag[len("redacted:"):]
    if not tracked:
        return None
    if scrub.EMAIL_RE.search(text):
        return "sensitive-text:email"
    if scrub.TICKET_RE.search(text):
        return "sensitive-text:ticket"
    if scrub.UUID_RE.search(text):
        return "sensitive-text:uuid"
    for repo in repos:
        for name in (repo, repo.rsplit("/", 1)[-1]):
            if name and _whole_word(name, text):
                return "sensitive-text:repo"
    return None


def resolve_target(scope: str, target: Optional[str], skill: Optional[str], cfg: Optional[dict],
                   real_home: str) -> Tuple[Optional[str], Optional[str]]:
    """(path, error) for the `global` and `skill` scopes; a read-only skill falls back to the global target."""
    from apprentice import scope as scoping  # scope imports this module

    global_path = os.path.realpath(os.path.join(real_home, ".claude", "CLAUDE.md"))
    if scope == "global":
        return global_path, None
    if scope != "skill":
        return None, "skipped:bad-scope"
    path = target
    if not path and skill:
        directory = scoping.find_skill_dir(skill, real_home)
        if directory is None:
            return None, "skipped:skill-not-found"
        path = os.path.join(directory, "SKILL.md")
    if not path:
        return None, "skipped:no-target"
    if os.path.isdir(path):
        path = os.path.join(path, "SKILL.md")
    if scoping.is_readonly_skill(path):
        return global_path, None
    return path, None


def _outcome(result: str, path: Optional[str] = None, info: Optional[dict] = None, out: Optional[dict] = None) -> dict:
    out = out or {}
    return {"result": result, "file": path, "line": out.get("line"), "backup": out.get("backup"),
            "tracked": bool(info and info.get("tracked")),
            "remote_visibility": info.get("visibility") if info else None, "diff": out.get("diff")}


def _pack_facts(args, home: str, merged_ids: Optional[List[str]]) -> Tuple[int, str, List[str]]:
    """(n, band, repos) for the candidate (and any merged ones) from the pack and its private sidecar."""
    if not args.pack:
        return 0, "manual", []
    candidates = ledger.pack_candidates(ledger.load_pack(args.pack))
    private = ledger.read_private(home, args.pack)
    cand = candidates.get(args.id, {})
    own = private.get(args.id) if isinstance(private.get(args.id), dict) else {}
    inputs = cand.get("band_inputs") if isinstance(cand.get("band_inputs"), dict) else {}
    n = own.get("n") if isinstance(own.get("n"), int) else inputs.get("n")
    band = cand.get("band")
    repos = []  # type: List[str]
    for rule_id in [args.id] + list(merged_ids or []):
        for repo in candidates.get(rule_id, {}).get("repos") or []:
            if isinstance(repo, str) and repo not in repos:
                repos.append(repo)
    return (n if isinstance(n, int) else 0), (band if isinstance(band, str) and band else "manual"), repos


def apply_candidate(args, home: str, cfg: dict, real_home: str, merged_ids: Optional[List[str]] = None) -> dict:
    """Write one accepted rule to its global or skill target and ledger it."""
    path, error = resolve_target(args.scope, args.target, getattr(args, "skill", None), cfg, real_home)
    if error:
        return _outcome(error)
    info = inspect_target(path, cfg)
    if info["tracked"] and not args.confirm_tracked:
        return _outcome("skipped:git-tracked", path, info)
    if info["tracked"] and not args.pack:
        return _outcome("skipped:no-pack", path, info)
    real = os.path.realpath(path)
    if os.path.basename(real) == "CLAUDE.md" and info["root"] is not None:
        global_real = os.path.realpath(os.path.join(real_home, ".claude", "CLAUDE.md"))
        if info["root"] != _find_repo_root(global_real):
            return _outcome("skipped:repo-claude-md", path, info)
    n, band, repos = _pack_facts(args, home, merged_ids)
    flagged = sensitive_check(args.text, info["tracked"], repos, cfg)
    if flagged:
        return _outcome("skipped:" + flagged, path, info)
    out = apply_rule_to_file(path, args.id, args.text, n, band, config.utc_now_iso()[:10], home,
                             dry_run=bool(args.dry_run), cfg=cfg, info=info)
    if not args.dry_run and out["result"] in ("written", "unchanged"):
        parsed = split_rule_line(out["line"])
        ledger.record_decision(home, args.pack, args.id, "accepted", text=parsed[0] if parsed else None,
                               scope=args.scope, target=path, merged_ids=merged_ids,
                               profile=config.current_profile(os.environ))
    return _outcome(out["result"], path, info, out)


def _last_written(rows: List[dict], rule_id: str, actions: Tuple[str, ...]) -> Optional[dict]:
    for entry in reversed(rows):
        if (entry.get("id") == rule_id and entry.get("action") in actions and isinstance(entry.get("file"), str)
                and entry.get("result") in ("written", "unchanged")):
            return entry
    return None


def undo(rule_id: str, home: str, cfg: Optional[dict], dry_run: bool = False) -> dict:
    entry = _last_written(ledger.read_applied(home), rule_id, ("apply", "replay"))
    if entry is None:
        return {"result": "skipped:not-applied", "file": None, "backup": None, "diff": None}
    out = write_target(entry["file"], lambda content: remove_rule(content, rule_id), home, dry_run=dry_run)
    out["file"] = entry["file"]
    if not dry_run and out["result"] in ("removed", "unchanged"):
        ledger.append_applied(home, {"action": "undo", "id": rule_id, "file": entry["file"],
                                     "backup": out["backup"], "result": out["result"]})
        ledger.record_decision(home, None, rule_id, "undone", target=entry["file"],
                               profile=config.current_profile(os.environ))
    return out


def _replay_one(rule_id: str, entry: dict, home: str, cfg: Optional[dict], confirm_tracked: bool,
                dry_run: bool) -> dict:
    path, line = entry["file"], entry["line"]

    def transform(content: str) -> Tuple[str, str]:
        return upsert_rule(content, line)

    probe = write_target(path, transform, home, dry_run=True)
    out = {"id": rule_id, "file": path, "result": probe["result"], "backup": None, "diff": None}
    if probe["result"] != "written":
        return out
    info = inspect_target(path, cfg or {})
    if info["tracked"] and not confirm_tracked:
        out["result"] = "skipped:git-tracked"
        return out
    # T28: repo targets run the exclude and index steps here
    if dry_run:
        out["diff"] = probe["diff"]
        return out
    written = write_target(path, transform, home)
    out.update(result=written["result"], backup=written["backup"])
    if written["result"] in ("written", "unchanged"):
        ledger.append_applied(home, {"action": "replay", "id": rule_id, "file": path, "line": line,
                                     "backup": written["backup"], "result": written["result"],
                                     "tracked": info["tracked"], "remote_visibility": info["visibility"]})
    return out


def replay(home: str, cfg: Optional[dict], confirm_tracked: bool, dry_run: bool) -> List[dict]:
    """Re-insert the stored line of every accepted rule that has gone missing from its file."""
    applied = ledger.read_applied(home)
    results = []
    for rule_id, state in ledger.current_states(ledger.read_proposals(home)).items():
        if state["decided"].get("status") != "accepted":
            continue
        entry = _last_written(applied, rule_id, ("apply", "replay"))
        if entry is None or not isinstance(entry.get("line"), str):
            continue
        results.append(_replay_one(rule_id, entry, home, cfg, confirm_tracked, dry_run))
    return results
