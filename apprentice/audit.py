"""Audit a candidate rule against the file it would be written to."""
import os
import re
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

from apprentice import cluster, config, ledger

_NEGATION_RE = re.compile(r"\b(?:never|don'?t|no)\b")
_BULLET_RE = re.compile(r"^\s*-\s")
STEM_INHERIT_JACCARD = 0.5
TOKEN_INHERIT_JACCARD = 0.5
REJECTED_MIN_EVENTS = 3
SNOOZE_S = 14 * 86400.0


def document_frequency(texts: Iterable[str]) -> Dict[str, int]:
    """Number of texts containing each content token; a repeat inside one text counts once."""
    counts = {}  # type: Dict[str, int]
    for text in texts:
        for token in cluster.content_tokens(text):
            counts[token] = counts.get(token, 0) + 1
    return counts


def rarest_words(text: str, df: Mapping[str, int], k: int = 3) -> List[str]:
    tokens = cluster.content_tokens(text)
    return sorted(tokens, key=lambda token: (df.get(token, 0), token))[:k]


def _units(lines: List[str]) -> List[str]:
    """Every line, plus every bullet joined with its indented continuation lines, deduplicated."""
    units = []  # type: List[str]
    bullet = None  # type: Optional[List[str]]
    for line in lines + [""]:
        if bullet is not None and line.strip() and line[:1].isspace() and not _BULLET_RE.match(line):
            bullet.append(line.strip())
            continue
        if bullet is not None:
            units.append(" ".join(bullet))
            bullet = None
        if _BULLET_RE.match(line):
            bullet = [line.strip()]
        if line.strip():
            units.append(line.strip())
    seen = set()
    unique = []
    for unit in units:
        if unit not in seen:
            seen.add(unit)
            unique.append(unit)
    return unique


def _negated(text: str) -> bool:
    return bool(_NEGATION_RE.search(text.lower().replace("’", "'")))


