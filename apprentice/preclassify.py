"""Opt-in haiku pre-classification of correction pairs. Current profile only; never run from a hook."""
import json
import os
import re
import subprocess
from typing import Callable, Dict, List, Optional

from apprentice import cluster, config, distill, scrub

PROMPT_MARKER = "[apprentice-preclassify v1]"
PROMPT_TEMPLATE = (
    PROMPT_MARKER + "\n"
    "You classify prompts a user typed right after an assistant turn. For each pair below, decide whether the\n"
    "user prompt corrects, redirects or rejects what the assistant just did.\n"
    "Reply with only a JSON array, one object per pair id, no prose and no code fence. Each object has:\n"
    '  "id": the pair id, copied exactly\n'
    '  "is_correction": true or false\n'
    '  "topic": a lowercase slug of at most four words joined by hyphens, naming what was corrected\n'
    '  "rule_sketch": the standing rule the correction implies, at most 20 words\n'
    '  "confidence": a number from 0 to 1\n'
    "The pairs follow as a JSON array on the last line; each has id, user and assistant_before.\n"
    "{pairs}\n"
)

BATCH_SIZE = 40
MAX_CONSECUTIVE_FAILURES = 3
TIMEOUT_S = 120
MODEL = "haiku"
# --safe-mode with an empty tool list: no tools, MCP servers or other plugins' hooks in the child (probed with OAuth).
EXTRA_CHILD_ARGS = ["--no-session-persistence", "--safe-mode", "--tools", ""]  # type: List[str]
# added to a batch failure whose child output is empty or not JSON, the likeliest symptom of missing auth
SAFE_MODE_HINT = "child failed under --safe-mode; settings-based auth (apiKeyHelper/env) is not loaded in safe mode"
TOPIC_MAX_WORDS = 4
TOPIC_MAX_CHARS = 60
SKETCH_MAX_CHARS = 200

