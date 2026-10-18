"""Tone metrics and exemplars over eligible prompts (distill side; the hook path never imports this).

Group rule: every number is computed over all prompts of every account group. A list entry (opener, n-gram,
top word, typo) and every example snippet is kept only when it also occurs in a prompt the current group may
see, so no other group's verbatim text reaches the output. There is no word-frequency list: rarity is
document frequency over the prompts of this run, and spelling uses the optional system dictionary.

The profile files are per account group (`profile/tone-<slug>.json` and `.md`); SessionStart names only the
current group's file, and the global pointer rule tells the session to use that file and no other."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import string
from collections import Counter
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from apprentice import apply, cluster, config, distill, events, lexicon, tmpfiles
from apprentice.scrub import EMAIL_RE, TICKET_RE, UUID_RE, mask_outbound, scrub

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
_SEQ_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_'-]*")
_TYPO_RE = re.compile(r"^[a-z]{4,}$")
_TYPO_STRIP = string.punctuation + "‘’“”«»…"
_CODE_START = ("_", ".", "/", "~", "-", "`")
_CODE_INNER = ("/", "_", "`")
# emails, UUIDs and ticket keys never enter a list entry (mask_outbound and the ticket scrub would change them)
_SKIP_RES = (EMAIL_RE, UUID_RE, TICKET_RE)
_CUT = "\x00"
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


def _segments(text: str) -> List[str]:
    """`text` cut at every email, UUID and ticket key, which are dropped; nothing joins across a cut."""
    for regex in _SKIP_RES:
        text = regex.sub(_CUT, text)
    return text.split(_CUT)


def _opener(text: str) -> str:
    """The first word that is not an email, UUID or ticket key, stripped of punctuation ("" when none)."""
    for segment in _segments(text):
        words = segment.split()
        if words:
            return words[0].strip(_TYPO_STRIP).lower()
    return ""


def _content_sequence(text: str) -> List[str]:
    stop = lexicon.stopwords()
    return [tok for segment in _segments(text) for tok in cluster._CONTENT_TOKEN_RE.findall(segment.lower())
            if tok not in stop]


def _text_ngrams(text: str, size: int) -> List[str]:
    """N-grams over the verbatim lowercase word sequence of each segment, kept when one word is not a stopword."""
    stop = lexicon.stopwords()
    out = []
    for segment in _segments(text):
        words = _SEQ_WORD_RE.findall(segment.lower())
        for i in range(len(words) - size + 1):
            gram = words[i:i + size]
            if any(word not in stop for word in gram):
                out.append(" ".join(gram))
    return out


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


def _code_like(raw: str) -> bool:
    """A path, flag, dotfile, snake_case or backticked token: never a typo."""
    return raw.startswith(_CODE_START) or any(mark in raw for mark in _CODE_INNER)


def _typos(skip: Set[str], visible_prompts: List[dict]) -> Tuple[Optional[List[dict]], Optional[str]]:
    if lexicon.dictionary() is None:
        return None, "typo detection skipped: no system dictionary at %s" % lexicon.DICT_PATH
    counts = Counter()  # type: Counter
    for prompt in visible_prompts:
        for raw in _text(prompt).split():
            if _code_like(raw):
                continue
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


def compute_metrics(prompts: List[dict], visible: Callable[[dict], bool], cfg: Optional[dict] = None,
                    repo_pieces: Optional[Set[str]] = None) -> dict:
    """Statistics over `prompts`. `ack` prompts count only toward registers (counts, length by register,
    lexicon); every other statistic uses the remaining prompts. `repo_pieces` (repo and cwd pieces of every
    event, used only to exclude typos) adds to the pieces of `prompts` themselves."""
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

    visible_openers = {_opener(_text(p)) for p in body_visible}
    visible_words = {tok for p in body_visible for tok in _content_sequence(_text(p))}
    visible_grams = {size: {g for p in body_visible for g in _text_ngrams(_text(p), size)} for size in (2, 3)}
    content = [_content_sequence(text) for text in texts]
    skip = _repo_pieces(prompts) | set(repo_pieces or ())
    typos, typos_note = _typos(skip, [p for p, can_see in zip(prompts, flags) if can_see])

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
        "openers": _ranked(Counter(o for o in (_opener(text) for text in texts) if o), visible_openers,
                           OPENER_LIMIT),
        "bigrams": _ranked(Counter(g for text in texts for g in _text_ngrams(text, 2)), visible_grams[2],
                           NGRAM_LIMIT),
        "trigrams": _ranked(Counter(g for text in texts for g in _text_ngrams(text, 3)), visible_grams[3],
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


# --- profile files, model, refresh and the pointer rule ---

TONE_POINTER_ID = "r_" + hashlib.sha1(b"tone-pointer").hexdigest()[:8]
LOW_SAMPLE = 50
BANDS = ("A1", "A2", "B1", "B2", "C1", "C2")
JUSTIFICATION_CHARS = 2000
REASSESS_SHIFT = 0.10
DISPLAY_NAME_CHARS = 100
_INPUT_ORDER = ("sentence_len_mean", "clause_density", "rarity_share", "subordination_per_100")
_DRAFTING = [
    "Use this when writing text the user will send as their own (chat replies, review comments, ticket notes).",
    "",
    "- One to three lowercase lines.",
    "- Start with the verb, or with \"can you\".",
    "- One constraint per message, plus a link when there is one.",
    "- Argue from consistency with what exists or from prod reality, not from preference.",
    "- No greetings, no thanks, no emoji.",
    "- No deliberate typos. When quoting the user, keep their typos as written.",
]
_TALKING = [
    "- Answer a status ask in one line.",
    "- Recommendation first, then the reasons.",
    "- Plain words at their level (see the proficiency estimate), no process jargon.",
    "- Never correct or comment on their spelling.",
]


class ToneError(Exception):
    """A refusal the script reports on stderr with exit `code`."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def pointer_rule_text(display_name: str) -> str:
    return ("**When drafting text as %s or answering them, follow the apprentice tone file named in this session's "
            "SessionStart line.** With no such line in context, use no tone file, and never open another "
            "`tone-*.md` file." % display_name)


