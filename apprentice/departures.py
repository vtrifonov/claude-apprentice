"""Departure search (Q1): which single feature separates the rare answers from the usual one in an ask cluster."""
from __future__ import annotations

import re
from collections import Counter

if False:  # annotations only
    from typing import Callable, List, Optional

MIN_N = 4
MIN_DEPARTURES = 2
MIN_PURITY = 0.8
MIN_SIDE = 2
OTHER = "other"
_META = frozenset(["answer", "visible", "ts", "numbers", "numbers_other", "reset_at", "risk_words_negated",
                   "ticket", "recommended"])
_PREFIX_RE = re.compile(r"^([A-Za-z]+)(?:-|$)")
_RECOMMENDED = "(Recommended)"
_SYMBOLS = {">=": "≥", "<": "<", "==": "="}


def hour_band(h: int) -> str:
    if h <= 6:
        return "night"
    if h <= 11:
        return "morning"
    if h <= 17:
        return "afternoon"
    return "evening"


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def ticket_prefix(ticket: Optional[str]) -> Optional[str]:
    """`XX` from `XX-123`; a bare prefix (already cut) maps to itself."""
    match = _PREFIX_RE.match(ticket or "")
    return match.group(1) if match else None


def _recommended_string(labels: Optional[List[str]]) -> Optional[str]:
    names = sorted(label.replace(_RECOMMENDED, "").strip().lower() for label in labels or [])
    return "|".join(names) if names else None


def _row_answer(row: dict) -> Optional[str]:
    if row.get("is_other"):
        return OTHER
    labels = row.get("labels") or []
    return "|".join(labels) if labels else None


def _weekday(value):
    """Weekday is categorical (equality only), so a number becomes its string."""
    return str(int(value)) if _is_number(value) else value


def feature_rows(cluster: dict) -> List[dict]:
    answered = [row for row in cluster.get("rows") or [] if _row_answer(row) is not None]
    words = sorted({word for row in answered for word in row.get("risk_words") or []})
    out = []
    for row in answered:
        features = {}
        for key, value in (row.get("numbers") or {}).items():
            if key in _META:
                continue
            if _is_number(value):
                features[key] = float(value)
            elif isinstance(value, str):
                features[key] = value
        hour = row.get("hour_local")
        extras = {
            "repo": row.get("repo"),
            "ticket_prefix": ticket_prefix(row.get("ticket")),
            "skill_hint": row.get("skill_hint"),
            "weekday": _weekday(row.get("weekday")),
            "hour_band": hour_band(hour) if _is_number(hour) else None,
        }
        features.update({key: value for key, value in extras.items() if value is not None})
        present = set(row.get("risk_words") or [])
        for word in words:
            features["risk:" + word] = word in present
        features.update({
            "answer": _row_answer(row),
            "visible": bool(row.get("visible")),
            "ts": row.get("ts"),
            "numbers": row.get("numbers") or {},
            "numbers_other": row.get("numbers_other") or [],
            "reset_at": row.get("reset_at"),
            "risk_words_negated": row.get("risk_words_negated") or [],
            "ticket": row.get("ticket"),
            "recommended": _recommended_string(row.get("recommended")),
        })
        out.append(features)
    return out


def _most_common(answers: List[str]) -> Optional[str]:
    if not answers:
        return None
    counts = Counter(answers)
    return sorted(counts, key=lambda answer: (-counts[answer], answer))[0]


def majority_answer(rows: List[dict]) -> str:
    return _most_common([row["answer"] for row in rows]) or ""


def recommended_answer(cluster: dict) -> Optional[str]:
    """The recommended label set that is most common across rows, as an answer string."""
    sets = [_recommended_string(row.get("recommended")) for row in cluster.get("rows") or []]
    return _most_common([found for found in sets if found is not None])