_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*[ \t]*\n(.*?)\n?```$", re.S)
_ID_OK_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
_REQUIRED = ("is_correction", "topic", "rule_sketch", "confidence")


def _cache_dir(home: str) -> str:
    return os.path.join(home, "state", "preclass")


def cache_path(home: str, event_id: str) -> str:
    return os.path.join(_cache_dir(home), event_id + ".json")


def _safe_id(event_id) -> bool:
    return isinstance(event_id, str) and bool(_ID_OK_RE.match(event_id))


def read_cache(home: str) -> Dict[str, dict]:
    out = {}  # type: Dict[str, dict]
    try:
        names = sorted(os.listdir(_cache_dir(home)))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(_cache_dir(home), name), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out[name[:-len(".json")]] = data
    return out


def write_cache(home: str, event_id: str, item: dict, model: str) -> None:
    folder = _cache_dir(home)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    record = dict(item)
    record["model"] = model
    record["ts"] = config.utc_now_iso()
    config.write_private(cache_path(home, event_id), json.dumps(record, ensure_ascii=False).encode("utf-8"))


def live_of(event: dict, profile: str) -> bool:
    """A live-captured event of `profile`. Backfilled events are never sent, whatever profile they carry."""
    return event.get("source") == "hook" and event.get("profile") == profile


def select_uncached(events: List[dict], home: str, profile: str, cfg: Optional[dict] = None) -> List[dict]:
    """Pairs of this profile's prompts with no cache file. Only prompts distill can build candidates from are
    eligible (`distill.clean`: no machine, pasted, image-only, duplicate, observer or other-entrypoint prompts,
    no backfill copies of live sessions). The profile filter runs before the pair limit."""
    if profile == "unknown":
        return []
    cfg = cfg if cfg is not None else config.load_config(home)
    mine = [event for event in distill.clean(events, cfg, None, {})
            if live_of(event, profile) and not event.get("count_only")]
    pairs = cluster.select_pairs(mine)
    return [pair for pair in pairs
            if _safe_id(pair["event"].get("id")) and isinstance(pair["event"].get("text"), str)
            and not os.path.exists(cache_path(home, pair["event"]["id"]))]


def _clean(text: Optional[str], cfg: dict) -> str:
    cleaned, _flags = scrub.scrub(text or "", cfg)
    return scrub.mask_outbound(cleaned or "")


def build_prompt(pairs: List[dict], cfg: dict) -> str:
    payload = [{"id": pair["event"]["id"], "user": _clean(pair["event"].get("text"), cfg),
                "assistant_before": _clean(pair["event"].get("prev_assistant"), cfg)} for pair in pairs]
    return PROMPT_TEMPLATE.replace("{pairs}", json.dumps(payload, ensure_ascii=False))


def _child_env(env) -> dict:
    child = dict(env)
    child["APPRENTICE_INTERNAL"] = "1"
    child["APPRENTICE_DISABLE"] = "1"
    child.pop("CLAUDE_CODE_ENTRYPOINT", None)
    return child


def _parse_envelope(stdout: str):
    """The verdict list, or a short reason string when the batch failed."""
    try:
        envelope = json.loads(stdout)
    except ValueError:
        return "malformed output"
    if not isinstance(envelope, dict):
        return "malformed output"
    if envelope.get("is_error") is True:
        return "child reported an error"
    result = envelope.get("result")
    if isinstance(result, str):
        text = result.strip()
        fenced = _FENCE_RE.match(text)
        if fenced:
            text = fenced.group(1).strip()
        try:
            result = json.loads(text)
        except ValueError:
            return "result is not JSON"
    if not isinstance(result, list):
        return "result is not an array"
    return result


def _valid_item(item, ids, cfg: dict) -> Optional[dict]:
    if not isinstance(item, dict) or item.get("id") not in ids:
        return None
    if any(key not in item for key in _REQUIRED):
        return None
    if not isinstance(item["is_correction"], bool):
        return None
    conf = item["confidence"]
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        return None
    if not isinstance(item["topic"], str) or not isinstance(item["rule_sketch"], str):
        return None
    if len(item["rule_sketch"].split()) > 20 or len(item["rule_sketch"]) > SKETCH_MAX_CHARS:
        return None
    slug = _SLUG_RE.sub("-", scrub.scrub(item["topic"], cfg)[0].strip().lower())
    words = [word for word in slug.split("-") if word]
    topic = "-".join(words[:TOPIC_MAX_WORDS])[:TOPIC_MAX_CHARS].strip("-")
    if not topic:
        return None
    return {"id": item["id"], "is_correction": item["is_correction"], "topic": topic,
            "rule_sketch": scrub.scrub(item["rule_sketch"], cfg)[0], "confidence": float(conf)}


def _run_child(binary: str, prompt: str, env, cwd: str) -> object:
    cmd = [binary, "-p", "--model", MODEL, "--output-format", "json"] + list(EXTRA_CHILD_ARGS)
    try:
        # bytes both ways: a lone surrogate in a prompt or non-UTF-8 output must not abort the run
        proc = subprocess.run(cmd, input=prompt.encode("utf-8", "replace"), capture_output=True,
                              timeout=TIMEOUT_S, env=_child_env(env), cwd=cwd)
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError:
        return "cannot run the claude binary"
    except Exception as exc:  # any other child failure only skips the batch
        return "child run failed (%s)" % exc.__class__.__name__
    stdout = proc.stdout.decode("utf-8", "replace")
    hint = "" if _is_json(stdout) else "; " + SAFE_MODE_HINT
    if proc.returncode != 0:
        # a limit failure still carries a JSON envelope; the exit status alone decides
        return "exit status %d%s" % (proc.returncode, hint)
    outcome = _parse_envelope(stdout)
    return outcome + hint if isinstance(outcome, str) else outcome


def _is_json(stdout: str) -> bool:
    try:
        json.loads(stdout)
    except ValueError:
        return False
    return True


def run_batches(pairs: List[dict], home: str, env, max_batches: Optional[int],
                log: Callable[[str], None]) -> dict:
    summary = {"batches": 0, "cached": 0, "skipped": 0, "stopped_early": False}
    cfg = config.load_config(home)
    binary = env.get("APPRENTICE_CLAUDE_BIN") or "claude"
    # run the child outside any project, so it loads no project CLAUDE.md or settings
    child_cwd = os.path.join(home, "state")
    os.makedirs(child_cwd, mode=0o700, exist_ok=True)
    failures = 0
    for start in range(0, len(pairs), BATCH_SIZE):
        if max_batches is not None and summary["batches"] >= max_batches:
            summary["stopped_early"] = True
            break
        if failures >= MAX_CONSECUTIVE_FAILURES:
            summary["stopped_early"] = True
            break
        chunk = pairs[start:start + BATCH_SIZE]
        profile = config.current_profile(env)
        batch = [pair for pair in chunk if pair.get("profile") == profile]
        summary["skipped"] += len(chunk) - len(batch)
        if not batch:
            continue
        summary["batches"] += 1
        outcome = _run_child(binary, build_prompt(batch, cfg), env, child_cwd)
        if isinstance(outcome, str):
            failures += 1
            summary["skipped"] += len(batch)
            log("preclassify: batch %d skipped (%s)" % (summary["batches"], outcome))
            continue
        failures = 0
        ids = {pair["event"]["id"] for pair in batch}
        seen = set()
        for raw in outcome:
            item = _valid_item(raw, ids, cfg)
            if item is None or item["id"] in seen:
                continue
            seen.add(item["id"])
            write_cache(home, item["id"], {k: v for k, v in item.items() if k != "id"}, MODEL)
            summary["cached"] += 1
        summary["skipped"] += len(ids) - len(seen)
    if failures >= MAX_CONSECUTIVE_FAILURES:
        summary["stopped_early"] = True
    return summary
