"""Departure context for an event: the repo, branch and ticket the user was working in."""
from __future__ import annotations

from apprentice import gitinfo
from apprentice.scrub import TICKET_RE

if False:  # annotations only: importing typing costs every hook start
    from typing import Optional


def find_ticket(*texts: Optional[str]) -> Optional[str]:
    for text in texts:
        if not text:
            continue
        match = TICKET_RE.search(text)
        if match:
            return match.group(0)
    return None


def departure_context(cwd: Optional[str], *texts: Optional[str]) -> dict:
    ctx = gitinfo.repo_context(cwd) if cwd else {}
    branch = ctx.get("branch")
    return {
        "repo": ctx.get("repo"),
        "branch": branch,
        "ticket": find_ticket(branch, cwd, *texts),
    }