def _candidates(rows: List[dict]):
    features = sorted({key for row in rows for key in row if key not in _META})
    for feature in features:
        values = [row[feature] for row in rows if feature in row and row[feature] is not None]
        numeric = sorted({value for value in values if _is_number(value)})
        for value in numeric:
            yield feature, ">=", value, True, lambda row, f=feature, v=value: _is_number(row.get(f)) and row[f] >= v
            yield feature, "<", value, True, lambda row, f=feature, v=value: _is_number(row.get(f)) and row[f] < v
        plain = sorted({value for value in values if not _is_number(value)}, key=str)
        for value in plain:
            yield feature, "==", value, False, lambda row, f=feature, v=value: f in row and row[f] == v


def find_split(rows: List[dict], is_departure: Callable[[dict], bool]) -> Optional[dict]:
    flags = [bool(is_departure(row)) for row in rows]
    maj_answer = majority_answer([row for row, flag in zip(rows, flags) if not flag])
    best = None
    best_key = None
    for feature, op, value, numeric, test in _candidates(rows):
        dep_total = dep_count = maj_total = maj_count = 0
        dep_answers = []
        for row, flag in zip(rows, flags):
            if test(row):
                dep_total += 1
                if flag:
                    dep_count += 1
                    dep_answers.append(row["answer"])
            else:
                maj_total += 1
                if not flag:
                    maj_count += 1
        if min(dep_total, maj_total) < MIN_SIDE:
            continue
        purity_dep = dep_count / dep_total
        purity_maj = maj_count / maj_total
        if purity_dep < MIN_PURITY or purity_maj < MIN_PURITY:
            continue
        purity = purity_dep + purity_maj
        order = (value if numeric else 0, str(value))
        key = (-round(purity, 9), -min(dep_total, maj_total), 0 if numeric else 1, feature,
               op != ">=", order)
        if best_key is None or key < best_key:
            best_key = key
            best = {"feature": feature, "op": op, "value": value, "dep_side": (dep_count, dep_total),
                    "maj_side": (maj_count, maj_total), "purity": purity,
                    "dep_answer": _most_common(dep_answers), "maj_answer": maj_answer}
    return best


def _departure_row(row: dict) -> dict:
    ticket = row.get("ticket")
    return {
        "ts": row.get("ts"),
        "repo": row.get("repo"),
        "ticket": ticket if row.get("visible") else ticket_prefix(ticket),
        "numbers": row.get("numbers"),
        "answer": row.get("answer"),
        "reset_at": row.get("reset_at"),
        "risk_words_negated": row.get("risk_words_negated"),
    }


def analyze(cluster: dict) -> dict:
    result = {"majority": None, "recommended": None, "departure_rows": []}
    rows = feature_rows(cluster)
    if len(rows) < MIN_N:
        return result
    majority = majority_answer(rows)
    by_majority = [row for row in rows if row["answer"] != majority]
    if len(by_majority) >= MIN_DEPARTURES:
        result["majority"] = find_split(rows, lambda row: row["answer"] != majority)
        result["departure_rows"] = [_departure_row(row) for row in by_majority]
    if recommended_answer(cluster) is not None:
        with_rec = [row for row in rows if row.get("recommended") is not None]
        by_recommended = [row for row in with_rec if row["answer"] != row["recommended"]]
        if len(by_recommended) >= MIN_DEPARTURES:
            result["recommended"] = find_split(with_rec, lambda row: row["answer"] != row["recommended"])
            if not result["departure_rows"]:
                result["departure_rows"] = [_departure_row(row) for row in by_recommended]
    return result


def _format_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return "%g" % value
    return str(value)


def sentence(split: Optional[dict]) -> str:
    if not split:
        return "no separating feature found"
    dep_count, dep_total = split["dep_side"]
    maj_count, maj_total = split["maj_side"]
    return '%s %s %s → "%s" %d of %d; otherwise "%s" %d of %d' % (
        split["feature"], _SYMBOLS[split["op"]], _format_value(split["value"]), split["dep_answer"],
        dep_count, dep_total, split["maj_answer"], maj_count, maj_total)
