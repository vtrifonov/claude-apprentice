"""Apply-side helpers."""
from __future__ import annotations

import contextlib
import difflib
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time

from apprentice import config, gitinfo, ledger, scrub, worktrees

if False:  # annotations only
    from typing import Callable, Iterator, List, Optional, Tuple

START = "<!-- apprentice:start -->"
END = "<!-- apprentice:end -->"
HEADING = "## Learned from sessions"
LOCK_DEADLINE = 10.0
LOCK_POLL = 0.05
BACKUP_KEEP = 20
SLUG_MAX_BYTES = 200
# technical prefixes that look like ticket keys (UTF-8, SHA-256, RFC-3339, ...) and are not
TICKET_ALLOW = frozenset(("UTF", "SHA", "ISO", "HTTP", "TLS", "SSL", "ES", "RFC", "CVE", "IPV", "X", "AES", "RSA",
                          "MD"))
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[^/\s]+")

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


def _same_stat(real: str, st) -> bool:
    try:
        now = os.stat(real)
    except OSError:
        return False
    return (now.st_ino, now.st_mtime_ns, now.st_size) == (st.st_ino, st.st_mtime_ns, st.st_size)


def _same(real: str, data: bytes, st) -> bool:
    if not _same_stat(real, st):
        return False
    try:
        with open(real, "rb") as fh:
            return fh.read() == data
    except OSError:
        return False


def _makedirs_private(path: str) -> None:
    """os.makedirs, with every directory it creates (not only the leaf) set to 0700."""
    missing = []  # type: List[str]
    current = os.path.abspath(path)
    while not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            continue
        os.chmod(directory, 0o700)


def _backup_dir(home: str, real: str) -> str:
    slug = real.replace("/", "__")
    if len(slug.encode("utf-8", "surrogateescape")) > SLUG_MAX_BYTES:
        digest = hashlib.sha1(real.encode("utf-8", "surrogateescape")).hexdigest()[:12]
        slug = os.path.basename(real) + "__" + digest
    return os.path.join(home, "backups", slug)


def _prune_backups(directory: str) -> None:
    """Keep the newest BACKUP_KEEP backups of one target."""
    try:
        names = os.listdir(directory)
        stamped = sorted((os.stat(os.path.join(directory, name)).st_mtime_ns, name) for name in names)
    except OSError:
        return
    for _mtime, name in stamped[:max(0, len(stamped) - BACKUP_KEEP)]:
        _unlink(os.path.join(directory, name))


