"""Tone metrics and exemplars over eligible prompts (distill side; the hook path never imports this).

Group rule: every number is computed over all prompts of every account group. A list entry (opener, n-gram,
top word, typo) and every example snippet is kept only when it also occurs in a prompt the current group may
see, so no other group's verbatim text reaches the output. There is no word-frequency list: rarity is
document frequency over the prompts of this run, and spelling uses the optional system dictionary."""
from __future__ import annotations

import math
import re
import string
from collections import Counter
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from apprentice import cluster, config, lexicon
from apprentice.scrub import mask_outbound

PRECISION = 6
OPENER_LIMIT = 15
NGRAM_LIMIT = 20
TOP_WORD_LIMIT = 50
EXAMPLES_PER_PATTERN = 2
TTR_TOKENS = 5000
PER_REGISTER = 5
JOINS = (" so ", "->", " and ", " but ")
SUBORDINATORS = re.compile(r"\b(?:which|although|unless|whether)\b")

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")
_TYPO_RE = re.compile(r"^[a-z]{4,}$")
_TYPO_STRIP = string.punctuation + "‘’“”«»…"
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_INNER_SENTENCE_END_RE = re.compile(r"[.!?]\s+\S")
_YES_RE = re.compile(r"^\W*(?:go|yep|sure|good|let's do that|approve|post)\b")
_NO_RE = re.compile(r"^\W*(?:no need of|let's skip|the fix is different)")
_PUSHBACK_RE = re.compile(r"\b(?:but why|can't we|shouldn't we|are you sure|i thought we've|but i said)\b")
_DIRECTIVE_RE = re.compile(r"\b(?:let's|please)\s+\w+")
_NUMBERED_RE = re.compile(r"^q?\d+\s*[-:]|^[a-d]\b(?:\s*[,)\-:.]|\s*$)")
_QUESTION_WORDS = frozenset(
    "what why how when where who which can could should shall would is are do does did will have has am was were"
    .split())
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\w\s+[,;:.!?](?=\s|$)")
_GREETING_RE = re.compile(r"^\W*(?:hi|hello|hey|good morning|good afternoon|good evening)\b")
_SIGNOFF_RE = re.compile(r"\b(?:thanks|thank you|thx|cheers|regards)\W*$")
_CURLY = "‘’“”"
_EMOJI_RANGES = ((0x1F300, 0x1FAFF), (0x2600, 0x27BF))
_MISSING_ARTICLE_RE = re.compile(r"\b(?:is|has|add|create)\s+(?!a |an |the )([a-z]+)\b", re.IGNORECASE)
_PATTERNS = (
    ("informations", re.compile(r"\binformations\b", re.IGNORECASE)),
    ("on_the_other_hands", re.compile(r"\bon the other hands\b", re.IGNORECASE)),
    ("prepositions", re.compile(r"\b(?:discuss about|explain me|depends of|in the end of)\b", re.IGNORECASE)),
)
_PATTERN_ORDER = ("missing_article", "on_the_other_hands", "informations", "prepositions")


def _r(value: float) -> float:
    return round(value, PRECISION)


def _ratio(part: float, whole: float) -> float:
    return _r(part / whole) if whole else 0.0


def _text(prompt: dict) -> str:
    return prompt.get("text") or ""


def eligible_prompts(events: Iterable[dict], cfg: Optional[dict] = None) -> List[dict]:
    """Prompts usable for tone: not pasted, image-only or duplicates, from a capturing (or unknown) entrypoint."""
    entrypoints = (cfg or config.DEFAULTS)["capture_entrypoints"]
    out = []
    for event in events:
        if event.get("kind") != "prompt" or event.get("text") is None:
            continue
        tags = event.get("tags") or []
        if "pasted" in tags or "image_only" in tags or event.get("pasted") or event.get("image_only"):
            continue
        if event.get("duplicate_of"):
            continue
        entrypoint = event.get("entrypoint")
        if entrypoint is not None and entrypoint not in entrypoints:
            continue
        out.append(event)
    return out


