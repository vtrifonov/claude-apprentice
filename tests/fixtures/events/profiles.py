"""Synthetic events across four profiles, each profile's text carrying its own marker word.

Profiles `.claude-acme-alice` and `.claude-acme-bob` form the `work` group in the tests that configure one;
`default` and `unknown` (backfilled, no profile rule) are the others. Every free-text field of a profile's events
(prompt text, anchor, free-text answers, ad-hoc stems and option labels, cwd, branch, transcript path) carries its
marker, so a search of the written pack for a marker shows whether that profile's text leaked.
"""
from __future__ import annotations

import time

from apprentice import events
from tests.fixtures.events.asks import ask_event
from tests.fixtures.events.prompts import prompt_event

if False:  # annotations only
    from typing import List, Optional

ALICE = ".claude-acme-alice"
BOB = ".claude-acme-bob"
DEFAULT = "default"
UNKNOWN = "unknown"
PROFILES = (ALICE, BOB, DEFAULT, UNKNOWN)
MARKERS = {ALICE: "zorbalice", BOB: "zorbbob", DEFAULT: "zorbdefault", UNKNOWN: "zorbunknown"}
_HOUR = {ALICE: 1, BOB: 2, DEFAULT: 3, UNKNOWN: 4}
_BASE = 1767225600  # 2026-01-01T00:00:00Z
_CORRECTIONS = {
    ALICE: "wants tabular widgets aligned neatly",
    BOB: "prefers verbose loggers everywhere",
    DEFAULT: "likes compact changelogs",
    UNKNOWN: "expects grouped imports",
}
MODEL_LABELS = ["Fable (Recommended)", "No fable (opus)"]
MODE_LABELS = ["Quick (Recommended)", "Full"]


def ts(day: float, seconds: int = 0) -> str:
    """A stored-format timestamp `day` days (and `seconds`) after 2026-01-01T00:00:00Z."""
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(_BASE + int(day * 86400) + seconds))


def _place(profile: str) -> dict:
    marker = MARKERS[profile]
    out = {
        "profile": profile,
        "cwd": "/nonexistent-zorb/%s/widgets" % marker,
        "transcript_path": "/nonexistent-zorb/%s/session.jsonl" % marker,
        "branch": "feat/%s" % marker,
        "repo": "acme/widgets",
    }
    if profile == UNKNOWN:
        out.update({"source": "backfill", "fidelity": "transcript"})
    return out


def correction(profile: str, day: float, session: str, text: Optional[str] = None) -> dict:
    marker = MARKERS[profile]
    return prompt_event(text=text or "no, %s %s" % (marker, _CORRECTIONS[profile]),
                        prev_assistant="I will %s adjust the layout." % marker,
                        ts=ts(day, _HOUR[profile] * 3600), session_id=session, **_place(profile))


def ask(profile: str, day: float, session: str, header: str, question: str, option_labels: List[str],
        picked: Optional[List[str]] = None, free_text: Optional[str] = None, skill: Optional[str] = None,
        numbers: Optional[dict] = None) -> dict:
    shape = {"header": header, "question": question, "option_labels": list(option_labels)}
    if free_text is not None:
        shape["free_text"] = free_text
    else:
        shape["answer_labels"] = list(picked if picked is not None else option_labels[:1])
    return ask_event(ts=ts(day, 10 * 3600), session_id=session, tool_use_id="tu-" + session, skill_hint=skill,
                     numbers=dict(numbers or {}), **dict(shape, **_place(profile)))


def _write(home: str, records: List[dict]) -> List[str]:
    for record in records:
        events.append_event(home, record)
    return [record["id"] for record in records]


def build(home: str) -> dict:
    """Write the fixture events under `home`; return the ids of each part, the markers and first-seen times."""
    ids = {"corrections": {}}
    for profile in PROFILES:
        records = [correction(profile, day, "corr-%s-%d" % (MARKERS[profile], index))
                   for index, day in enumerate((0, 9, 18))]
        ids["corrections"][profile] = _write(home, records)

    rotation = [ALICE, BOB, DEFAULT, UNKNOWN] * 2
    used = [10, 20, 80, 30, 40, 90, 50, 95]
    model = []
    for index, (profile, pct) in enumerate(zip(rotation, used)):
        picked = MODEL_LABELS[1:] if pct >= 80 else MODEL_LABELS[:1]
        model.append(ask(profile, 1 + 8 * index, "model-%d" % index, "Models", "Use fable?", MODEL_LABELS,
                         picked=picked, numbers={"used_pct": float(pct)}))
    ids["model"] = _write(home, model)

    layout = ["Zorbalice grid", "Plain list"]
    ids["adhoc_work"] = _write(home, [
        ask(ALICE, 2, "layout-0", "Layout", "Use the zorbalice grid layout?", layout),
        ask(BOB, 10, "layout-1", "Layout", "Use the zorbalice grid layout?", layout),
        ask(ALICE, 18, "layout-2", "Layout", "Use the zorbalice grid layout?", layout),
    ])

    steps = ["Steps", "Single"]
    ids["variant"] = _write(home, [
        ask(ALICE, 3, "steps-0", "Steps", "Split the rollout into steps?", steps,
            free_text="zorbalice wants staged steps"),
        ask(BOB, 11, "steps-1", "Steps", "Split the rollout into steps?", steps),
        ask(DEFAULT, 19, "steps-2", "Steps", "Split the rollout into steps?", steps,
            free_text="zorbdefault wants a single rollout"),
    ])

    ids["picker"] = _write(home, [
        ask(ALICE, 4, "mode-0", "Mode", "Quick mode?", MODE_LABELS, skill="oneshot-pipeline"),
        ask(BOB, 12, "mode-1", "Mode", "Quick mode?", MODE_LABELS, skill="oneshot-pipeline",
            free_text="zorbbob wants the full review"),
        ask(ALICE, 20, "mode-2", "Mode", "Quick mode?", MODE_LABELS, skill="oneshot-pipeline"),
    ])

    naming = ["Rename zorbunknown", "Keep"]
    ids["adhoc_unknown"] = _write(home, [
        ask(UNKNOWN, 5, "naming-0", "Naming", "Rename the zorbunknown helper?", naming,
            free_text="zorbunknown prefers short names"),
        ask(UNKNOWN, 13, "naming-1", "Naming", "Rename the zorbunknown helper?", naming, picked=["Keep"]),
    ])

    first_seen = {profile: ts(0, _HOUR[profile] * 3600) for profile in PROFILES}
    return {"ids": ids, "markers": dict(MARKERS), "first_seen": first_seen}
