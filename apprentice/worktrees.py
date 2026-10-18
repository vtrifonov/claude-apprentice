"""Link a linked worktree's CLAUDE.local.md to the main checkout's copy, only when git already ignores it."""
from __future__ import annotations

import os

from apprentice import gitinfo

if False:  # annotations only: importing typing costs every hook start
    from typing import List, Optional, Tuple

LOCAL_NAME = "CLAUDE.local.md"
CONTENT_CAP = 8192


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(CONTENT_CAP)
    except OSError:
        return None


def is_exclude_line(line: str) -> bool:
    """An info/exclude line that ignores the checkout-root CLAUDE.local.md (with or without the leading slash)."""
    return line.strip() in (LOCAL_NAME, "/" + LOCAL_NAME)


def _excluded(root: str) -> bool:
    path = gitinfo.exclude_path(root)
    text = _read_text(path) if path else None
    if text is None:
        return False
    return any(is_exclude_line(line) for line in text.splitlines())


def ensure_exclude_line(main: str) -> str:
    """Make git ignore CLAUDE.local.md in the checkout at `main`: "added", "present" or "failed".

    Apply calls it under its apply lock, so two applies never both append the line.
    """
    path = gitinfo.exclude_path(main)
    if path is None:
        return "failed"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existing = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="surrogateescape") as fh:
                existing = fh.read()
        if any(is_exclude_line(line) for line in existing.splitlines()):
            return "present"
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with open(path, "a", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(prefix + LOCAL_NAME + "\n")
    except (OSError, ValueError):
        return "failed"
    return "added"


def fan_out(main: str) -> List[dict]:
    """Symlink every linked worktree's CLAUDE.local.md to the one in `main`, leaving anything already there."""
    source = os.path.abspath(os.path.join(main, LOCAL_NAME))
    results = []
    for wt in gitinfo.worktrees(main):
        dest = os.path.join(wt, LOCAL_NAME)
        if os.path.lexists(dest):
            if not os.path.islink(dest):
                result = "skipped-file-exists"
            elif os.path.realpath(dest) == os.path.realpath(source):
                result = "present"
            else:
                result = "skipped-foreign-symlink"
        else:
            try:
                os.symlink(source, dest)
                result = "linked"
            except OSError:
                result = "skipped-file-exists"
        results.append({"worktree": wt, "result": result})
    return results


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
