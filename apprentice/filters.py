from __future__ import annotations

import hashlib
import re

if False:  # annotations only: importing typing costs every hook start
    from typing import Dict, List, Optional

TEXT_CAP = 4000
PASTED_CAP = 300
LONG_TEXT = 1500

_DROP_PREFIXES = (
    "Continue this fork with the following request",
    "Caveat: The messages below",
    "This session is being continued from",
)
_COMMAND_RE = re.compile(r"^/[\w:.-]+$")
_IMAGE_ONLY_RE = re.compile(r"^\[Image #\d+\]\W*$")
_PASTE_LINE_RE = re.compile(r"(?:>|```|[ \t]+at |[ \t]*File \"|Traceback)")
_PASTE_LINES = 3
_ACK_RE = re.compile(r"^(continue|go on|resume|retry|go|yep|yes|ok|sure|post|approve)\W*$",
                     re.IGNORECASE)


def _phrases(words: str) -> "re.Pattern":
    parts = [re.escape(w.strip()) for w in words.split(",")]
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b")


_CORRECTION_FIRST_RE = re.compile(r"^(?:but|wait)\b")
_CORRECTION_RE = _phrases(
    "why did we, why do we, can't we, shouldn't we, didn't we, isn't it, what do you mean, "
    "are you sure, i said, i thought we, again, undo, revert, the fix is different")
_STANDING_RE = _phrases("remember, store in your memory, always, never, from now on, i said")
_DECISION_RE = re.compile(
    r"\b(?:let's|go with|approve|i allow)\b|^q?\d+\s*[-:–>]|^[a-d]\b[\s,)-]")
_STATUS_RE = _phrases("where are we, what is remaining, any progress, did you, is it running")
_REPORT_RE = _phrases("merged, closed, done")
_STRONG_RE = _phrases(
    "but i said, i thought we, store in your memory, remember to, remember that, from now on")


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()


def normalized_sha1(text: str) -> str:
    return _sha1(" ".join(text.lower().split()))


def _paste_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if _PASTE_LINE_RE.match(line))


def _result(outcome: str, text: str, stored: Optional[str], **extra) -> Dict:
    result = {
        "outcome": outcome,
        "command": None,
        "text": stored,
        "sha1": _sha1(text),
        "len": len(text),
        "words": len(text.split()),
        "tags": [],
        "register": None,
        "strong_marker": False,
        "pasted": False,
        "image_only": False,
    }
    result.update(extra)
    return result


def _tags(head: str) -> List[str]:
    tags = []
    if _CORRECTION_FIRST_RE.match(head) or _CORRECTION_RE.search(head):
        tags.append("correction")
    if _STANDING_RE.search(head):
        tags.append("standing")
    if _DECISION_RE.search(head):
        tags.append("decision")
    if _STATUS_RE.search(head):
        tags.append("status_ask")
    if _REPORT_RE.search(head):
        tags.append("report")
    return sorted(tags)


def _register(tags: List[str], text: str) -> str:
    if "status_ask" in tags:
        return "status_ask"
    if "correction" in tags:
        return "pushback"
    if "report" in tags:
        return "report"
    if text.strip().endswith("?"):
        return "question"
    return "direction"


def classify(text: str) -> Dict:
    if not text.strip():
        return _result("drop", text, None)
    if text.startswith("<") or "system-reminder" in text[:40]:
        return _result("drop", text, None)
    if text.startswith(_DROP_PREFIXES):
        return _result("drop", text, None)
    command = text.split()[0]
    if _COMMAND_RE.match(command):
        return _result("machine", text, None, command=command)
    stripped = text.lstrip()
    if _IMAGE_ONLY_RE.match(stripped):
        return _result("prompt", text, None, tags=["image_only"], image_only=True)
    pasted = len(text) > LONG_TEXT or _paste_lines(text) >= _PASTE_LINES
    if not pasted and _ACK_RE.match(stripped):
        return _result("prompt", text, text[:TEXT_CAP], register="ack")
    tags = _tags(stripped[:80].lower())
    if pasted:
        tags = sorted(tags + ["pasted"])
    return _result(
        "prompt", text, text[:PASTED_CAP] if pasted else text[:TEXT_CAP],
        tags=tags, register=_register(tags, text), pasted=pasted,
        strong_marker=bool(_STRONG_RE.search(text.lower())))
