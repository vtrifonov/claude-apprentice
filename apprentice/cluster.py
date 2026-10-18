"""Ask clusters: group AskUserQuestion events by (skill, header, stem, option labels) and find reversals."""
from __future__ import annotations

import hashlib
import re
from collections import Counter

from apprentice import config, lexicon
from apprentice.departures import ticket_prefix
from apprentice.scrub import TICKET_RE, mask_outbound

if False:  # annotations only
    from typing import Callable, Dict, List, Optional, Set, Tuple

STEM_FALLBACK_CHARS = 120
PAIR_LIMIT = 150
JACCARD_THRESHOLD = 0.5
DROP_CONFIDENCE = 0.8
LINK_SHARED_TOKENS = 2
LINK_WINDOW_S = 120.0
_CONTENT_TOKEN_RE = re.compile(r"[a-z][a-z0-9_'-]+")
_SIGNAL_TAGS = ("correction", "standing")
REVERSAL_WINDOW_S = 7 * 86400.0
RECENT_WINDOW_S = 30 * 86400.0
DISPLAY_QUESTIONS = 3
_KEY_SEP = "\x1f"
_RECOMMENDED = "(Recommended)"

_BUDGET_RE = re.compile(r"(?:Budget:|Weekly\s+budget|Weekly\s+usage|Usage:).*?(?:[.!?](?=\s|$)|$)",
                        re.IGNORECASE | re.DOTALL)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?(?:\s*[+-]\d{2}:?\d{2}|Z)?")
_QUOTED_RE = re.compile(r"\"[^\"]*\"|(?<!\w)'[^']*'|`[^`]*`")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")
_TOKEN_RE = re.compile(r"\S+")
_EXT_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,4}$")
_ABBREV_RE = re.compile(r"^(?:[A-Za-z]\.)+[A-Za-z]$")
_PUNCT_RE = re.compile(r"[^\w\s?<>]")
_TOKEN_TAIL = "?.,;:!)"


def _is_path(core: str) -> bool:
    if "/" in core:
        return True
    return core[0].isalpha() and not _ABBREV_RE.match(core) and bool(_EXT_RE.search(core))


def _path_token(match) -> str:
    token = match.group(0)
    core = token.rstrip(_TOKEN_TAIL)
    if core and _is_path(core):
        return "<path>" + token[len(core):]
    return token


def normalize_stem(question: str) -> str:
    text = question or ""
    mark = text.find("?")
    text = text[:mark + 1] if mark >= 0 else text[:STEM_FALLBACK_CHARS]
    text = _BUDGET_RE.sub("", text)
    text = _DATE_RE.sub("", text)
    text = _QUOTED_RE.sub("<q>", text)
    text = TICKET_RE.sub("<ticket>", text)
    text = _TOKEN_RE.sub(_path_token, text)
    text = _NUMBER_RE.sub("", text)
    text = _PUNCT_RE.sub("", text.lower())
    return " ".join(text.split())


def fold_header(header: Optional[str], aliases: Dict[str, str]) -> str:
    folded = " ".join((header or "").lower().split())
    return aliases.get(folded, folded)


def _label_name(label: str) -> str:
    return label.replace(_RECOMMENDED, "").strip().lower()


def cluster_key(skill: Optional[str], header: Optional[str], question: str, labels: List[str],
                aliases: Dict[str, str]) -> Tuple[str, str, str, str]:
    return (skill or "", fold_header(header, aliases), normalize_stem(question),
            "|".join(sorted(_label_name(label) for label in labels)))


def key_string(key: Tuple[str, str, str, str]) -> str:
    return _KEY_SEP.join(key)


def split_key(text: str) -> Optional[Tuple[str, str, str, str]]:
    """The inverse of key_string; None for anything that is not a four-part key."""
    parts = text.split(_KEY_SEP) if isinstance(text, str) else []
    return (parts[0], parts[1], parts[2], parts[3]) if len(parts) == 4 else None


def ask_rule_id(key: Tuple[str, str, str, str]) -> str:
    return "r_" + hashlib.sha1(("ask:" + key_string(key)).encode()).hexdigest()[:8]


def _answer_string(answer: Optional[dict]) -> Optional[str]:
    if not answer:
        return None
    if answer.get("is_other"):
        return "other"
    names = sorted(_label_name(label) for label in answer.get("labels") or [])
    return "|".join(names) if names else None


