"""Scope helpers for skills."""
from __future__ import annotations

import glob
import os

if False:  # annotations only
    from typing import Optional


def find_skill_dir(name: str, real_home: str) -> Optional[str]:
    claude = os.path.join(real_home, ".claude")
    candidates = [os.path.join(claude, "skills", name), os.path.join(claude, "local-skills", name)]
    candidates.extend(sorted(glob.glob(os.path.join(glob.escape(claude), "skills", "synced", "*", name))))
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "SKILL.md")):
            return candidate
    return None


def is_readonly_skill(path: str) -> bool:
    parts = os.path.realpath(path).split(os.sep)
    if "synced" in parts:
        return True
    for index in range(1, len(parts)):
        if parts[index] == "plugins" and parts[index - 1].startswith(".claude"):
            return True
    return False
