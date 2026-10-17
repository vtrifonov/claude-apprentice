"""Link a linked worktree's CLAUDE.local.md to the main checkout's copy, only when git already ignores it."""
from __future__ import annotations

import os

from apprentice import gitinfo

if False:  # annotations only: importing typing costs every hook start
    from typing import Optional, Tuple

LOCAL_NAME = "CLAUDE.local.md"
CONTENT_CAP = 8192


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(CONTENT_CAP)
    except OSError:
        return None


def _excluded(root: str) -> bool:
    path = gitinfo.exclude_path(root)
    text = _read_text(path) if path else None
    if text is None:
        return False
    return any(line.strip() == LOCAL_NAME for line in text.splitlines())


def is_worktree(cwd: str) -> bool:
    root = gitinfo.find_git(cwd)
    if root is None:
        return False
    dirs = gitinfo.git_dirs(root)
    return dirs is not None and os.path.isfile(os.path.join(root, ".git")) and dirs[0] != dirs[1]


def ensure_link(cwd: str) -> Tuple[str, Optional[str]]:
    root = gitinfo.find_git(cwd)
    if root is None or not os.path.isfile(os.path.join(root, ".git")):
        return "not-a-repo", None
    main = gitinfo.main_checkout(root)
    if main is None:
        return "no-main-checkout", None
    if main == root:
        return "not-a-repo", None
    source = os.path.join(main, LOCAL_NAME)
    if not os.path.isfile(source):
        return "no-source", None
    target = os.path.join(root, LOCAL_NAME)
    if os.path.lexists(target):
        if os.path.islink(target) and os.path.realpath(target) == os.path.realpath(source):
            return "present", None
        return "skipped-file-exists", None
    if not _excluded(root):
        return "skipped-not-excluded", None
    content = _read_text(source)
    if content is None:
        return "no-source", None
    try:
        os.symlink(source, target)
    except OSError:
        return "skipped-file-exists", None
    return "created", content