def profile_paths(home: str, group: str) -> Tuple[str, str]:
    base = os.path.join(home, "profile", "tone-%s" % config.group_slug(group))
    return base + ".json", base + ".md"


def _current_group(home: str, env: Mapping, cfg: dict) -> Optional[str]:
    """The current profile's group from the existing labels (read only)."""
    return config.group_of_profile(config.current_profile(env), cfg, config.read_group_labels(home))


def build_profile(home: str, env: Mapping, cfg: dict, real_home: str) -> dict:
    """The `tone-<group>.json` content for the current profile's account group. Labels profiles like distill does
    (the two are the only label writers). `real_home` is accepted for symmetry with distill and unused."""
    raw = list(events.iter_events(home))
    profile = config.current_profile(env)
    labels = distill.label_profiles(home, raw, cfg)
    if profile != "unknown" and profile not in labels and profile not in distill._grouped_profiles(cfg):
        labels = config.assign_group_labels(home, {profile: config.utc_now_iso()})
    group = config.group_of_profile(profile, cfg, labels)
    prompts = eligible_prompts(distill.clean(raw, cfg, group, labels), cfg)

    def visible(prompt: dict) -> bool:
        return bool(prompt.get("text_visible"))

    exemplars, left_out = select_exemplars(prompts, visible)
    return {
        "group": group,
        "group_profiles": config.group_profiles(group, cfg, labels) if group else [],
        "unknown_text_used": config.text_visible("unknown", group, cfg, labels),
        "prompts_group": sum(1 for prompt in prompts if visible(prompt)),
        "prompts_all": len(prompts),
        "low_sample": len(prompts) < LOW_SAMPLE,
        "generated_at": config.utc_now_iso(),
        # pieces of every event's repo and cwd, any kind and any group: an exclusion set only, never output
        "metrics": compute_metrics(prompts, visible, cfg, repo_pieces=_repo_pieces(raw)),
        "exemplars": exemplars,
        "exemplars_left_out": left_out,
        "model": {},
    }