def _backup(home: str, real: str, data: bytes) -> str:
    directory = _backup_dir(home, real)
    _makedirs_private(directory)
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
    if st.st_nlink > 1:
        # os.replace would give the file a new inode and silently cut it off from its other names
        return {"result": "skipped:hardlinked", "backup": None, "diff": None}
    for _attempt in range(2):
        backup, tmp = None, None  # type: Optional[str], Optional[str]
        try:
            data, st = _read_target(real)
            content = data.decode("utf-8", "surrogateescape")
            new, result = transform(content)
            if result in ("unchanged", "edited", "moved") or result.startswith("skipped:") or new == content:
                return {"result": result, "backup": None, "diff": None}
            if dry_run:
                diff = "".join(difflib.unified_diff(content.splitlines(True), new.splitlines(True), real, real))
                return {"result": result, "backup": None, "diff": diff}
            backup = _backup(home, real, data)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix=".apprentice-tmp-")
            try:
                os.write(fd, new.encode("utf-8", "surrogateescape"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
            # the full compare, then a stat-only re-check right before the swap; a save landing between that
            # re-check and os.replace is still lost (the window is inherent to replace-based writes)
            if not _same(real, data, st) or not _same_stat(real, st):
                # someone edited the file since we read it: drop this attempt (and its backup) and redo it
                _unlink(tmp)
                _unlink(backup)
                continue
            os.replace(tmp, real)
        except OSError:
            _unlink(tmp)
            _unlink(backup)
            return {"result": "skipped:io-error", "backup": None, "diff": None}
        except BaseException:
            _unlink(tmp)
            raise
        _prune_backups(os.path.dirname(backup))
        return {"result": result, "backup": backup, "diff": None}
    return {"result": "skipped:concurrent-edit", "backup": None, "diff": None}


@contextlib.contextmanager
def apply_lock(home: str) -> Iterator[bool]:
    """Hold <home>/state/apply.lock; yields False when it could not be taken within LOCK_DEADLINE."""
    lock = os.path.join(home, "state", "apply.lock")
    _makedirs_private(os.path.dirname(lock))
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _acquire(fd):
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def write_target(path: str, transform: Callable[[str], Tuple[str, str]], home: str, dry_run: bool = False) -> dict:
    """Rewrite `path` (through symlinks) with `transform`, serialized on <home>/state/apply.lock.

    The target is backed up first, written to a temp file beside it, and only swapped in with os.replace when
    the file still matches what was read; one concurrent edit is retried, a second refuses. A hard-linked target
    is refused. The replace keeps the mode bits but not extended attributes or ACLs (copying those has no
    portable stdlib call on macOS). A dry run takes no lock and creates nothing: a stale read is fine for a
    preview.
    """
    if dry_run:
        return _write_locked(path, transform, home, True)
    with apply_lock(home) as held:
        if not held:
            return {"result": "skipped:lock-timeout", "backup": None, "diff": None}
        return _write_locked(path, transform, home, False)


def _record_apply(home: str, rule_id: str, outcome: dict) -> None:
    """The one applied.jsonl row of an apply attempt, whatever its result (refusals included)."""
    ledger.append_applied(home, {"action": "apply", "id": rule_id, "file": outcome.get("file"),
                                 "line": outcome.get("line"), "backup": outcome.get("backup"),
                                 "result": outcome["result"], "tracked": bool(outcome.get("tracked")),
                                 "remote_visibility": outcome.get("remote_visibility")})


def apply_rule_to_file(path: str, rule_id: str, text: str, n: int, band: str, date: str, home: str,
                       dry_run: bool = False, cfg: Optional[dict] = None, info: Optional[dict] = None,
                       record: bool = True) -> dict:
    clean = scrub.scrub(text, cfg if cfg is not None else config.load_config(home))[0] or ""
    line = render_line(rule_id, clean, n, band, date)
    out = write_target(path, lambda content: upsert_rule(content, line), home, dry_run=dry_run)
    out["line"] = line
    if record and not dry_run:
        _record_apply(home, rule_id, _outcome(out["result"], path, info, out))
    return out


def _find_repo_root(path: str) -> Optional[str]:
    directory = os.path.dirname(path)
    for _ in range(gitinfo._MAX_LEVELS + 1):
        if os.path.lexists(os.path.join(directory, ".git")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent
    return None


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
    return any(worktrees.is_exclude_line(line) for line in (text or "").splitlines())


def _git_info(real: str, timeout: float, cache: Optional[dict]) -> dict:
    """The git part of `inspect_target`, memoized in `cache` (one per pack build): by realpath, and the
    `gh repo view` answer by remote, so a build asks gh once per distinct remote."""
    by_path = cache.setdefault("inspect", {}) if cache is not None else {}
    if real in by_path:
        return dict(by_path[real])
    root = _find_repo_root(real)
    info = {"tracked": False, "root": None, "remote": None, "visibility": None, "excluded": None}
    if root is not None:
        info["tracked"] = True
        info["root"] = root
        info["remote"] = gitinfo.origin(root)
        by_remote = cache.setdefault("visibility", {}) if cache is not None else {}
        if info["remote"] not in by_remote:
            by_remote[info["remote"]] = _visibility(info["remote"], timeout)
        info["visibility"] = by_remote[info["remote"]]
        info["excluded"] = _excluded(root, real)
    by_path[real] = dict(info)
    return info


def inspect_target(path: str, cfg: dict, timeout: float = 5.0, cache: Optional[dict] = None) -> dict:
    """Describe where a target file lives.

    `tracked` here means the file sits inside a git work tree (a `.git` is found above its realpath), the
    "tracked in <root>" sense used for skills. It is not `gitinfo.is_tracked`, which asks whether a file is in
    the index and is what decides CLAUDE.local.md. Public-mirror skills are also reported as tracked.
    `cache` is a dict owned by the caller for one run; never share it across processes.
    """
    real = os.path.realpath(path)
    git = _git_info(real, timeout, cache)
    info = {"tracked": git["tracked"], "root": git["root"], "remote": git["remote"],
            "visibility": git["visibility"], "public_mirror": False, "excluded": git["excluded"]}
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
    for match in scrub.TICKET_RE.finditer(text):
        if match.group(0).split("-", 1)[0] not in TICKET_ALLOW:
            return "sensitive-text:ticket"
    if scrub.UUID_RE.search(text):
        return "sensitive-text:uuid"
    if _HOME_PATH_RE.search(text):
        return "sensitive-text:home-path"
    for repo in repos:
        names = [repo, repo.rsplit("/", 1)[-1]]
        owner = repo.split("/", 1)[0] if "/" in repo else ""
        if len(owner) >= 3:
            # usually the employer's name: the likeliest leak into a public skill
            names.append(owner)
        for name in names:
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
    if os.path.basename(os.path.realpath(path)) != "SKILL.md":
        return None, "skipped:bad-target"
    if scoping.is_readonly_skill(path):
        return global_path, None
    return path, None


def _outcome(result: str, path: Optional[str] = None, info: Optional[dict] = None, out: Optional[dict] = None) -> dict:
    out = out or {}
    return {"result": result, "file": path, "line": out.get("line"), "backup": out.get("backup"),
            "tracked": bool(info and info.get("tracked")),
            "remote_visibility": info.get("visibility") if info else None, "diff": out.get("diff"),
            "root": info.get("root") if info else None, "remote": info.get("remote") if info else None}


def pack_facts(args, home: str, merged_ids: Optional[List[str]]) -> Tuple[int, str, List[str]]:
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


def apply_candidate(args, home: str, cfg: dict, real_home: str, merged_ids: Optional[List[str]] = None,
                    allow_no_pack: bool = False) -> dict:
    """Write one accepted rule to its global or skill target and ledger it (every outcome, refusals included).

    `allow_no_pack` lifts the tracked-target pack requirement; only the tone pointer passes it (tone.py
    --install-pointer), since that rule has no pack. `--confirm-tracked` is still required."""
    outcome = _apply_candidate(args, home, cfg, real_home, merged_ids, allow_no_pack)
    if not args.dry_run:
        _record_apply(home, args.id, outcome)
    return outcome


def _apply_candidate(args, home: str, cfg: dict, real_home: str, merged_ids: Optional[List[str]],
                     allow_no_pack: bool) -> dict:
    path, error = resolve_target(args.scope, args.target, getattr(args, "skill", None), cfg, real_home)
    if error:
        return _outcome(error)
    info = inspect_target(path, cfg)
    # A dry run writes nothing, and its diff is what the user must see before confirming a tracked target.
    if info["tracked"] and not args.confirm_tracked and not args.dry_run:
        return _outcome("skipped:git-tracked", path, info)
    if info["tracked"] and not args.pack and not args.dry_run and not allow_no_pack:
        return _outcome("skipped:no-pack", path, info)
    real = os.path.realpath(path)
    if os.path.basename(real) == "CLAUDE.md" and info["root"] is not None:
        global_real = os.path.realpath(os.path.join(real_home, ".claude", "CLAUDE.md"))
        if info["root"] != _find_repo_root(global_real):
            return _outcome("skipped:repo-claude-md", path, info)
    n, band, repos = pack_facts(args, home, merged_ids)
    flagged = sensitive_check(args.text, info["tracked"], repos, cfg)
    if flagged:
        return _outcome("skipped:" + flagged, path, info)
    out = apply_rule_to_file(path, args.id, args.text, n, band, config.utc_now_iso()[:10], home,
                             dry_run=bool(args.dry_run), cfg=cfg, info=info, record=False)
    if not args.dry_run and out["result"] in ("written", "unchanged"):
        parsed = split_rule_line(out["line"])
        ledger.record_decision(home, args.pack, args.id, "accepted", text=parsed[0] if parsed else None,
                               scope=args.scope, target=path, merged_ids=merged_ids,
                               profile=config.current_profile(os.environ), cfg=cfg)
    return _outcome(out["result"], path, info, out)


def _repo_main(target: str, repo: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(main checkout, error) for a repo-scope target: `<main>/CLAUDE.local.md` of the repo named `repo`."""
    if os.path.basename(target) != worktrees.LOCAL_NAME:
        return None, "skipped:bad-target"
    main = os.path.realpath(os.path.dirname(target))
    found = gitinfo.main_checkout(main)
    if found is None:
        return None, "skipped:no-main-checkout"
    if found != main or (repo is not None and gitinfo.origin(main) != repo):
        return None, "skipped:bad-target"
    return main, None


def _repo_index(main: str) -> Optional[str]:
    """Refuse a CLAUDE.local.md that git tracks (or may track): its rules would be committed."""
    tracked = gitinfo.is_tracked(main, worktrees.LOCAL_NAME)
    if tracked is True:
        return "skipped:git-tracked-local"
    if tracked is None:
        return "skipped:git-index-unknown"
    return None


def _create_local(path: str) -> bool:
    if os.path.lexists(path):
        return True
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        return True
    except OSError:
        return False
    return True


def _repo_dry_run(local: str, rule_id: str, text: str, n: int, band: str, date: str, home: str,
                  cfg: Optional[dict]) -> dict:
    if os.path.lexists(local):
        return apply_rule_to_file(local, rule_id, text, n, band, date, home, dry_run=True, cfg=cfg)
    clean = scrub.scrub(text, cfg if cfg is not None else config.load_config(home))[0] or ""
    line = render_line(rule_id, clean, n, band, date)
    new, result = upsert_rule("", line)
    diff = "".join(difflib.unified_diff([], new.splitlines(True), local, local))
    return {"result": result, "backup": None, "diff": diff, "line": line}


def apply_repo(target: str, repo: str, rule_id: str, text: str, n: int, band: str, date: str, home: str,
               cfg: Optional[dict], dry_run: bool, merged_ids: Optional[List[str]] = None,
               pack: Optional[str] = None) -> dict:
    """Write a rule to the main checkout's git-ignored CLAUDE.local.md, link it into the worktrees, ledger it."""
    outcome, main = _apply_repo(target, repo, rule_id, text, n, band, date, home, cfg, dry_run)
    if dry_run:
        return outcome
    _record_apply(home, rule_id, outcome)
    if main is None or outcome["result"] not in ("written", "unchanged"):
        return outcome
    local = outcome["file"]
    outcome["links"] = worktrees.fan_out(main)
    for link in outcome["links"]:
        ledger.append_applied(home, {"action": "link", "id": rule_id,
                                     "file": os.path.join(link["worktree"], worktrees.LOCAL_NAME),
                                     "target": local, "result": link["result"]})
    parsed = split_rule_line(outcome["line"])
    ledger.record_decision(home, pack, rule_id, "accepted", text=parsed[0] if parsed else None,
                           scope="repo", target=local, merged_ids=merged_ids,
                           profile=config.current_profile(os.environ), cfg=cfg)
    return outcome


def _symlinked_local(local: str) -> Optional[str]:
    """A symlinked CLAUDE.local.md would be written through to a file the index check never saw."""
    return "skipped:local-is-symlink" if os.path.islink(local) else None


def _prepare_local(main: str, local: str, home: str) -> Optional[str]:
    """Under the apply lock: the exclude line first, then an empty CLAUDE.local.md when there is none."""
    with apply_lock(home) as held:
        if not held:
            return "skipped:lock-timeout"
        if worktrees.ensure_exclude_line(main) == "failed":
            return "skipped:exclude-failed"
        if not _create_local(local):
            return "skipped:create-failed"
    return None


def _apply_repo(target: str, repo: str, rule_id: str, text: str, n: int, band: str, date: str, home: str,
                cfg: Optional[dict], dry_run: bool) -> Tuple[dict, Optional[str]]:
    """(outcome, main checkout once the rule reached the file, else None); no ledger rows."""
    main, error = _repo_main(target, repo)
    if error:
        return _outcome(error, target), None
    error = _repo_index(main)
    if error:
        out = _outcome(error, target)
        out["message"] = "git may track CLAUDE.local.md, so nothing was written; rescope this rule to global"
        return out, None
    local = os.path.join(main, worktrees.LOCAL_NAME)
    error = _symlinked_local(local)
    if error:
        return _outcome(error, local), None
    flagged = sensitive_check(text, False, [repo], cfg)
    if flagged:
        return _outcome("skipped:" + flagged, local), None
    if dry_run:
        out = _repo_dry_run(local, rule_id, text, n, band, date, home, cfg)
        return _outcome(out["result"], local, None, out), None
    error = _prepare_local(main, local, home)
    if error:
        return _outcome(error, local), None
    out = apply_rule_to_file(local, rule_id, text, n, band, date, home, dry_run=False, cfg=cfg, record=False)
    outcome = _outcome(out["result"], local, None, out)
    outcome["links"] = []
    return outcome, main


def _last_written(rows: List[dict], rule_id: str, actions: Tuple[str, ...]) -> Optional[dict]:
    for entry in reversed(rows):
        if (entry.get("id") == rule_id and entry.get("action") in actions and isinstance(entry.get("file"), str)
                and entry.get("result") in ("written", "unchanged")):
            return entry
    return None


def _files_since_undo(rows: List[dict], rule_id: str) -> List[str]:
    """Every distinct file a `written` apply or replay row put the rule in since its last undo, oldest first."""
    files = []  # type: List[str]
    for entry in rows:
        if entry.get("id") != rule_id or not isinstance(entry.get("file"), str):
            continue
        if entry.get("action") == "undo":
            files = []
        elif entry.get("action") in ("apply", "replay") and entry.get("result") == "written":
            if entry["file"] not in files:
                files.append(entry["file"])
    return files


def undo(rule_id: str, home: str, cfg: Optional[dict], dry_run: bool = False) -> dict:
    """Remove the rule from every file it was written to since the last undo (a rescope leaves several).

    Returns the last file's outcome with `results` holding one outcome per file.
    """
    files = _files_since_undo(ledger.read_applied(home), rule_id)
    if not files:
        return {"result": "skipped:not-applied", "file": None, "backup": None, "diff": None, "results": []}
    results = []
    for path in files:
        out = write_target(path, lambda content: remove_rule(content, rule_id), home, dry_run=dry_run)
        out["file"] = path
        results.append(out)
        if not dry_run and out["result"] in ("removed", "unchanged"):
            ledger.append_applied(home, {"action": "undo", "id": rule_id, "file": path,
                                         "backup": out["backup"], "result": out["result"]})
    done = [out for out in results if out["result"] in ("removed", "unchanged")]
    if not dry_run and done:
        ledger.record_decision(home, None, rule_id, "undone", target=done[-1]["file"],
                               profile=config.current_profile(os.environ))
    return dict(results[-1], results=results)


def _repo_replay_steps(path: str, home: str, dry_run: bool) -> Tuple[Optional[str], Optional[str]]:
    """(main, error) for a stored repo target: main checkout, index, symlink; then, for a real run, the exclude
    line and (when the file is gone) an empty CLAUDE.local.md, both under the apply lock, before any write."""
    main, error = _repo_main(path, None)
    if error is not None:
        return None, "skipped:bad-target"
    error = _repo_index(main) or _symlinked_local(path)
    if error is None and not dry_run:
        error = _prepare_local(main, path, home)
    return main, error


def _replay_transform(line: str) -> Callable[[str], Tuple[str, str]]:
    """Insert `line` only when its id is nowhere in the file; a present line is never rewritten."""
    text, rule_id = split_rule_line(line) or ("", "")

    def transform(content: str) -> Tuple[str, str]:
        lines = content.splitlines(True)
        state, start, end = _section(lines)
        if state == "ok":
            index, existing = _find_rule(lines, start, end, rule_id)
            if index >= 0:
                # the user's hand edit wins over the stored line
                return content, ("unchanged" if existing == text else "edited")
        if state != "malformed" and "<!-- apprentice:%s " % rule_id in content:
            return content, "moved"
        return upsert_rule(content, line)
    return transform


def _replay_one(rule_id: str, entry: dict, home: str, cfg: Optional[dict], confirm_tracked: bool,
                dry_run: bool) -> dict:
    path, line = entry["file"], entry["line"]
    transform = _replay_transform(line)
    repo_target = os.path.basename(path) == worktrees.LOCAL_NAME
    if repo_target and not os.path.lexists(path):
        # `git clean -fdx` in main removes the ignored file: recreate it (and the exclude line) and replay
        return _replay_missing_local(rule_id, path, line, home, dry_run)
    probe = write_target(path, transform, home, dry_run=True)
    out = {"id": rule_id, "file": path, "result": probe["result"], "backup": None, "diff": None}
    if repo_target and not probe["result"].startswith("skipped:"):
        _main, error = _repo_replay_steps(path, home, dry_run)
        if error:
            out["result"] = error
            return out
    if probe["result"] in ("edited", "moved") and not dry_run:
        ledger.append_applied(home, {"action": "replay", "id": rule_id, "file": path, "line": line,
                                     "backup": None, "result": probe["result"]})
    if probe["result"] != "written":
        return out
    if repo_target:
        info = {"tracked": False, "visibility": None}
    else:
        info = inspect_target(path, cfg or {})
        if info["tracked"] and not confirm_tracked:
            out["result"] = "skipped:git-tracked"
            return out
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


def _replay_missing_local(rule_id: str, path: str, line: str, home: str, dry_run: bool) -> dict:
    """Replay into a repo CLAUDE.local.md that is gone: the repo steps create it, then the line goes in.
    The worktree links are not fanned out again (SessionStart restores them)."""
    out = {"id": rule_id, "file": path, "result": "skipped:missing", "backup": None, "diff": None}
    _main, error = _repo_replay_steps(path, home, dry_run)
    if error:
        out["result"] = error
        return out
    if dry_run:
        new, result = upsert_rule("", line)
        out.update(result=result, diff="".join(difflib.unified_diff([], new.splitlines(True), path, path)))
        return out
    written = write_target(path, _replay_transform(line), home)
    out.update(result=written["result"], backup=written["backup"])
    if written["result"] in ("written", "unchanged"):
        ledger.append_applied(home, {"action": "replay", "id": rule_id, "file": path, "line": line,
                                     "backup": written["backup"], "result": written["result"],
                                     "tracked": False, "remote_visibility": None})
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