def _ts_key(ts) -> Tuple[float, str]:
    return (config.ts_epoch(ts) or 0.0, ts or "")


def _recommended_labels(question: dict) -> Optional[List[str]]:
    found = [opt.get("label") or "" for opt in question.get("options") or []
             if opt.get("recommended") or _RECOMMENDED in (opt.get("label") or "")]
    return found or None


def _row(event: dict, question: dict, is_visible: bool) -> dict:
    answer = question.get("answer") or {}
    free_text = answer.get("free_text")
    return {
        "event_id": event.get("id"),
        "session_id": event.get("session_id"),
        "ts": event.get("ts"),
        "profile": event.get("profile"),
        "visible": is_visible,
        "labels": sorted(_label_name(label) for label in answer.get("labels") or []),
        "free_text": mask_outbound(free_text) if is_visible and free_text else None,
        "is_other": bool(answer.get("is_other")),
        "followed_recommended": answer.get("followed_recommended"),
        "recommended": _recommended_labels(question),
        "repo": event.get("repo"),
        "ticket": event.get("ticket") if is_visible else ticket_prefix(event.get("ticket")),
        "numbers": event.get("numbers") or {},
        "risk_words": event.get("risk_words") or [],
        "skill_hint": event.get("skill_hint"),
        "weekday": event.get("weekday"),
        "hour_local": event.get("hour_local"),
        "cwd": event.get("cwd") if is_visible else None,
        "reset_at": event.get("reset_at"),
        "numbers_other": event.get("numbers_other") or [],
        "risk_words_negated": event.get("risk_words_negated") or [],
    }


def _find_reversals(rows: List[dict]) -> List[dict]:
    answered = []
    for row in rows:
        epoch = config.ts_epoch(row["ts"])
        if epoch is not None and row["_answer"] is not None:
            answered.append((epoch, row))
    answered.sort(key=lambda pair: pair[0])
    found = []
    for (epoch_a, row_a), (epoch_b, row_b) in zip(answered, answered[1:]):
        if epoch_b - epoch_a > REVERSAL_WINDOW_S or row_a["_answer"] == row_b["_answer"]:
            continue
        same_session = bool(row_a["session_id"]) and row_a["session_id"] == row_b["session_id"]
        same_repo = bool(row_a["repo"]) and row_a["repo"] == row_b["repo"]
        if same_session or same_repo:
            found.append({"ts_a": row_a["ts"], "answer_a": row_a["_answer"],
                          "ts_b": row_b["ts"], "answer_b": row_b["_answer"]})
    return found


def _finish(acc: dict) -> dict:
    rows = sorted(acc["rows"], key=lambda row: _ts_key(row["ts"]))
    reversals = _find_reversals(rows)
    for row in rows:
        row.pop("_answer", None)
    answers = acc["answers"]
    followed = acc["followed"]
    recent = sorted(acc["questions"], key=lambda pair: _ts_key(pair[0]), reverse=True)
    visible_rows = sum(1 for row in rows if row["visible"])
    stamps = [row["ts"] for row in rows if row["ts"]]
    return {
        "key": acc["key"],
        "id": acc["id"],
        "skill": acc["key"][0] or None,
        "header": acc["key"][1],
        "stem": acc["key"][2],
        "labels": acc["key"][3].split("|") if acc["key"][3] else [],
        "rows": rows,
        "n": len(rows),
        "answers": answers,
        "rec_follow_rate": (sum(1 for flag in followed if flag) / len(followed)) if followed else None,
        "free_texts": [mask_outbound(text) for text in acc["free_texts"]],
        "free_text_withheld": acc["withheld_texts"],
        "sessions": sorted({row["session_id"] for row in rows if row["session_id"]}),
        "repos": sorted({row["repo"] for row in rows if row["repo"]}),
        "first_seen": min(stamps, key=_ts_key) if stamps else None,
        "last_seen": max(stamps, key=_ts_key) if stamps else None,
        "reversals": reversals,
        "raw_headers": sorted(acc["raw_headers"]),
        "display_questions": [mask_outbound(text) for _, text in recent[:DISPLAY_QUESTIONS]],
        "text_visible": visible_rows > 0,
        "visible_rows": visible_rows,
        "withheld_rows": len(rows) - visible_rows,
    }