def audit_target(summary_text: str, target_path: Optional[str], quote_allowed: bool,
                 df: Mapping[str, int]) -> str:
    if not target_path or not os.path.isfile(target_path):
        return "new"
    words = rarest_words(summary_text, df, 3)
    if not words:
        return "new"
    try:
        with open(target_path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return "new"
    summary_negated = _negated(summary_text)
    contradicting = []  # type: List[str]
    for unit in _units(lines):
        tokens = cluster.content_tokens(unit)
        hits = sum(1 for word in words if word in tokens)
        if hits == len(words) and len(words) == 3:
            return "already present"
        if hits >= 2 and _negated(unit) != summary_negated:
            contradicting.append(unit)
    if not contradicting:
        return "new"
    if quote_allowed:
        return "contradicts: " + contradicting[0]
    return "contradicts: %d line(s) in a target from other account groups (text withheld)" % len(contradicting)


class LedgerIndex(object):
    """Lookup tables over proposals.jsonl for matching this run's candidates to earlier decisions."""

    def __init__(self, proposals: List[dict]) -> None:
        self.entries = [entry for entry in proposals if isinstance(entry.get("id"), str)]
        self.states = ledger.current_states(self.entries)
        self.aliases = ledger.aliases(self.entries)
        self.old_keys = ledger.old_keys(self.entries)
        self.by_id = {}  # type: Dict[str, List[dict]]
        for entry in self.entries:
            self.by_id.setdefault(entry["id"], []).append(entry)

    def decided(self, rule_id: Optional[str]) -> Optional[Tuple[str, dict]]:
        state = self.states.get(rule_id) if rule_id else None
        return (rule_id, state["decided"]) if state else None


def _ts_key(ts) -> Tuple[float, str]:
    return (config.ts_epoch(ts) or 0.0, ts or "")


def _kind(cand: dict) -> str:
    return cand.get("kind") or ("correction" if "pairs" in cand else "ask")


def _single_best(scores: Dict[str, float]) -> Optional[str]:
    if not scores:
        return None
    top = max(scores.values())
    winners = [rule_id for rule_id, score in scores.items() if score == top]
    return winners[0] if len(winners) == 1 else None


def _match_reworded_stem(cand: dict, index: LedgerIndex,
                         cluster_last_ts_by_key: Dict[str, str]) -> Optional[str]:
    key = cand.get("key")
    first_seen = cand.get("first_seen")
    if not key or len(key) != 4 or not first_seen:
        return None
    own_tokens = cluster.content_tokens(key[2])
    scores = {}  # type: Dict[str, float]
    for rule_id, keys in index.old_keys.items():
        if rule_id not in index.states:
            continue
        for text in keys:
            old = cluster.split_key(text)
            if old is None or old[2] == key[2] or (old[0], old[1], old[3]) != (key[0], key[1], key[3]):
                continue
            last = cluster_last_ts_by_key.get(text)
            # an old key still firing after the new stem appeared is a co-occurring question, not a rewording
            if last is not None and _ts_key(first_seen) <= _ts_key(last):
                continue
            score = cluster.jaccard(cluster.content_tokens(old[2]), own_tokens)
            if score >= STEM_INHERIT_JACCARD and score > scores.get(rule_id, -1.0):
                scores[rule_id] = score
    return _single_best(scores)


def _evidence_ids(cand: dict) -> Set[str]:
    found = set(cand.get("evidence_ids") or [])
    for pair in cand.get("pairs") or []:
        event_id = (pair.get("event") or {}).get("id")
        if event_id:
            found.add(event_id)
    return found


def inherit_by_evidence(cand: dict, index: LedgerIndex) -> Optional[str]:
    """The id of the most recent ledger entry sharing an evidence event with this group."""
    own = _evidence_ids(cand)
    if not own:
        return None
    for entry in reversed(index.entries):
        if own & set(entry.get("evidence_ids") or []):
            return entry["id"]
    return None


def _correction_score(cand: dict, entries: List[dict]) -> float:
    topic = cand.get("topic")
    if topic and any(entry.get("topic") == topic for entry in entries):
        return 1.0
    pair_sets = [pair.get("tokens") or set() for pair in cand.get("pairs") or []]
    best = 0.0
    for entry in entries:
        for stored in entry.get("tokens") or []:
            if not isinstance(stored, list):
                continue
            seed = set(stored)
            for tokens in pair_sets:
                best = max(best, cluster.jaccard(set(tokens), seed))
    return best


def _match_correction(cand: dict, index: LedgerIndex) -> Optional[str]:
    scores = {}  # type: Dict[str, float]
    for rule_id, state in index.states.items():
        if not ledger.is_hiding(state["decided"].get("status")):
            continue
        score = _correction_score(cand, index.by_id.get(rule_id, []))
        if score >= TOKEN_INHERIT_JACCARD:
            scores[rule_id] = score
    if not scores:
        return None

    def rank(rule_id: str) -> Tuple[float, Tuple[float, str], str]:
        return scores[rule_id], _ts_key(index.states[rule_id]["decided"].get("decided_at")), rule_id

    return max(scores, key=rank)


def match_candidate(cand: dict, index: LedgerIndex,
                    cluster_last_ts_by_key: Dict[str, str]) -> Optional[Tuple[str, dict]]:
    """The earlier decision this candidate inherits, as (decided_id, decided_entry), or None to keep its own id."""
    found = index.decided(cand.get("id")) or index.decided(index.aliases.get(cand.get("id")))
    if found:
        return found
    if _kind(cand) == "ask":
        return index.decided(_match_reworded_stem(cand, index, cluster_last_ts_by_key))
    return index.decided(inherit_by_evidence(cand, index)) or index.decided(_match_correction(cand, index))


def ledger_disposition(cand: dict, decided: Optional[dict], events_after: int, now: str,
                       line_present: Optional[bool]) -> str:
    """How an earlier decision treats this run's candidate; a match also moves the candidate onto the decided id."""
    if not decided:
        return "proposable"
    if decided.get("id") and decided["id"] != cand.get("id"):
        cand["inherited_from"] = cand.get("id")
        cand["id"] = decided["id"]
    status = decided.get("status")
    if status == "rejected":
        threshold = max(REJECTED_MIN_EVENTS, decided.get("n_at_decision") or 0)
        return "hidden-rejected" if events_after < threshold else "proposable"
    if status == "snoozed":
        since = config.ts_epoch(decided.get("decided_at")) or config.ts_epoch(decided.get("ts"))
        now_epoch = config.ts_epoch(now)
        if since is not None and now_epoch is not None and now_epoch < since + SNOOZE_S:
            return "hidden-snoozed"
        return "proposable"
    if status == "accepted":
        return "covered" if line_present else "missing"
    return "proposable"
