"""Synthetic `prompt` events in the stored envelope and payload shape (see docs/record-schema.md)."""
from __future__ import annotations

from apprentice import events

_CWD = "/home/alice/code/acme/widgets"


def prompt_event(**overrides) -> dict:
    """A full `prompt` event tagged `correction` with a previous-assistant anchor.
    Any keyword replaces the top-level field of that name."""
    record = events.envelope("prompt", "hook", "sess-1", "default", "cli", _CWD, "acme/widgets", "main", None,
                             "/home/alice/.claude/projects/acme/sess-1.jsonl",
                             ts="2030-01-05T10:00:00.000Z")
    record.update({
        "text": "no, use the other helper instead",
        "sha1": "0" * 40,
        "len": 32,
        "words": 6,
        "tags": ["correction"],
        "register": "pushback",
        "strong_marker": False,
        "after_interrupt": False,
        "after_rejected_ask": False,
        "prev_assistant": "I will call the shared helper.",
        "duplicate_of": None,
    })
    record.update(overrides)
    return record
