"""Ask payload for AskUserQuestion events."""
from __future__ import annotations

import json
import re
import time

from apprentice import config
from apprentice.scrub import cut_safe, scrub
from apprentice.transcript import map_answer

if False:  # annotations only: importing typing or datetime costs every hook start
    from datetime import datetime
    from typing import Dict, List, Optional, Tuple, Union

_RAW_CAP = 4000
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_WORD_RE = re.compile(r"[a-z0-9]+")
_KEYVAL_TRAILING = ".,;:?!)"


def _blank(text: str, span: Tuple[int, int]) -> str:
    return text[:span[0]] + " " * (span[1] - span[0]) + text[span[1]:]


def _to_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except ValueError:
        return None


def extract_numbers(text: str, tables: dict) -> Tuple[Dict[str, Union[float, str]], List[float], Optional[str]]:
    numbers = {}  # type: Dict[str, Union[float, str]]
    reset_at = None
    match = config.RESET_AT_RE.search(text)
    if match:
        reset_at = match.group(1)
        text = _blank(text, match.span())
    for regex, name, kind in tables["number_extractors"]:
        for found in list(regex.finditer(text)):
            if name == "KEYVAL":
                key, raw = found.group(1).lower(), found.group(2)
                raw = raw.rstrip(_KEYVAL_TRAILING)
                if not raw or raw.startswith("<redacted"):
                    text = _blank(text, found.span())
                    continue
                value = _to_float(raw)  # type: Union[float, str, None]
                if value is None:
                    value = raw
            else:
                raw = found.group(1)
                value = raw if kind == "string" else _to_float(raw)
                key = name
            text = _blank(text, found.span())
            if value is not None and key not in numbers:
                numbers[key] = value
    other = [float(m.group(0)) for m in _NUMBER_RE.finditer(text)]
    return numbers, other, reset_at


def _scan_risk(text: str, words: List[str]) -> Tuple[set, set]:
    present, negated = set(), set()
    lowered = text.lower()
    for word in words:
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])")
        for hit in pattern.finditer(lowered):
            before = lowered[:hit.start()]
            tokens = _WORD_RE.findall(before)[-3:]
            if any(t in config.NEGATIONS for t in tokens) or before.endswith("non-"):
                negated.add(word)
            else:
                present.add(word)
    return present, negated


def risk_words(question: str, option_descriptions: List[str], tables: dict) -> Tuple[List[str], List[str]]:
    present, negated = set(), set()
    for text in [question] + list(option_descriptions):
        if not text:
            continue
        got, neg = _scan_risk(text, tables["risk_words"])
        present |= got
        negated |= neg
    return sorted(present), sorted(negated - present)


def _collapse(text: Optional[str]) -> str:
    return " ".join((text or "").split()).lower()


def skill_hint(header: str, question: str, tables: dict) -> Optional[str]:
    question = question or ""
    cut = question.find("?")
    stem = _collapse(question[:cut + 1] if cut >= 0 else question[:120])
    head = _collapse(header)
    body = _collapse(question)
    for phrase, fields, skill in tables["skill_hints"]:
        needle = _collapse(phrase)
        if fields == "stem":
            hit = needle in stem
        elif fields == "header_or_stem":
            hit = needle in head or needle in stem
        else:
            parts = [_collapse(p) for p in phrase.split("|")]
            hit = len(parts) == 2 and parts[0] in head and parts[1] in body
        if hit:
            return skill
    return None


def _scrubbed(text: Optional[str], cfg: dict, flags: List[str]) -> Optional[str]:
    out, got = scrub(text, cfg)
    for flag in got:
        if flag not in flags:
            flags.append(flag)
    return out


def _lookup_answer(answers: Optional[Dict[str, str]], question: str) -> Optional[str]:
    if not answers:
        return None
    if question in answers:
        return answers[question]
    wanted = " ".join(question.split())
    for key, value in answers.items():
        if " ".join(key.split()) == wanted:
            return value
    return None


def _text(value) -> Optional[str]:
    return value if value is None or isinstance(value, str) else str(value)


def _coerced(raw: dict) -> dict:
    """The question with a non-string header, question, label or description turned into a string, so one
    oddly typed field never costs the whole ask event."""
    out = dict(raw)
    for key in ("header", "question"):
        out[key] = _text(raw.get(key))
    options = raw.get("options")
    if isinstance(options, list):
        out["options"] = [dict(opt, label=_text(opt.get("label")), description=_text(opt.get("description")))
                          if isinstance(opt, dict) else opt for opt in options]
    return out


def _build(tool_questions: list, answers: Optional[Dict[str, str]], cfg: dict,
           flags: List[str]) -> Tuple[list, bool]:
    built = []
    partial = False
    for raw in tool_questions:
        raw = _coerced(raw)
        text = raw.get("question") or ""
        options = []
        for opt in raw.get("options") or []:
            if not isinstance(opt, dict):
                continue
            label = opt.get("label") or ""
            options.append({
                "label": _scrubbed(label, cfg, flags),
                "description": _scrubbed(opt.get("description"), cfg, flags),
                "recommended": "(Recommended)" in label,
            })
        answer = map_answer(_lookup_answer(answers, text), raw)
        if answer is None:
            partial = True
        else:
            answer["labels"] = [_scrubbed(lab, cfg, flags) for lab in answer["labels"]]
            answer["free_text"] = _scrubbed(answer["free_text"], cfg, flags)
        built.append({
            "header": _scrubbed(raw.get("header"), cfg, flags),
            "question": _scrubbed(text, cfg, flags),
            "multi_select": bool(raw.get("multiSelect")),
            "options": options,
            "answer": answer,
        })
    return built, partial


def build_questions(tool_questions: list, answers: Optional[Dict[str, str]], cfg: dict) -> Tuple[list, bool]:
    return _build(tool_questions, answers, cfg, [])


def ask_payload(tool_use_id: str, tool_questions: list, answers: Optional[dict], raw_response,
                cfg: dict, when_local: Optional[datetime] = None) -> Tuple[dict, List[str], bool]:
    """when_local defaults to the local wall clock now, read without importing datetime."""
    if when_local is None:
        now = time.localtime()
        hour, weekday = now.tm_hour, now.tm_wday
    else:
        hour, weekday = when_local.hour, when_local.weekday()
    flags = []  # type: List[str]
    questions, partial = _build(tool_questions, answers, cfg, flags)
    tables = config.effective_tables(cfg)
    numbers, other, reset_at = extract_numbers("\n".join(q["question"] for q in questions), tables)
    present, negated = set(), set()
    for q in questions:
        got, neg = risk_words(q["question"], [o["description"] or "" for o in q["options"]], tables)
        present |= set(got)
        negated |= set(neg)
    hint = None
    for q in questions:
        hint = skill_hint(q["header"] or "", q["question"], tables)
        if hint:
            break
    payload = {
        "tool_use_id": tool_use_id,
        "skill_hint": hint,
        "questions": questions,
        "numbers": numbers,
        "numbers_other": other,
        "reset_at": reset_at,
        "risk_words": sorted(present),
        "risk_words_negated": sorted(negated - present),
        "hour_local": hour,
        "weekday": weekday,
    }
    if raw_response is not None:
        raw = raw_response if isinstance(raw_response, str) else json.dumps(raw_response)
        payload["raw_response"] = cut_safe(_scrubbed(raw, cfg, flags) or "", _RAW_CAP)
    return payload, flags, partial