def build_ask_clusters(events: List[dict], cfg: dict, visible: Callable[[dict], bool]) -> List[dict]:
    aliases = config.effective_tables(cfg)["header_aliases"]
    groups = {}  # type: Dict[Tuple[str, str, str, str], dict]
    for event in events:
        if event.get("kind") != "ask":
            continue
        is_visible = bool(visible(event))
        for question in event.get("questions") or []:
            options = [opt.get("label") or "" for opt in question.get("options") or []]
            key = cluster_key(event.get("skill_hint"), question.get("header"), question.get("question") or "",
                              options, aliases)
            acc = groups.get(key)
            if acc is None:
                acc = groups[key] = {
                    "key": key, "id": ask_rule_id(key), "rows": [], "answers": Counter(), "followed": [],
                    "free_texts": [], "withheld_texts": 0, "raw_headers": set(), "questions": [],
                }
            answer = question.get("answer")
            row = _row(event, question, is_visible)
            row["_answer"] = _answer_string(answer)
            acc["rows"].append(row)
            if row["_answer"] is not None:
                acc["answers"][row["_answer"]] += 1
            if answer and answer.get("followed_recommended") is not None:
                acc["followed"].append(bool(answer["followed_recommended"]))
            if answer and answer.get("free_text"):
                if is_visible:
                    acc["free_texts"].append(answer["free_text"])
                else:
                    acc["withheld_texts"] += 1
            if is_visible:
                if question.get("header"):
                    acc["raw_headers"].add(question["header"])
                acc["questions"].append((event.get("ts"), question.get("question") or ""))
    clusters = [_finish(acc) for acc in groups.values()]
    clusters.sort(key=lambda found: (-found["n"], found["id"]))
    return clusters


def reversal_30d(cluster: dict, now) -> bool:
    now_epoch = config.ts_epoch(now) if isinstance(now, str) else now
    if now_epoch is None:
        return False
    for reversal in cluster.get("reversals") or []:
        later = config.ts_epoch(reversal["ts_b"])
        if later is not None and now_epoch - later <= RECENT_WINDOW_S:
            return True
    return False


def content_tokens(text: Optional[str]) -> Set[str]:
    return set(_CONTENT_TOKEN_RE.findall((text or "").lower())) - lexicon.stopwords()


