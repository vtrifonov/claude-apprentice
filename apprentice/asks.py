"""Ask payload and departure context for AskUserQuestion events."""
import json
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union

from apprentice import config, gitinfo
from apprentice.scrub import TICKET_RE, scrub
from apprentice.transcript import map_answer

_RAW_CAP = 4000
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_WORD_RE = re.compile(r"[a-z0-9]+")


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


def find_ticket(*texts: Optional[str]) -> Optional[str]:
    for text in texts:
        if not text:
            continue
        match = TICKET_RE.search(text)
        if match:
            return match.group(0)
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


def _build(tool_questions: list, answers: Optional[Dict[str, str]], cfg: dict,
           flags: List[str]) -> Tuple[list, bool]:
    built = []
    partial = False
    for raw in tool_questions:
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
                cfg: dict, when_local: datetime) -> Tuple[dict, List[str], bool]:
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
        "hour_local": when_local.hour,
        "weekday": when_local.weekday(),
    }
    if raw_response is not None:
        raw = raw_response if isinstance(raw_response, str) else json.dumps(raw_response)
        payload["raw_response"] = (_scrubbed(raw, cfg, flags) or "")[:_RAW_CAP]
    return payload, flags, partial


def departure_context(cwd: Optional[str], *texts: Optional[str]) -> dict:
    ctx = gitinfo.repo_context(cwd) if cwd else {}
    branch = ctx.get("branch")
    return {
        "repo": ctx.get("repo"),
        "branch": branch,
        "ticket": find_ticket(branch, cwd, *texts),
    }
