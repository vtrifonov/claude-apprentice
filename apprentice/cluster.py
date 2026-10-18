"""Ask clusters: group AskUserQuestion events by (skill, header, stem, option labels) and find reversals."""
from __future__ import annotations

import hashlib
import re
from collections import Counter

from apprentice import config
from apprentice.scrub import TICKET_RE, mask_outbound

if False:  # annotations only
    from typing import Callable, Dict, List, Optional, Tuple

STEM_FALLBACK_CHARS = 120
REVERSAL_WINDOW_S = 7 * 86400.0
RECENT_WINDOW_S = 30 * 86400.0
DISPLAY_QUESTIONS = 3
_KEY_SEP = "\x1f"
_RECOMMENDED = "(Recommended)"

_BUDGET_RE = re.compile(r"(?:Budget:|Weekly\s+budget|Weekly\s+usage|Usage:).*?(?:[.!?](?=\s|$)|$)",
                        re.IGNORECASE | re.DOTALL)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?(?:\s*[+-]\d{2}:?\d{2}|Z)?")
_QUOTED_RE = re.compile(r"\"[^\"]*\"|(?<!\w)'[^']*'|`[^`]*`")
_PCT_RE = re.compile(r"\d+(?:\.\d+)?%")
_TOKEN_RE = re.compile(r"\S+")
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_PUNCT_RE = re.compile(r"[^\w\s?<>]")
_TOKEN_TAIL = "?.,;:!)"


def _path_token(match) -> str:
    token = match.group(0)
    core = token.rstrip(_TOKEN_TAIL)
    if core and ("/" in core or _EXT_RE.search(core)):
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
    text = _PCT_RE.sub("", text)
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


def _row(event: dict, answer: Optional[dict], is_visible: bool) -> dict:
    answer = answer or {}
    free_text = answer.get("free_text")
    return {
        "event_id": event.get("id"),
        "session_id": event.get("session_id"),
        "ts": event.get("ts"),
        "profile": event.get("profile"),
        "visible": is_visible,
        "labels": sorted(_label_name(label) for label in answer.get("labels") or []),
        "free_text": free_text if is_visible else None,
        "is_other": bool(answer.get("is_other")),
        "followed_recommended": answer.get("followed_recommended"),
        "repo": event.get("repo"),
        "ticket": event.get("ticket"),
        "numbers": event.get("numbers") or {},
        "risk_words": event.get("risk_words") or [],
        "skill_hint": event.get("skill_hint"),
        "weekday": event.get("weekday"),
        "hour_local": event.get("hour_local"),
        "cwd": event.get("cwd"),
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
    for index, (epoch_a, row_a) in enumerate(answered):
        for epoch_b, row_b in answered[index + 1:]:
            if epoch_b - epoch_a > REVERSAL_WINDOW_S:
                break
            if row_a["_answer"] == row_b["_answer"]:
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
            row = _row(event, answer, is_visible)
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
            if question.get("header"):
                acc["raw_headers"].add(question["header"])
            if is_visible:
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