def jaccard(a: Set[str], b: Set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _has_signal(event: dict) -> bool:
    return (bool(set(event.get("tags") or []) & set(_SIGNAL_TAGS))
            or bool(event.get("after_interrupt")) or bool(event.get("after_rejected_ask")))


def _recent_first(pair_or_event_ts, event_id) -> Tuple[Tuple[float, str], str]:
    return (_ts_key(pair_or_event_ts), event_id or "")


_WITHHELD_EVENT_KEYS = ("id", "kind", "ts", "session_id", "profile", "repo", "tags", "after_interrupt",
                        "after_rejected_ask", "strong_marker")


def _withheld_event(event: dict) -> dict:
    """A non-visible prompt keeps its signal flags and ids, never its text, anchor, ticket, cwd or branch."""
    kept = {key: event.get(key) for key in _WITHHELD_EVENT_KEYS if key in event}
    kept["ticket"] = ticket_prefix(event.get("ticket"))
    return kept


def select_pairs(events: List[dict], limit: int = PAIR_LIMIT,
                 visible: Optional[Callable[[dict], bool]] = None) -> List[dict]:
    """Correction candidates: prompts with a correction signal and a previous-assistant anchor, most recent first."""
    chosen = [event for event in events
              if event.get("kind") == "prompt" and event.get("prev_assistant") and _has_signal(event)]
    chosen.sort(key=lambda event: _recent_first(event.get("ts"), event.get("id")), reverse=True)
    out = []
    for event in chosen[:limit]:
        is_visible = True if visible is None else bool(visible(event))
        out.append({
            "event": event if is_visible else _withheld_event(event),
            "tokens": content_tokens(event.get("text")),
            "visible": is_visible,
            "profile": event.get("profile"),
            "ts": event.get("ts"),
            "session_id": event.get("session_id"),
            "repo": event.get("repo"),
        })
    return out


def _correction_group(seed_pair: dict, topic: Optional[str]) -> dict:
    return {"seed": seed_pair["event"].get("id"), "pairs": [seed_pair], "tokens": set(seed_pair["tokens"]),
            "topic": topic}


def _finish_group(acc: dict) -> dict:
    pairs = acc["pairs"]
    earliest = min(pairs, key=lambda pair: _recent_first(pair["ts"], pair["event"].get("id")))
    stamps = [pair["ts"] for pair in pairs if pair["ts"]]
    visible_pairs = sum(1 for pair in pairs if pair["visible"])
    return {
        "id": "r_" + hashlib.sha1(("corr:" + str(earliest["event"].get("id"))).encode()).hexdigest()[:8],
        "seed": acc["seed"],
        "pairs": pairs,
        "tokens": acc["tokens"],
        "topic": acc["topic"],
        "first_seen": min(stamps, key=_ts_key) if stamps else None,
        "last_seen": max(stamps, key=_ts_key) if stamps else None,
        "sessions": sorted({pair["session_id"] for pair in pairs if pair["session_id"]}),
        "repos": sorted({pair["repo"] for pair in pairs if pair["repo"]}),
        "visible_pairs": visible_pairs,
        "withheld_pairs": len(pairs) - visible_pairs,
        "strong_marker_pairs": sum(1 for pair in pairs if pair["event"].get("strong_marker")),
    }


def group_pairs(pairs: List[dict], preclass: Dict[str, dict]) -> List[dict]:
    by_topic = {}  # type: Dict[str, dict]
    by_overlap = []  # type: List[dict]
    ordered = sorted(pairs, key=lambda pair: _recent_first(pair["ts"], pair["event"].get("id")), reverse=True)
    for pair in ordered:
        verdict = preclass.get(pair["event"].get("id")) or {}
        if verdict.get("is_correction") is False and (verdict.get("confidence") or 0.0) >= DROP_CONFIDENCE:
            continue
        topic = verdict.get("topic")
        if topic:
            acc = by_topic.get(topic)
            if acc is None:
                by_topic[topic] = _correction_group(pair, topic)
            else:
                acc["pairs"].append(pair)
            continue
        for acc in by_overlap:
            if jaccard(pair["tokens"], acc["tokens"]) >= JACCARD_THRESHOLD:
                acc["pairs"].append(pair)
                break
        else:
            by_overlap.append(_correction_group(pair, None))
    groups = [_finish_group(acc) for acc in list(by_topic.values()) + by_overlap]
    groups.sort(key=lambda found: (-len(found["pairs"]), found["id"]))
    return groups


def _cluster_tokens(found: dict) -> Set[str]:
    return content_tokens("%s %s" % (found.get("header") or "", found.get("stem") or ""))


def _pair_links(pair: dict, header_stem_tokens: Set[str], asks: List[Tuple[Optional[str], float]]) -> bool:
    if pair["event"].get("after_rejected_ask"):
        return True
    if len(pair["tokens"] & header_stem_tokens) >= LINK_SHARED_TOKENS:
        return True
    epoch = config.ts_epoch(pair["ts"])
    if epoch is None or not pair["session_id"]:
        return False
    for session_id, ask_epoch in asks:
        if session_id == pair["session_id"] and 0.0 <= epoch - ask_epoch <= LINK_WINDOW_S:
            return True
    return False


def link_groups_to_clusters(groups: List[dict], clusters: List[dict], events: List[dict]) -> Dict[str, List[str]]:
    """Map each correction group to the ask clusters it plausibly answers."""
    ask_by_id = {event.get("id"): event for event in events if event.get("kind") == "ask"}
    cluster_tokens = {found["id"]: _cluster_tokens(found) for found in clusters}
    cluster_asks = {}  # type: Dict[str, List[Tuple[Optional[str], float]]]
    for found in clusters:
        stamped = []
        for row in found.get("rows") or []:
            ask = ask_by_id.get(row.get("event_id"))
            epoch = config.ts_epoch(ask.get("ts")) if ask else None
            if ask is not None and epoch is not None:
                stamped.append((ask.get("session_id"), epoch))
        cluster_asks[found["id"]] = stamped
    linked = {}  # type: Dict[str, List[str]]
    for group in groups:
        hits = []
        for found in clusters:
            if any(_pair_links(pair, cluster_tokens[found["id"]], cluster_asks[found["id"]])
                   for pair in group["pairs"]):
                hits.append(found["id"])
        linked[group["id"]] = hits
    return linked
