"""Audit a candidate rule against the file it would be written to."""
import os
import re
from typing import Dict, Iterable, List, Mapping, Optional

from apprentice import cluster

_NEGATION_RE = re.compile(r"\b(?:never|don'?t|no)\b")
_BULLET_RE = re.compile(r"^\s*-\s")


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