def _fmt(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return str(value).lower()
    if isinstance(value, float):
        return ("%.2f" % value).rstrip("0").rstrip(".") or "0"
    return str(value)


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def _ranked_line(entries: List[dict], key: str = "text") -> str:
    return ", ".join("`%s` (%d)" % (_one_line(entry[key]).replace("`", "'"), entry["count"]) for entry in entries) \
        or "none"


def _table(header: Tuple[str, ...], rows: List[Tuple[Any, ...]]) -> List[str]:
    out = ["| %s |" % " | ".join(header), "|%s" % ("---|" * len(header))]
    out.extend("| %s |" % " | ".join(_fmt(cell) for cell in row) for row in rows)
    return out


def _stats(profile: dict) -> List[str]:
    metrics = profile["metrics"]
    overall = metrics["length"]["overall"]
    rows = [
        ("prompts, all groups", metrics["counts"]["all"]),
        ("prompts, this group", metrics["counts"]["visible"]),
        ("words per prompt p50 / p90 / max", "%d / %d / %d" % (overall["p50"], overall["p90"], overall["max"])),
        ("clauses per prompt", metrics["clauses"]["mean"]),
        ("run-on share", metrics["clauses"]["run_on_share"]),
        ("average word length", metrics["avg_word_len"]),
        ("type-token ratio (first 5,000 words)", metrics["ttr_5000"]),
    ]
    return ["## Stats", ""] + _table(("measure", "value"), rows)


def _registers_section(metrics: dict) -> List[str]:
    by_register = metrics["length"]["by_register"]
    rows = [(name, metrics["counts"]["by_register"].get(name, 0),
             "%d / %d / %d" % (stats["p50"], stats["p90"], stats["max"]))
            for name, stats in sorted(by_register.items())]
    lexicon_rows = sorted(metrics["registers"].items())
    return (["## Registers", ""] + _table(("register", "prompts", "words p50 / p90 / max"), rows)
            + ["", "Phrase signals:", ""] + _table(("signal", "prompts"), lexicon_rows))


def _vocabulary(metrics: dict) -> List[str]:
    return ["## Vocabulary and phrases", "",
            "- Openers: " + _ranked_line(metrics["openers"]),
            "- Top words: " + _ranked_line(metrics["top_words"]),
            "- Two-word phrases: " + _ranked_line(metrics["bigrams"]),
            "- Three-word phrases: " + _ranked_line(metrics["trigrams"])]


def _punctuation_section(metrics: dict) -> List[str]:
    rows = sorted(metrics["punctuation"].items())
    return ["## Punctuation and casing", ""] + _table(("measure", "value"), rows)


def _spelling(metrics: dict) -> List[str]:
    spelling = metrics["spelling"]
    out = ["## Spelling and patterns (observed patterns)", "",
           "Recorded so drafts keep the user's voice, not to correct it.", ""]
    if spelling["typos"] is None:
        out.append("- Recurring typos: %s" % spelling["typos_note"])
    else:
        out.append("- Recurring typos: " + _ranked_line(spelling["typos"], "word"))
    for pattern in spelling["patterns"]:
        examples = "; ".join("`%s`" % _one_line(example).replace("`", "'") for example in pattern["examples"])
        out.append("- %s: %d%s" % (pattern["name"].replace("_", " "), pattern["count"],
                                   " (e.g. %s)" % examples if examples else ""))
    return out


def _proficiency(profile: dict) -> List[str]:
    inputs = profile["metrics"]["proficiency_inputs"]
    rows = [(name, inputs.get(name)) for name in _INPUT_ORDER]
    out = ["## Proficiency estimate", ""] + _table(("input", "value"), rows) + [""]
    model = profile.get("model") or {}
    if model.get("proficiency_band"):
        out.append("Estimated band: **%s** (an estimate from the inputs above, not a test result). %s"
                   % (model["proficiency_band"], _one_line(model.get("proficiency_justification"))))
    else:
        out.append("Estimated band: not yet assessed (an estimate, filled in by /apprentice:tone).")
    if profile.get("reassess_band"):
        out.extend(["", "The inputs moved more than 10% since the last build: reassess the band."])
    return out


def _exemplars(profile: dict) -> List[str]:
    out = ["## Exemplars", ""]
    for entry in profile["exemplars"]:
        where = ", ".join(part for part in (entry.get("date"), entry.get("repo"), entry.get("register")) if part)
        out.append("- \"%s\" (%s)" % (_one_line(entry["text"]), where))
    if not profile["exemplars"]:
        out.append("No exemplars from this account group.")
    if profile.get("exemplars_left_out"):
        out.extend(["", "%d prompts from other account groups left out (text withheld)."
                    % profile["exemplars_left_out"]])
    return out


def render_md(profile: dict) -> str:
    out = ["# Writing profile: account group %s" % (profile.get("group") or "none"), ""]
    if profile.get("low_sample"):
        out.extend(["> **Low sample:** %d prompts across all groups (under %d). Treat every number as rough."
                    % (profile["prompts_all"], LOW_SAMPLE), ""])
    if profile.get("group_profiles"):
        out.append("Profiles in this group: %s." % ", ".join(profile["group_profiles"]))
    out.append("Generated %s from %d prompts in this group and %d across all groups. Numbers use every group; "
               "text comes only from this group." % (profile.get("generated_at"), profile["prompts_group"],
                                                      profile["prompts_all"]))
    if not profile["prompts_group"]:
        out.append("No prompts from this account group are visible: add this profile to `account_groups`, or set "
                   "`unknown_group` for backfilled prompts, in config.json.")
    metrics = profile["metrics"]
    for section in (_stats(profile), _registers_section(metrics), _vocabulary(metrics),
                    _punctuation_section(metrics), _spelling(metrics), _proficiency(profile), _exemplars(profile)):
        out.append("")
        out.extend(section)
    prose = (profile.get("model") or {}).get("prose")
    if prose:
        out.extend(["", "## How to use", "", prose.strip()])
    else:
        out.extend(["", "## How to use: drafting as the user", ""] + _DRAFTING)
        out.extend(["", "## How to use: talking to the user", ""] + _TALKING)
    return "\n".join(out) + "\n"


def _write_private(path: str, text: str) -> None:
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def _dump(profile: dict) -> str:
    return json.dumps(profile, ensure_ascii=False, indent=2) + "\n"


def write_profile(home: str, profile: dict) -> Tuple[str, str]:
    """Write `profile/tone-<slug>.json` then `.md`; return both paths."""
    if not profile.get("group"):
        raise ToneError("no account group for the current profile (state/group-labels.json unreadable?)")
    config.ensure_dirs(home)
    json_path, md_path = profile_paths(home, profile["group"])
    _write_private(json_path, _dump(profile))
    _write_private(md_path, render_md(profile))
    return json_path, md_path


def _load_json(path: str, what: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ToneError("cannot read %s %s: %s" % (what, path, exc.__class__.__name__))
    if not isinstance(data, dict):
        raise ToneError("%s %s is not a JSON object" % (what, path))
    return data


def _validated_model(data: dict, cfg: dict) -> dict:
    band = data.get("proficiency_band")
    if band not in BANDS:
        raise ToneError("proficiency_band must be one of %s" % ", ".join(BANDS))
    justification = data.get("proficiency_justification")
    if not isinstance(justification, str) or not justification.strip():
        raise ToneError("proficiency_justification must be a non-empty string")
    if len(justification) > JUSTIFICATION_CHARS:
        raise ToneError("proficiency_justification is over %d characters" % JUSTIFICATION_CHARS)
    prose = data.get("prose")
    if prose is not None and not isinstance(prose, str):
        raise ToneError("prose must be a string")
    model = {"proficiency_band": band, "proficiency_justification": scrub(justification, cfg)[0]}
    if prose and prose.strip():
        model["prose"] = scrub(prose, cfg)[0]
    return model


def _read_model_file(home: str, model_file: str) -> dict:
    """The model file's JSON; it must lie under state/tmp and is deleted once read, whatever happens next."""
    try:
        raw, error = tmpfiles.read(home, model_file, "--set-model file")
    finally:
        tmpfiles.discard(home, model_file)
    if error:
        raise ToneError(error)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ToneError("cannot read model file %s: %s" % (model_file, exc.__class__.__name__))
    if not isinstance(data, dict):
        raise ToneError("model file %s is not a JSON object" % model_file)
    return data


def set_model(home: str, env: Mapping, cfg: dict, model_file: str) -> dict:
    """Store the model's band, justification and optional prose in the current group's JSON (every other key is
    left as it is, and any other key in `model_file` is ignored), then re-render the markdown. The band now
    reflects the current inputs: `reassess_band` is cleared and `model.assessed_inputs` keeps a copy of them,
    the baseline the next refresh compares against."""
    data = _read_model_file(home, model_file)
    group = _current_group(home, env, cfg)
    if not group:
        raise ToneError("no account group for the current profile: run tone.py without flags first")
    json_path, md_path = profile_paths(home, group)
    if not os.path.exists(json_path):
        raise ToneError("no tone profile for account group %s: run tone.py without flags first" % group)
    model = _validated_model(data, cfg)
    profile = _load_json(json_path, "tone profile")
    inputs = (profile.get("metrics") or {}).get("proficiency_inputs")
    model["assessed_inputs"] = dict(inputs) if isinstance(inputs, dict) else {}
    profile["model"] = model
    profile["reassess_band"] = False
    _write_private(json_path, _dump(profile))
    _write_private(md_path, render_md(profile))
    return profile


def _moved(old: Any, new: Any) -> bool:
    if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return old != new
    if old == 0:
        return new != 0
    return abs(new - old) > REASSESS_SHIFT * abs(old)


def refresh(home: str, env: Mapping, cfg: dict, real_home: str) -> dict:
    """Recompute the profile, keep the previous `model`, and set `reassess_band` when any proficiency input moved
    by more than 10% relative to the inputs the band was assessed on (`model.assessed_inputs`), or to the
    previous build when the model stores none."""
    profile = build_profile(home, env, cfg, real_home)
    previous = {}  # type: Dict[str, Any]
    if profile["group"]:
        json_path = profile_paths(home, profile["group"])[0]
        if os.path.exists(json_path):
            previous = _load_json(json_path, "tone profile")
    profile["model"] = previous.get("model") if isinstance(previous.get("model"), dict) else {}
    old_inputs = profile["model"].get("assessed_inputs")
    if not isinstance(old_inputs, dict):
        old_inputs = ((previous.get("metrics") or {}).get("proficiency_inputs") or {}) if previous else {}
    new_inputs = profile["metrics"]["proficiency_inputs"]
    profile["reassess_band"] = bool(old_inputs) and any(_moved(old_inputs.get(name), new_inputs.get(name))
                                                        for name in new_inputs)
    return profile


def display_name_set(home: str) -> bool:
    """True when config.json itself names a display_name (the default "the user" does not count)."""
    try:
        with open(os.path.join(home, "config.json"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and isinstance(data.get("display_name"), str) and bool(data["display_name"].strip())


def set_display_name(home: str, name: str) -> dict:
    """Merge `display_name` into config.json (temp file and replace); refuses a malformed config.json. A
    symlinked config.json stays a link: the write goes to its realpath."""
    clean = (name or "").strip()
    if not clean or "\n" in clean or "\r" in clean or len(clean) > DISPLAY_NAME_CHARS:
        raise ToneError("display name must be one non-empty line of at most %d characters" % DISPLAY_NAME_CHARS)
    os.makedirs(home, mode=0o700, exist_ok=True)
    path = os.path.realpath(os.path.join(home, "config.json"))
    data = _load_json(path, "config") if os.path.exists(path) else {}
    data["display_name"] = clean
    _write_private(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return data


def set_display_name_from_file(home: str, name_file: str) -> dict:
    """`set_display_name` with the name read from a file under state/tmp (one trailing newline dropped); the
    file is deleted once read, so typed text never reaches a command line or outlives its use."""
    try:
        raw, error = tmpfiles.read(home, name_file, "--display-name-file")
    finally:
        tmpfiles.discard(home, name_file)
    if error:
        raise ToneError(error)
    return set_display_name(home, raw[:-1] if raw.endswith("\n") else raw)


def configured_display_name(cfg: dict) -> str:
    """The pointer's name for the user; a missing, empty or non-string `display_name` counts as unset."""
    name = cfg.get("display_name")
    return name.strip() if isinstance(name, str) and name.strip() else config.DEFAULTS["display_name"]


def _creation_diff(path: str, line: str, cfg: dict) -> dict:
    """The dry-run outcome of installing `line` into a global CLAUDE.md that does not exist yet."""
    new, result = apply.upsert_rule("", line)
    diff = "".join(difflib.unified_diff([], new.splitlines(True), "/dev/null", path))
    info = apply.inspect_target(path, cfg)
    return {"result": result, "file": path, "line": line, "backup": None, "tracked": info["tracked"],
            "remote_visibility": info["visibility"], "root": info["root"], "remote": info["remote"],
            "diff": diff, "message": "global CLAUDE.md does not exist; installing creates it"}


def install_pointer(home: str, cfg: dict, real_home: str, confirm_tracked: bool, dry_run: bool) -> dict:
    """Install the tone pointer rule into the global CLAUDE.md through apply's global write path.

    The pointer has no pack, so it is exempt from the tracked-target pack requirement, but a tracked global
    still needs `confirm_tracked`. A missing global CLAUDE.md is created (0600) with the section: the user
    asked to install. Returns apply's outcome dict."""
    text = pointer_rule_text(configured_display_name(cfg))
    path = os.path.join(real_home, ".claude", "CLAUDE.md")
    created = False
    if not os.path.lexists(path):
        if dry_run:
            line = apply.render_line(TONE_POINTER_ID, scrub(text, cfg)[0] or "", 0, "manual",
                                     config.utc_now_iso()[:10])
            return _creation_diff(path, line, cfg)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        created = True
    args = argparse.Namespace(id=TONE_POINTER_ID, text=text, scope="global", target=None, pack=None,
                              confirm_tracked=confirm_tracked, dry_run=dry_run)
    outcome = apply.apply_candidate(args, home, cfg, real_home, allow_no_pack=True)
    if created and outcome["result"] not in ("written", "unchanged"):
        try:
            if os.path.getsize(path) == 0:
                os.unlink(path)
        except OSError:
            pass
    return outcome