def _percentile(sorted_values: List[int], pct: int) -> int:
    rank = -(-pct * len(sorted_values) // 100)  # nearest rank, ceiling
    return sorted_values[max(rank, 1) - 1]


def _length_stats(counts: List[int]) -> Dict[str, int]:
    ordered = sorted(counts)
    if not ordered:
        return {"p50": 0, "p90": 0, "max": 0}
    return {"p50": _percentile(ordered, 50), "p90": _percentile(ordered, 90), "max": ordered[-1]}


def _joins(text: str) -> int:
    lowered = text.lower()
    return lowered.count(",") + sum(lowered.count(join) for join in JOINS)


def _is_run_on(text: str) -> bool:
    return _joins(text) >= 2 and not _INNER_SENTENCE_END_RE.search(text)


def _first_word(text: str) -> str:
    words = text.split()
    return words[0].strip(string.punctuation).lower() if words else ""


def _content_sequence(text: str) -> List[str]:
    stop = lexicon.stopwords()
    return [tok for tok in cluster._CONTENT_TOKEN_RE.findall(text.lower()) if tok not in stop]


def _ngrams(tokens: List[str], size: int) -> List[str]:
    return [" ".join(tokens[i:i + size]) for i in range(len(tokens) - size + 1)]


def _ranked(counter: Counter, allowed: Set[str], limit: int) -> List[dict]:
    items = sorted(((text, count) for text, count in counter.items() if text in allowed),
                   key=lambda item: (-item[1], item[0]))
    return [{"text": text, "count": count} for text, count in items[:limit]]


def _is_emoji(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _EMOJI_RANGES)


def _punctuation(texts: List[str]) -> dict:
    total = len(texts)
    lowercase = 0
    questions = 0
    answered = 0
    for text in texts:
        first = next((c for c in text if c.isalpha()), None)
        if first is not None and first.islower():
            lowercase += 1
        if "?" in text or _first_word(text) in _QUESTION_WORDS:
            questions += 1
            if "?" in text:
                answered += 1
    joined = "\n".join(texts)
    return {
        "lowercase_opener_rate": _ratio(lowercase, total),
        "final_period_rate": _ratio(sum(1 for t in texts if t.rstrip().endswith(".")), total),
        "question_mark_rate_on_questions": _ratio(answered, questions),
        "exclamation_rate": _ratio(sum(1 for t in texts if "!" in t), total),
        "space_before_punct_rate": _ratio(sum(1 for t in texts if _SPACE_BEFORE_PUNCT_RE.search(t)), total),
        "curly_quotes": sum(joined.count(c) for c in _CURLY),
        "emoji_count": sum(1 for c in joined if _is_emoji(c)),
        "greetings_count": sum(1 for t in texts if _GREETING_RE.search(t.lower())),
        "signoffs_count": sum(1 for t in texts if _SIGNOFF_RE.search(t.lower())),
    }


def _registers(prompts: List[dict]) -> dict:
    out = {"yes": 0, "no": 0, "pushback": 0, "status_ask": 0, "directive": 0, "numbered": 0}
    for prompt in prompts:
        text = _text(prompt).lower().replace("’", "'").strip()
        out["yes"] += bool(_YES_RE.search(text))
        out["no"] += bool(_NO_RE.search(text))
        out["pushback"] += bool(_PUSHBACK_RE.search(text))
        out["status_ask"] += prompt.get("register") == "status_ask"
        out["directive"] += bool(_DIRECTIVE_RE.search(text))
        out["numbered"] += bool(_NUMBERED_RE.search(text))
    return out


def _repo_pieces(prompts: List[dict]) -> Set[str]:
    pieces = set()  # type: Set[str]
    for prompt in prompts:
        for value in (prompt.get("repo"), prompt.get("cwd")):
            if value:
                pieces.update(piece for piece in re.split(r"[/\-_.]", value.lower()) if piece)
    return pieces


def _typos(prompts: List[dict], visible_prompts: List[dict]) -> Tuple[Optional[List[dict]], Optional[str]]:
    if lexicon.dictionary() is None:
        return None, "typo detection skipped: no system dictionary at %s" % lexicon.DICT_PATH
    skip = _repo_pieces(prompts)
    counts = Counter()  # type: Counter
    for prompt in visible_prompts:
        for raw in _text(prompt).split():
            token = raw.strip(_TYPO_STRIP)
            if _TYPO_RE.match(token) and token not in skip and lexicon.known_word(token) is False:
                counts[token] += 1
    found = sorted(((word, count) for word, count in counts.items() if count >= 2), key=lambda i: (-i[1], i[0]))
    return [{"word": word, "count": count} for word, count in found], None


def _missing_article_hits(text: str) -> List[str]:
    stop = lexicon.stopwords()
    hits = []
    for match in _MISSING_ARTICLE_RE.finditer(text):
        word = match.group(1).lower()
        if word not in stop and not word.endswith("s") and lexicon.known_word(word) is True:
            hits.append(match.group(0))
    return hits


def _patterns(prompts: List[dict], visible_flags: List[bool], dictionary_present: bool) -> List[dict]:
    finders = {name: (lambda text, rx=rx: [m.group(0) for m in rx.finditer(text)]) for name, rx in _PATTERNS}
    if dictionary_present:
        finders["missing_article"] = _missing_article_hits
    out = []
    for name in _PATTERN_ORDER:
        finder = finders.get(name)
        if finder is None:
            continue
        count = 0
        examples = []  # type: List[str]
        for prompt, can_see in zip(prompts, visible_flags):
            hits = finder(_text(prompt))
            count += len(hits)
            if can_see:
                examples.extend(mask_outbound(hit) for hit in hits[:EXAMPLES_PER_PATTERN])
        if count:
            out.append({"name": name, "count": count, "examples": examples[:EXAMPLES_PER_PATTERN]})
    return out


def _rarity_share(prompts: List[dict]) -> float:
    per_prompt = [cluster.content_tokens(_text(prompt)) for prompt in prompts]
    frequency = Counter(token for tokens in per_prompt for token in tokens)
    total = sum(len(tokens) for tokens in per_prompt)
    return _ratio(sum(1 for count in frequency.values() if count == 1), total)


def compute_metrics(prompts: List[dict], visible: Callable[[dict], bool], cfg: Optional[dict] = None) -> dict:
    """Statistics over `prompts`. `ack` prompts count only toward registers (counts, length by register,
    lexicon); every other statistic uses the remaining prompts."""
    flags = [bool(visible(prompt)) for prompt in prompts]
    body = [prompt for prompt in prompts if prompt.get("register") != "ack"]
    body_visible = [prompt for prompt in body if visible(prompt)]
    texts = [_text(prompt) for prompt in body]
    word_counts = [len(text.split()) for text in texts]

    by_register_words = {}  # type: Dict[str, List[int]]
    for prompt in prompts:
        by_register_words.setdefault(prompt.get("register") or "unknown", []).append(len(_text(prompt).split()))

    joins = [_joins(text) for text in texts]
    sentences = [[s for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()] for text in texts]
    sentence_total = sum(len(parts) for parts in sentences)
    sentence_words = sum(len(s.split()) for parts in sentences for s in parts)
    letters = [len(word) for text in texts for word in _WORD_RE.findall(text)]
    sequence = [word.lower() for text in texts for word in _WORD_RE.findall(text)][:TTR_TOKENS]

    visible_openers = {_first_word(_text(p)) for p in body_visible}
    visible_content = [_content_sequence(_text(p)) for p in body_visible]
    visible_words = {tok for toks in visible_content for tok in toks}
    visible_grams = {size: {g for toks in visible_content for g in _ngrams(toks, size)} for size in (2, 3)}
    content = [_content_sequence(text) for text in texts]
    typos, typos_note = _typos(prompts, [p for p, can_see in zip(prompts, flags) if can_see])

    return {
        "counts": {
            "all": len(prompts),
            "visible": sum(flags),
            "by_register": {name: len(values) for name, values in by_register_words.items()},
        },
        "length": {
            "overall": _length_stats(word_counts),
            "by_register": {name: _length_stats(values) for name, values in by_register_words.items()},
        },
        "clauses": {
            "mean": _ratio(sum(j + 1 for j in joins), len(texts)),
            "run_on_share": _ratio(sum(1 for text in texts if _is_run_on(text)), len(texts)),
        },
        "avg_word_len": _ratio(sum(letters), len(letters)),
        "ttr_5000": _ratio(len(set(sequence)), len(sequence)),
        "registers": _registers(prompts),
        "openers": _ranked(Counter(_first_word(text) for text in texts), visible_openers, OPENER_LIMIT),
        "bigrams": _ranked(Counter(g for toks in content for g in _ngrams(toks, 2)), visible_grams[2], NGRAM_LIMIT),
        "trigrams": _ranked(Counter(g for toks in content for g in _ngrams(toks, 3)), visible_grams[3],
                            NGRAM_LIMIT),
        "top_words": _ranked(Counter(tok for toks in content for tok in toks), visible_words, TOP_WORD_LIMIT),
        "punctuation": _punctuation(texts),
        "spelling": {
            "typos": typos,
            "typos_note": typos_note,
            "patterns": _patterns(prompts, flags, typos is not None),
        },
        "proficiency_inputs": {
            "sentence_len_mean": _ratio(sentence_words, sentence_total),
            "clause_density": _ratio(sum(j + 1 for j in joins), sentence_total),
            "rarity_share": _rarity_share(prompts),
            "subordination_per_100": _r(100.0 * sum(len(SUBORDINATORS.findall(t.lower())) for t in texts)
                                        / sum(word_counts)) if sum(word_counts) else 0.0,
        },
    }


def select_exemplars(prompts: List[dict], visible: Callable[[dict], bool], n: int = 30) -> Tuple[List[dict], int]:
    """Up to `n` masked exemplars from visible prompts: the most informative (content tokens) five per register,
    then filled from the largest registers. Second value: eligible prompts of other groups left out."""
    left_out = sum(1 for prompt in prompts if not visible(prompt))
    seen = set()  # type: Set[str]
    pools = {}  # type: Dict[str, List[tuple]]
    for prompt in prompts:
        text = prompt.get("text")
        if not visible(prompt) or not text or "email" in (prompt.get("flags") or []):
            continue
        digest = prompt.get("sha1") or text
        if digest in seen:
            continue
        seen.add(digest)
        masked = mask_outbound(text)
        entry = {"text": masked, "date": (prompt.get("ts") or "")[:10], "repo": prompt.get("repo"),
                 "register": prompt.get("register")}
        rank = (-len(cluster.content_tokens(masked)), -len(masked), prompt.get("ts") or "", prompt.get("id") or "")
        pools.setdefault(prompt.get("register") or "unknown", []).append((rank, entry))
    order = sorted(pools, key=lambda name: (-len(pools[name]), name))
    ranked = {name: [entry for _, entry in sorted(pools[name], key=lambda item: item[0])] for name in order}
    chosen = []  # type: List[dict]
    for name in order:
        chosen.extend(ranked[name][:PER_REGISTER])
    remaining = {name: ranked[name][PER_REGISTER:] for name in order}
    while len(chosen) < n and any(remaining.values()):
        for name in order:
            if remaining[name] and len(chosen) < n:
                chosen.append(remaining[name].pop(0))
    return chosen[:n], left_out
