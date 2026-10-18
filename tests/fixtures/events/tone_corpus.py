"""A 20-prompt synthetic corpus for tone metrics: 12 prompts from the visible profile, 8 from another group.

Every expected value in tests/test_tone.py is computed by hand from these texts. Edit a text and the
hand-computed numbers in the tests must be redone."""
from __future__ import annotations

import hashlib
import re

from tests.fixtures.events.prompts import prompt_event

VISIBLE_PROFILE = "default"
HIDDEN_PROFILE = "work"

# (key, register, text); V* belong to the visible group, W* to the other group.
_VISIBLE = [
    ("V1", "direction", "add the retry helper, so the job reruns and the queue drains"),
    ("V2", "direction", "Please fix the flaky test. Then run the suite again."),
    ("V3", "pushback", "but why did we pick the slow path, can't we cache it"),
    ("V4", "question", "where is the config loaded?"),
    ("V5", "direction", "let's discuss about the module and move the helpers out"),
    ("V6", "ack", "yep"),
    ("V7", "ack", "go"),
    ("V8", "direction", "2 - create handler for the retry"),
    ("V9", "direction", "we recieve the payload twice and the log is silent!"),
    ("V10", "direction", "i recieve nothing back, although the widgets job runs"),
    ("V11", "status_ask", "any progress on the acme widgets build"),
    ("V12", "report", "Hello, the wigdet panel is done now, thanks"),
]
_HIDDEN = [
    ("W1", "direction", "add the cache layer, and the cleanup job, and the report"),
    ("W2", "pushback", "are you sure the parser handles unicode, shouldn't we test it"),
    ("W3", "direction", "explain me the build and discuss about the plan"),
    ("W4", "question", "why is the build slow?"),
    ("W5", "ack", "sure"),
    ("W6", "direction", "Please review the informations in the dashboard"),
    ("W7", "direction", "ship the zorbalice change today"),
    ("W8", "direction", "add retry helper to the module, is helper missing"),
]

# Words the temp dictionary must NOT know: the misspellings, a repo name piece and a hidden-only word.
UNKNOWN_WORDS = {"recieve", "wigdet", "widgets", "acme", "zorbalice"}


def sha1_of(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def make_prompt(key: str, profile: str, register: str, text: str, day: int, **overrides) -> dict:
    fields = {
        "text": text,
        "sha1": sha1_of(text),
        "len": len(text),
        "words": len(text.split()),
        "tags": [],
        "register": register,
        "profile": profile,
        "session_id": "sess-" + key,
        "ts": "2030-01-%02dT10:00:00.000Z" % day,
    }
    fields.update(overrides)
    return prompt_event(**fields)


def corpus() -> list:
    events = []
    for index, (key, register, text) in enumerate(_VISIBLE):
        events.append(make_prompt(key, VISIBLE_PROFILE, register, text, index + 1))
    for index, (key, register, text) in enumerate(_HIDDEN):
        events.append(make_prompt(key, HIDDEN_PROFILE, register, text, index + 13))
    return events


def corpus_by_key() -> dict:
    keys = [item[0] for item in _VISIBLE + _HIDDEN]
    return dict(zip(keys, corpus()))


def visible(event: dict) -> bool:
    return event.get("profile") == VISIBLE_PROFILE


def dictionary_text(texts, unknown=UNKNOWN_WORDS) -> str:
    """A temp dictionary: every lowercase word in `texts` except `unknown`."""
    words = set()
    for text in texts:
        words.update(re.findall(r"[a-z]+", text.lower()))
    return "\n".join(sorted(words - set(unknown))) + "\n"


def corpus_dictionary_text() -> str:
    return dictionary_text([item[2] for item in _VISIBLE + _HIDDEN])
