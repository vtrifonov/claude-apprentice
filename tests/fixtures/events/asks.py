"""Synthetic `ask` events in the stored envelope and payload shape (see docs/record-schema.md)."""
from __future__ import annotations

from apprentice import events

if False:  # annotations only
    from typing import List

_QUESTION = ("Use fable? Budget: 74% of the weekly limit used, 2 working days left, "
             "13%/day available (pace=tight), resets 2030-01-07 04:00 +02:00.")
_MODEL_LABELS = ["Fable (Recommended)", "No fable (opus)"]
_MODE_LABELS = ["Quick (Recommended)", "Full"]
_CWD = "/home/alice/code/acme/widgets"

_SPECIAL = ("header", "question", "option_labels", "answer_labels", "free_text", "multi_select")


def _options(labels: List[str]) -> List[dict]:
    return [{"label": label, "description": "option " + label.lower(),
             "recommended": "(Recommended)" in label} for label in labels]


def _answer(option_labels: List[str], picked: List[str], free_text) -> dict:
    recommended = [label for label in option_labels if "(Recommended)" in label]
    if free_text is not None and not picked:
        followed = None
    elif recommended:
        followed = all(label in recommended for label in picked)
    else:
        followed = None
    return {"labels": list(picked), "free_text": free_text, "is_other": free_text is not None,
            "followed_recommended": followed}


def ask_event(**overrides) -> dict:
    """A full `ask` event. `header`, `question`, `option_labels`, `answer_labels`, `free_text` and
    `multi_select` shape the single question; any other keyword replaces that top-level field."""
    special = {key: overrides.pop(key) for key in _SPECIAL if key in overrides}
    option_labels = special.get("option_labels", list(_MODEL_LABELS))
    picked = special.get("answer_labels", option_labels[:1] if "free_text" not in special else [])
    free_text = special.get("free_text")
    record = events.envelope("ask", "hook", "sess-1", "default", "cli", _CWD, "acme/widgets", "main", None,
                             "/home/alice/.claude/projects/acme/sess-1.jsonl",
                             ts="2030-01-05T10:00:01.000Z")
    record.update({
        "tool_use_id": "tu1",
        "skill_hint": "oneshot-pipeline",
        "questions": [{
            "header": special.get("header", "Models"),
            "question": special.get("question", _QUESTION),
            "multi_select": special.get("multi_select", False),
            "options": _options(option_labels),
            "answer": _answer(option_labels, picked, free_text),
        }],
        "numbers": {"used_pct": 74.0, "per_day_pct": 13.0, "working_days_left": 2.0, "pace": "tight"},
        "numbers_other": [],
        "reset_at": "2030-01-07 04:00 +02:00",
        "risk_words": [],
        "risk_words_negated": [],
        "hour_local": 10,
        "weekday": 5,
    })
    record.update(overrides)
    return record


def model_set_variants() -> List[dict]:
    """Five phrasings of one model-set picker: two header spellings, varied budget clauses and numbers."""
    spec = [
        ("Models", "Use fable? Budget: 74% of the weekly limit used, 2 working days left, "
                   "13%/day available (pace=tight), resets 2030-01-07 04:00 +02:00."),
        ("Model set", "Use fable? used 4% of weekly limit, 5 working days until reset, 19% per day (pace=ample)"),
        ("Models", "Weekly usage 65%, 1 working day left, 35%/day (pace=low). Use fable?"),
        ("Model set", "Weekly usage is 87% used, 3 working days left, 4% per day, pace=tight. "
                      "Use fable? Usage: 87% resets 2030-02-11 09:30:15Z"),
        ("Models", "Budget: 61% used; 2 working days left; 20%/day; pace=tight, resets 2030-03-01T08:00. "
                   "Use fable?"),
    ]
    out = []
    for index, (header, question) in enumerate(spec):
        out.append(ask_event(header=header, question=question,
                             ts="2030-01-05T10:00:%02d.000Z" % (index + 1)))
    return out


def mode_variants() -> List[dict]:
    """Three phrasings of the quick-mode picker under the headers `Mode` and `Quick mode`."""
    spec = [
        ("Mode", "Quick mode?"),
        ("Quick mode", "Quick mode? (test-only change: one spec file)"),
        ("Mode", "Quick mode? XX-123 is a 2-value change that trips the deploy trigger"),
    ]
    out = []
    for index, (header, question) in enumerate(spec):
        out.append(ask_event(header=header, question=question, option_labels=list(_MODE_LABELS),
                             ts="2030-01-06T10:00:%02d.000Z" % (index + 1)))
    return out
