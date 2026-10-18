"""Two-signal rule and confidence bands for candidates (ARCH-9: a second signal must be an independent event)."""
from __future__ import annotations

import time
from collections import Counter

from apprentice import config, filters

if False:  # annotations only
    from typing import List, Optional, Tuple

BANDS = ("low", "medium", "high")
HIGH_N = 5
HIGH_SESSIONS = 3
HIGH_REPOS = 2
HIGH_CONSISTENCY = 0.9
MEDIUM_N = 3
STRONG_MEDIUM_N = 2
SPLIT_PURITY_MIN = 0.9
_OWN_TAGS = ("correction", "standing")


def _event(item: dict) -> dict:
    """The event behind a cluster row or a group pair; a bare ask row stands in for its event."""
    event = item.get("event")
    if event is not None:
        return event
    return {"id": item.get("event_id"), "kind": "ask", "ts": item.get("ts"),
            "session_id": item.get("session_id")}


def _has_anchor(event: dict) -> bool:
    """Ask rows always follow an assistant turn; a withheld prompt drops the key but was selected with one."""
    if event.get("kind") == "ask" or "prev_assistant" not in event:
        return True
    return bool(event["prev_assistant"])


def independent(e1: dict, e2: dict) -> bool:
    """`e2` is the later event. Same event, a duplicate, or the same session-turn is one signal, not two."""
    id1, id2 = e1.get("id"), e2.get("id")
    if not id1 or not id2 or id1 == id2:
        return False
    if e1.get("duplicate_of") == id2 or e2.get("duplicate_of") == id1:
        return False
    if e1.get("session_id") and e1.get("session_id") == e2.get("session_id") and e1.get("ts") == e2.get("ts"):
        return False
    return _has_anchor(e2)


def _independent_pairs(events: List[dict]):
    ordered = sorted(events, key=lambda event: (config.ts_epoch(event.get("ts")) or 0.0, event.get("id") or ""))
    for index, later in enumerate(ordered):
        for earlier in ordered[:index]:
            if independent(earlier, later):
                yield earlier, later


def _local_day(ts) -> Optional[str]:
    epoch = config.ts_epoch(ts)
    return None if epoch is None else time.strftime("%Y-%m-%d", time.localtime(epoch))


def _own_tag(event: dict) -> bool:
    """A correction or standing tag that survives removing the strong-marker phrase from the text."""
    text = event.get("text")
    if not text:
        return False
    stripped = filters._STRONG_RE.sub(" ", text.lower())
    return any(tag in _OWN_TAGS for tag in filters.classify(stripped)["tags"])


def two_signal(kind: str, items: List[dict], links: List[str]) -> Optional[str]:
    events = [_event(item) for item in items]
    for earlier, later in _independent_pairs(events):
        day_a, day_b = _local_day(earlier.get("ts")), _local_day(later.get("ts"))
        sessions = earlier.get("session_id"), later.get("session_id")
        if day_a and day_b and day_a != day_b and all(sessions) and sessions[0] != sessions[1]:
            return "different-days"
    for earlier, later in _independent_pairs(events):
        for event in (earlier, later):
            if event.get("strong_marker") and _own_tag(event):
                return "strong-marker"
    if links:
        return "ask-plus-correction"
    return None


def _drop(value: str) -> str:
    return BANDS[max(BANDS.index(value) - 1, 0)]


def band(n: int, sessions: int, repos: int, consistency: float, reversal_30d: bool, has_reversal: bool,
         path: Optional[str], split_purity: Optional[float], majority_band: Optional[str]) -> Tuple[str, dict]:
    if (n >= HIGH_N and (repos >= HIGH_REPOS or sessions >= HIGH_SESSIONS)
            and consistency >= HIGH_CONSISTENCY and not reversal_30d):
        value = "high"
    elif n >= MEDIUM_N or (n >= STRONG_MEDIUM_N and path == "strong-marker"):
        value = "medium"
    else:
        value = "low"
    if has_reversal:
        value = _drop(value)
    if split_purity is not None and split_purity >= SPLIT_PURITY_MIN and majority_band is not None:
        if BANDS.index(majority_band) > BANDS.index(value):
            value = majority_band
    inputs = {"n": n, "sessions": sessions, "repos": repos, "consistency": consistency,
              "reversal_30d": reversal_30d, "has_reversal": has_reversal, "path": path,
              "split_purity": split_purity, "majority_band": majority_band}
    return value, inputs


def majority_band(maj_rows: List[dict], path: Optional[str]) -> str:
    """The band the split's majority side would earn alone."""
    sessions = len({row.get("session_id") for row in maj_rows if row.get("session_id")})
    repos = len({row.get("repo") for row in maj_rows if row.get("repo")})
    return band(len(maj_rows), sessions, repos, 1.0, False, False, path, None, None)[0]


def assessed(cluster: dict) -> bool:
    """Ask clusters have an answer distribution to judge; correction groups do not."""
    return "answers" in cluster


def consistency(cluster: dict, split: Optional[dict]) -> float:
    if not assessed(cluster):
        return 1.0
    if split:
        agree = split["dep_side"][0] + split["maj_side"][0]
        total = split["dep_side"][1] + split["maj_side"][1]
        return agree / total if total else 0.0
    counts = Counter(cluster["answers"])
    total = sum(counts.values())
    return max(counts.values()) / total if total else 0.0
