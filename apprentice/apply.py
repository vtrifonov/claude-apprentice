"""Apply-side helpers."""
from __future__ import annotations

import json
import os
import subprocess

from apprentice import gitinfo

if False:  # annotations only
    from typing import Optional


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
