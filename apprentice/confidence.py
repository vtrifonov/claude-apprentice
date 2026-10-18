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
    """Ask rows always follow an assistant turn; a withheld prompt says so in `anchored`; no key means no anchor."""
    if event.get("kind") == "ask":
        return True
    if "anchored" in event:
        return bool(event["anchored"])
    return bool(event.get("prev_assistant"))


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


def _local_day(ts) -> Optional[str]:
    epoch = config.ts_epoch(ts)
    return None if epoch is None else time.strftime("%Y-%m-%d", time.localtime(epoch))


def text_own_tag(text: Optional[str]) -> bool:
    """A correction or standing tag that survives removing the strong-marker phrase from the text."""
    if not text:
        return False
    stripped = filters._STRONG_RE.sub(" ", text.lower())
    return any(tag in _OWN_TAGS for tag in filters.classify(stripped)["tags"])


def own_strong(event: dict) -> bool:
    """The event's own strong-marker result, computed on its full text."""
    return bool(event.get("strong_marker")) and text_own_tag(event.get("text"))


def _own_tag(event: dict) -> bool:
    if not event.get("text") and "own_strong" in event:
        return bool(event["own_strong"])
    return text_own_tag(event.get("text"))


def _different_days(ordered: List[dict]) -> bool:
    days = [_local_day(event.get("ts")) for event in ordered]
    sessions = [event.get("session_id") for event in ordered]
    if len({day for day in days if day}) < 2 or len({session for session in sessions if session}) < 2:
        return False
    by_day = {}  # type: dict
    for index, day in enumerate(days):
        if day:
            by_day.setdefault(day, []).append(index)
    for later, day in enumerate(days):
        if not day or not sessions[later]:
            continue
        for other_day, indexes in by_day.items():
            if other_day == day:
                continue
            for earlier in indexes:
                if earlier >= later:
                    break
                if sessions[earlier] and sessions[earlier] != sessions[later] \
                        and independent(ordered[earlier], ordered[later]):
                    return True
    return False


def _strong_with_partner(ordered: List[dict]) -> bool:
    # spec 10.5: two independent occurrences, at least one strong; the partner needs no correction text of its own
    for index, event in enumerate(ordered):
        if not (event.get("strong_marker") and _own_tag(event)):
            continue
        for other_index, other in enumerate(ordered):
            if other_index == index:
                continue
            earlier, later = (other, event) if other_index < index else (event, other)
            if independent(earlier, later):
                return True
    return False


def two_signal(kind: str, items: List[dict], links: List[str]) -> Optional[str]:
    events = [_event(item) for item in items]
    ordered = sorted(events, key=lambda event: (config.ts_epoch(event.get("ts")) or 0.0, event.get("id") or ""))
    if _different_days(ordered):
        return "different-days"
    if _strong_with_partner(ordered):
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
