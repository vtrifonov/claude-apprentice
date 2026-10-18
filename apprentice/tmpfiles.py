"""Temp input files under <home>/state/tmp (distill side; the hook path never imports this).

The skill and the commands Write a file there and pass only its path to a script, so no typed text reaches a
command line. Each consumer deletes its file once read, and distill.py and tone.py sweep stale leftovers at
start: `state/tmp/` is shared by every account group, so nothing may outlive its use there. File names carry
the group tag the scripts print as `group=<tag>`."""
from __future__ import annotations

import os
import stat
import time

from apprentice import config

if False:  # annotations only
    from typing import Optional, Tuple

SWEEP_AGE = 3600.0


def tmp_root(home: str) -> str:
    return os.path.realpath(os.path.join(home, "state", "tmp"))


def _confined(home: str, path: str) -> Optional[str]:
    """The realpath of `path` when it lies under state/tmp, else None."""
    resolved = os.path.realpath(path)
    return resolved if resolved.startswith(tmp_root(home) + os.sep) else None


def read(home: str, path: str, flag: str) -> Tuple[Optional[str], Optional[str]]:
    """(content, None) for a readable file under state/tmp, else (None, error message)."""
    resolved = _confined(home, path)
    if resolved is None:
        return None, "%s must be under %s" % (flag, tmp_root(home))
    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            return fh.read(), None
    except (OSError, ValueError) as exc:
        return None, "cannot read %s: %s" % (flag, exc.__class__.__name__)


def discard(home: str, path: Optional[str]) -> None:
    """Delete a consumed temp file; a path outside state/tmp is never touched."""
    resolved = _confined(home, path) if path else None
    if resolved is None:
        return
    try:
        if stat.S_ISREG(os.lstat(resolved).st_mode):
            os.unlink(resolved)
    except OSError:
        pass


def sweep(home: str, max_age: float = SWEEP_AGE, now: Optional[float] = None) -> int:
    """Remove every regular file directly in state/tmp older than `max_age` seconds; return how many."""
    root = tmp_root(home)
    cutoff = (time.time() if now is None else now) - max_age
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    removed = 0
    for name in names:
        path = os.path.join(root, name)
        try:
            st = os.lstat(path)
            if stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
                os.unlink(path)
                removed += 1
        except OSError:
            continue
    return removed


def group_tag(group: Optional[str]) -> str:
    """The session-independent prefix of temp file names for an account group."""
    return (config.group_slug(group) if group else "") or "nogroup"
