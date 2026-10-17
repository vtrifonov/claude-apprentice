from __future__ import annotations

import json
import re

from apprentice.scrub import scrub

if False:  # annotations only: importing typing costs every hook start
    from typing import Dict, Iterator, List, Optional, Tuple

PAIR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"="((?:[^"\\]|\\.)*)"')

_ANCHOR_CHARS = 400
_INTERRUPT_PREFIX = "[Request interrupted by user"
_RECOMMENDED_RE = re.compile(r"\(recommended\)", re.IGNORECASE)
_READ_CHUNK = 64 * 1024


def message_text(line_obj: dict) -> Optional[str]:
    message = line_obj.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        if parts:
            return "\n".join(parts)
    return None


def _empty_context() -> dict:
    return {"prev_assistant": None, "after_interrupt": False, "entrypoint": None}


def _read_tail(path: str, nbytes: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        offset = max(0, size - nbytes)
        fh.seek(offset)
        data = fh.read(size - offset)
    if offset > 0:
        newline = data.find(b"\n")
        data = b"" if newline < 0 else data[newline + 1:]
    return data


def _anchor(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    clean = scrub(" ".join(text.split()))[0]
    if not clean:
        return None
    return clean[-_ANCHOR_CHARS:]


def tail_context(path: str, current_prompt: Optional[str] = None, nbytes: int = 65536) -> dict:
    ctx = _empty_context()
    try:
        data = _read_tail(path, nbytes)
    except (OSError, ValueError):
        return ctx
    have_assistant = False
    have_user = False
    have_entry = False
    first_user_seen = False
    for raw in reversed(data.split(b"\n")):
        if have_assistant and have_user and have_entry:
            break
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if not have_entry and isinstance(obj.get("entrypoint"), str) and obj["entrypoint"]:
            ctx["entrypoint"] = obj["entrypoint"]
            have_entry = True
        kind = obj.get("type")
        if kind == "assistant" and not have_assistant:
            # one content block per line: walk back past tool_use/thinking lines to the newest text
            text = message_text(obj)
            if text is None:
                continue
            ctx["prev_assistant"] = _anchor(text)
            have_assistant = True
        elif kind == "user" and not have_user:
            text = message_text(obj)
            if text is None:
                continue
            if not first_user_seen:
                first_user_seen = True
                if current_prompt is not None and text.strip() == current_prompt.strip():
                    continue
            ctx["after_interrupt"] = text.startswith(_INTERRUPT_PREFIX)
            have_user = True
    return ctx


class LineStream(object):
    def __init__(self, path: str, offset: int = 0, max_line: int = 8 * 1024 * 1024):
        self.path = path
        self.offset = offset
        self.max_line = max_line
        self.skipped_long = 0

    def __iter__(self) -> Iterator[Tuple[int, bytes]]:
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            pos = self.offset
            parts: List[bytes] = []
            size = 0
            overflow = False
            while True:
                chunk = fh.readline(_READ_CHUNK)
                if not chunk:
                    return
                pos += len(chunk)
                if not overflow:
                    parts.append(chunk)
                    size += len(chunk)
                    if size > self.max_line:
                        overflow = True
                        parts = []
                if not chunk.endswith(b"\n"):
                    continue
                if overflow:
                    self.skipped_long += 1
                    line = None
                else:
                    line = b"".join(parts)
                parts = []
                size = 0
                overflow = False
                self.offset = pos
                if line is not None:
                    yield pos, line


def parse_ask_answer(tool_response) -> Optional[Dict[str, str]]:
    if isinstance(tool_response, dict):
        answers = tool_response.get("answers")
        if isinstance(answers, dict):
            return {str(k): str(v) for k, v in answers.items()}
        if isinstance(answers, list):
            out = {}
            for item in answers:
                if isinstance(item, dict) and "question" in item and "answer" in item:
                    out[str(item["question"])] = str(item["answer"])
            return out or None
        return None
    if isinstance(tool_response, list):
        texts = [b["text"] for b in tool_response if isinstance(b, dict) and isinstance(b.get("text"), str)]
        if not texts:
            return None
        return parse_ask_answer("".join(texts))
    if isinstance(tool_response, str):
        pairs = {q.replace('\\"', '"'): a.replace('\\"', '"') for q, a in PAIR_RE.findall(tool_response)}
        return pairs or None
    return None


def _norm(label: str) -> str:
    return " ".join(_RECOMMENDED_RE.sub("", label).split()).lower()


def map_answer(answer: Optional[str], question: dict) -> Optional[dict]:
    if answer is None:
        return None
    options = [o for o in question.get("options") or [] if isinstance(o, dict) and isinstance(o.get("label"), str)]
    by_norm = {}
    recommended = set()
    for opt in options:
        # match on the normalized form, but store the label exactly as options[].label has it
        key = _norm(opt["label"])
        by_norm[key] = opt["label"]
        if _RECOMMENDED_RE.search(opt["label"]):
            recommended.add(key)
    keys: List[str] = []
    whole = _norm(answer)
    if whole in by_norm:
        keys = [whole]
    elif question.get("multiSelect"):
        parts = [_norm(p) for p in answer.split(", ")]
        if parts and all(p in by_norm for p in parts):
            keys = parts
    if not keys:
        return {"labels": [], "free_text": answer, "is_other": True, "followed_recommended": None}
    followed = None
    if recommended:
        followed = all(k in recommended for k in keys)
    return {"labels": [by_norm[k] for k in keys], "free_text": None, "is_other": False, "followed_recommended": followed}
