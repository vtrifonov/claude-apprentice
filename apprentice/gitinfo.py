"""Subprocess-free git helpers: everything is read from plain files, failures yield None or []."""
import os
import re
from typing import Dict, List, Optional, Tuple

_MAX_LEVELS = 12
_SECTION_RE = re.compile(r'^\[\s*([^\s\]"]+)(?:\s+"([^"]*)")?\s*\]')
_HTTPS_RE = re.compile(r"^https?://[^/]+/([^/]+)/(.+?)(?:\.git)?/?$")
_SSH_URL_RE = re.compile(r"^ssh://[^/]+/([^/]+)/(.+?)(?:\.git)?/?$")
_SCP_RE = re.compile(r"^[^@/\s]+@[^:/\s]+:([^/]+)/(.+?)(?:\.git)?/?$")


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _resolve(base: str, path: str) -> str:
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return os.path.normpath(path)


def _parse_config(path: str) -> Dict[Tuple[str, str], Dict[str, str]]:
    text = _read(path)
    sections = {}  # type: Dict[Tuple[str, str], Dict[str, str]]
    if text is None:
        return sections
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        match = _SECTION_RE.match(line)
        if match:
            current = sections.setdefault((match.group(1).lower(), match.group(2) or ""), {})
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        current[key.strip().lower()] = value
    return sections


def find_git(cwd: str) -> Optional[str]:
    try:
        d = os.path.abspath(cwd)
        for _ in range(_MAX_LEVELS + 1):
            if os.path.lexists(os.path.join(d, ".git")):
                return os.path.realpath(d)
            parent = os.path.dirname(d)
            if parent == d:
                return None
            d = parent
    except (OSError, ValueError):
        pass
    return None


def git_dirs(d: str) -> Optional[Tuple[str, str]]:
    dot_git = os.path.join(d, ".git")
    if os.path.isdir(dot_git):
        return dot_git, dot_git
    text = _read(dot_git)
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("gitdir:"):
            target = line[len("gitdir:"):].strip()
            if not target:
                return None
            gitdir = _resolve(d, target)
            common = _read(os.path.join(gitdir, "commondir"))
            if common is not None and common.strip():
                return gitdir, _resolve(gitdir, common.strip())
            return gitdir, gitdir
    return None


def main_checkout(d: str) -> Optional[str]:
    dirs = git_dirs(d)
    if dirs is None:
        return None
    gitdir, commondir = dirs
    # A separate git dir (or submodule) is its own main, even when its store is named .git.
    if os.path.isfile(os.path.join(d, ".git")) and gitdir == commondir:
        return find_git(d)
    if os.path.basename(commondir) == ".git":
        return os.path.realpath(os.path.dirname(commondir))
    worktree = _parse_config(os.path.join(commondir, "config")).get(("core", ""), {}).get("worktree")
    if worktree:
        return os.path.realpath(_resolve(commondir, worktree))
    return None


def exclude_path(d: str) -> Optional[str]:
    dirs = git_dirs(d)
    if dirs is None:
        return None
    return os.path.join(dirs[1], "info", "exclude")


def worktrees(d: str) -> List[str]:
    dirs = git_dirs(d)
    if dirs is None:
        return []
    root = os.path.join(dirs[1], "worktrees")
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for name in names:
        text = _read(os.path.join(root, name, "gitdir"))
        if not text or not text.strip():
            continue
        wt = os.path.dirname(_resolve(os.path.join(root, name), text.strip()))
        if os.path.isdir(wt):
            out.append(os.path.realpath(wt))
    return sorted(out)


def parse_origin_url(url: str) -> Optional[str]:
    url = (url or "").strip()
    for pattern in (_HTTPS_RE, _SSH_URL_RE, _SCP_RE):
        match = pattern.match(url)
        if match:
            return "%s/%s" % (match.group(1), match.group(2))
    return None


def origin(d: str) -> Optional[str]:
    dirs = git_dirs(d)
    if dirs is None:
        return None
    url = _parse_config(os.path.join(dirs[1], "config")).get(("remote", "origin"), {}).get("url")
    return parse_origin_url(url) if url else None


def branch(d: str) -> Optional[str]:
    dirs = git_dirs(d)
    if dirs is None:
        return None
    text = _read(os.path.join(dirs[0], "HEAD"))
    if text is None:
        return None
    prefix = "ref: refs/heads/"
    line = text.strip()
    if line.startswith(prefix) and len(line) > len(prefix):
        return line[len(prefix):]
    return None


def repo_context(cwd: str) -> dict:
    root = find_git(cwd)
    if root is None:
        return {"root": None, "main": None, "repo": None, "branch": None}
    return {"root": root, "main": main_checkout(root), "repo": origin(root), "branch": branch(root)}


def resolve_main_for_cwd(cwd: str, worktree_suffix: str) -> Optional[str]:
    try:
        cwd = os.path.abspath(cwd)
        if os.path.isdir(cwd):
            root = find_git(cwd)
            return main_checkout(root) if root else None
        child = cwd
        while True:
            parent = os.path.dirname(child)
            if parent == child:
                break
            name = os.path.basename(parent)
            if worktree_suffix and name.endswith(worktree_suffix) and len(name) > len(worktree_suffix):
                candidate = os.path.join(os.path.dirname(parent), name[: -len(worktree_suffix)])
                if os.path.isdir(candidate):
                    return os.path.realpath(candidate)
            child = parent
        ancestor = os.path.dirname(cwd)
        while not os.path.isdir(ancestor):
            nxt = os.path.dirname(ancestor)
            if nxt == ancestor:
                return None
            ancestor = nxt
        root = find_git(ancestor)
        return main_checkout(root) if root else None
    except (OSError, ValueError):
        return None
