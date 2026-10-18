"""Review pack builder: clean events, build candidates, rank them, write the pack JSON and markdown.

Privacy boundary: verbatim text (prompts, free text, anchors, headers, stems, labels, tickets, cwd, token sets,
quoted target lines) of any account group other than the current one never enters the pack. Counts, band inputs
and the departure search use every group's events; only the private sidecar outside `review/` holds token sets.
"""
import glob
import json
import os
import re
from collections import Counter
from typing import Dict, List, Mapping, Optional, Tuple

from apprentice import SCHEMA_VERSION, audit, cluster, confidence, config, departures, events, gitinfo, ledger
from apprentice import scope as scoping
from apprentice.scrub import mask_outbound

OBSERVER = "claude-mem-observer"
WATERMARK_S = 5.0
DEFAULT_LIMIT = 10
SUMMARY_CHARS = 200
QUOTE_CHARS = 300
QUOTES = 2
WITHHELD_HINT = "set unknown_group to see text from backfilled events"
MISSING_TITLE = "Missing (run /apprentice:apply --replay)"
SCOPES = ("global", "repo", "skill")
_BAND_RANK = {"high": 2, "medium": 1, "low": 0}
_ASK_KINDS = ("ask", "ask_rejected")
_MARK_START = "<!-- apprentice:start -->"
_MARK_END = "<!-- apprentice:end -->"
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REMAINING_KEYS = ("beyond_cap", "weak", "covered", "hidden_by_ledger", "single_signal", "text_withheld")


def _ts_key(ts) -> Tuple[float, str]:
    return (config.ts_epoch(ts) or 0.0, ts or "")


def since_epoch(since: Optional[str]) -> Optional[float]:
    """Seconds since the epoch for `YYYY-MM`, `YYYY-MM-DD` or an ISO timestamp (UTC); ValueError otherwise."""
    if not since:
        return None
    text = since.strip()
    if _MONTH_RE.match(text):
        text += "-01"
    if _DAY_RE.match(text):
        text += "T00:00:00+00:00"
    epoch = config.ts_epoch(text)
    if epoch is None:
        raise ValueError("unparseable --since: %r" % since)
    return epoch


def _is_pasted(event: dict) -> bool:
    return bool(event.get("pasted")) or "pasted" in (event.get("tags") or [])


def _is_image_only(event: dict) -> bool:
    return bool(event.get("image_only")) or "image_only" in (event.get("tags") or []) or event.get("text") is None


def _dropped(event: dict, entrypoints: set) -> bool:
    kind = event.get("kind")
    if kind == "machine":
        return True
    entry = event.get("entrypoint")
    if entry is not None and entry not in entrypoints:
        return True
    if any(OBSERVER in (event.get(field) or "") for field in ("cwd", "transcript_path")):
        return True
    return kind == "prompt" and (bool(event.get("duplicate_of")) or _is_pasted(event) or _is_image_only(event))


def clean(events_in: List[dict], cfg: dict, current_group: Optional[str], labels: Dict[str, str]) -> List[dict]:
    """Drop noise, apply the live-coverage watermark, dedup backfill copies and tag visibility; input is untouched."""
    entrypoints = set(cfg.get("capture_entrypoints") or [])
    kept = [event for event in events_in if not _dropped(event, entrypoints)]
    first_hook = {}  # type: Dict[str, float]
    for event in kept:
        epoch = config.ts_epoch(event.get("ts"))
        sid = event.get("session_id")
        if event.get("source") == "hook" and sid and epoch is not None:
            first_hook[sid] = min(first_hook.get(sid, epoch), epoch)
    covered = []
    for event in kept:
        limit = first_hook.get(event.get("session_id"))
        epoch = config.ts_epoch(event.get("ts"))
        if limit is not None and event.get("fidelity") == "transcript" and epoch is not None \
                and epoch >= limit - WATERMARK_S:
            continue
        covered.append(event)
    covered.sort(key=lambda event: (_ts_key(event.get("ts")), event.get("id") or ""))
    winners = {}  # type: Dict[Tuple[Optional[str], str], dict]
    for event in covered:
        if event.get("kind") in _ASK_KINDS and event.get("tool_use_id"):
            key = (event.get("session_id"), event["tool_use_id"])
            held = winners.get(key)
            if held is None or (held.get("fidelity") != "live" and event.get("fidelity") == "live"):
                winners[key] = event
    winner_ids = {id(event) for event in winners.values()}
    seen_prompts = set()
    out = []
    for event in covered:
        kind = event.get("kind")
        if kind in _ASK_KINDS and event.get("tool_use_id") and id(event) not in winner_ids:
            continue
        if kind == "prompt" and event.get("fidelity") == "transcript":
            key = (event.get("session_id"), event.get("ts"), event.get("sha1"))
            if key in seen_prompts:
                continue
            seen_prompts.add(key)
        tagged = dict(event)
        tagged["text_visible"] = config.text_visible(event.get("profile") or "", current_group, cfg, labels)
        if kind == "prompt" and event.get("register") == "ack":
            tagged["count_only"] = True
        out.append(tagged)
    return out


def _grouped_profiles(cfg: dict) -> set:
    return {profile for members in (cfg.get("account_groups") or {}).values() for profile in members}


def label_profiles(home: str, events_in: List[dict], cfg: dict) -> Dict[str, str]:
    """Opaque `group-N` labels for profiles outside every account group, in first-seen order."""
    grouped = _grouped_profiles(cfg)
    first_seen = {}  # type: Dict[str, str]
    for event in events_in:
        profile, ts = event.get("profile"), event.get("ts")
        if not isinstance(profile, str) or not profile or profile == "unknown" or profile in grouped:
            continue
        if isinstance(ts, str) and (profile not in first_seen or _ts_key(ts) < _ts_key(first_seen[profile])):
            first_seen[profile] = ts
    return config.assign_group_labels(home, first_seen)


def read_preclass(home: str, event_id: Optional[str]) -> Optional[dict]:
    """The pre-classification verdict T30 caches for one event, or None."""
    if not isinstance(event_id, str) or not event_id or os.sep in event_id or event_id.startswith("."):
        return None
    try:
        with open(os.path.join(home, "state", "preclass", event_id + ".json"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _df_texts(cleaned: List[dict]) -> List[str]:
    texts = []
    for event in cleaned:
        if event.get("kind") == "prompt" and isinstance(event.get("text"), str):
            texts.append(event["text"])
        elif event.get("kind") == "ask":
            for question in event.get("questions") or []:
                texts.append(question.get("question") or "")
                texts.extend(opt.get("label") or "" for opt in question.get("options") or [])
                answer = question.get("answer") or {}
                if answer.get("free_text"):
                    texts.append(answer["free_text"])
    return texts


def _known_skill_names(real_home: str) -> List[str]:
    claude = os.path.join(real_home, ".claude")
    roots = [os.path.join(claude, "skills"), os.path.join(claude, "local-skills")]
    roots.extend(sorted(glob.glob(os.path.join(glob.escape(claude), "skills", "synced", "*"))))
    names = set()
    for root in roots:
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        names.update(name for name in entries if os.path.isfile(os.path.join(root, name, "SKILL.md")))
    return sorted(names)


def _clip(text: str, limit: int) -> str:
    return mask_outbound(" ".join(text.split()))[:limit]


def _quotes(texts_newest_first: List[str]) -> List[str]:
    out = []  # type: List[str]
    for text in texts_newest_first:
        quote = _clip(text, QUOTE_CHARS)
        if quote and quote not in out:
            out.append(quote)
        if len(out) == QUOTES:
            break
    return out


def _satisfies(split: dict, row: dict) -> bool:
    """Whether a feature row falls on the split's departure side; a missing feature is the majority side."""
    value = row.get(split["feature"])
    if value is None:
        return False
    op = split["op"]
    if op == ">=":
        return departures._is_number(value) and value >= split["value"]
    if op == "<":
        return departures._is_number(value) and value < split["value"]
    return value == split["value"]


def _line_present(target: Optional[str], rule_id: str) -> Optional[bool]:
    if target is None:
        return None
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return False
    needle = "<!-- apprentice:%s " % rule_id
    inside = False
    for line in lines:
        if _MARK_START in line:
            inside = True
            continue
        if _MARK_END in line:
            inside = False
            continue
        if inside and needle in line:
            return True
    return False


class _Item(object):
    """One evidence event of a candidate: a cluster row or a group pair, reduced to what the pack logic needs."""

    __slots__ = ("event_id", "ts", "cwd", "repo", "visible", "profile")

    def __init__(self, event_id, ts, cwd, repo, visible, profile):
        self.event_id, self.ts, self.cwd, self.repo, self.visible, self.profile = \
            event_id, ts, cwd, repo, visible, profile


class _Run(object):
    def __init__(self, home: str, cfg: dict, real_home: str, labels: Dict[str, str], now: str,
                 current_group: Optional[str]) -> None:
        self.home = home
        self.cfg = cfg
        self.now = now
        self.current_group = current_group
        self.suffix = cfg.get("worktree_suffix") or ""
        self.resolver = scoping.TargetResolver(cfg, real_home, labels, {})
        self.skill_names = _known_skill_names(real_home)
        self.mains = {}  # type: Dict[str, Optional[str]]
        self.df = {}  # type: Dict[str, int]
        self.index = audit.LedgerIndex(ledger.read_proposals(home))
        self.last_ts_by_key = {}  # type: Dict[str, str]

    def main_of(self, cwd: str) -> Optional[str]:
        if cwd not in self.mains:
            self.mains[cwd] = gitinfo.resolve_main_for_cwd(cwd, self.suffix)
        return self.mains[cwd]

    def evidence_mains(self, items: List[_Item]) -> List[str]:
        out = []  # type: List[str]
        for item in sorted(items, key=lambda it: _ts_key(it.ts), reverse=True):
            main = self.main_of(item.cwd) if item.cwd else None
            if main is not None and main not in out:
                out.append(main)
        return out

    def targets(self, kind: str, found: dict, scope_name: str, skill_name: Optional[str], items: List[_Item],
                text_visible: bool, mains: List[str]) -> dict:
        resolver = self.resolver
        visible_cwds = []  # type: List[str]
        repo_cwds = {}  # type: Dict[str, List[str]]
        for item in sorted(items, key=lambda it: (_ts_key(it.ts), it.event_id or ""), reverse=True):
            if item.visible and item.cwd:
                if item.cwd not in visible_cwds:
                    visible_cwds.append(item.cwd)
                if item.repo and item.cwd not in repo_cwds.setdefault(item.repo, []):
                    repo_cwds[item.repo].append(item.cwd)
        repos = list(found.get("repos") or [])
        out = {"target": None, "target_status": "resolved", "target_git": None, "target_shared_groups": []}
        if scope_name == "skill":
            info = resolver.skill_target(skill_name)
            if info is None:
                out["target_status"] = "skill-not-found"
            else:
                out.update(target=info["target"], target_status=info["target_status"], target_git=info["target_git"])
        elif scope_name == "repo":
            evidence = scoping._evidence_repos(kind, found)
            if not (evidence and None not in evidence and "" not in evidence and len(set(evidence)) == 1):
                main = scoping.names_repo_artifact(scoping._visible_text(kind, found), mains)
                named = gitinfo.origin(main) if main else None
                repos = [named] if named else []
            if repos:
                info = resolver.repo_target(visible_cwds, repos[0], text_visible)
                out.update(target=info["target"], target_status=info["target_status"])
            else:
                out["target_status"] = "no-checkout"
        else:
            info = resolver.global_target()
            out.update(target=info["target"], target_shared_groups=info["target_shared_groups"])
        out["repos"] = repos
        out["recommend"] = resolver.recommend(out["target_status"], out["target_git"])
        out["alt_targets"] = resolver.alt_targets(scope_name, skill_name, repo_cwds,
                                                  {repo: True for repo in repo_cwds})
        return out

    def ledger_outcome(self, record: dict, match: dict, items: List[_Item]) -> str:
        found = audit.match_candidate(match, self.index, self.last_ts_by_key)
        decided = found[1] if found else None
        events_after = 0
        line_present = None
        if decided is not None:
            since = config.ts_epoch(decided.get("decided_at"))
            if since is not None:
                events_after = len({item.event_id for item in items
                                    if (config.ts_epoch(item.ts) or 0.0) > since})
            line_present = _line_present(record["target"], found[0])
        outcome = audit.ledger_disposition(match, decided, events_after, self.now, line_present)
        record["id"] = match["id"]
        record["inherited_from"] = match.get("inherited_from")
        return outcome


def _ask_items(found: dict) -> List[_Item]:
    return [_Item(row.get("event_id"), row.get("ts"), row.get("cwd"), row.get("repo"), bool(row.get("visible")),
                  row.get("profile")) for row in found.get("rows") or []]


def _pair_items(group: dict) -> List[_Item]:
    return [_Item(pair["event"].get("id"), pair.get("ts"), pair["event"].get("cwd"), pair.get("repo"),
                  bool(pair.get("visible")), pair.get("profile")) for pair in group.get("pairs") or []]


def _evidence_ids(items: List[_Item]) -> List[str]:
    out = []  # type: List[str]
    for item in sorted(items, key=lambda it: (_ts_key(it.ts), it.event_id or "")):
        if item.event_id and item.event_id not in out:
            out.append(item.event_id)
    return out


def _ask_summary(found: dict) -> str:
    answers = Counter(found.get("answers") or {})
    head = "%s: %s" % (found["header"], found["stem"]) if found["header"] else found["stem"]
    if answers:
        top = sorted(answers, key=lambda answer: (-answers[answer], answer))[0]
        head += " → %s %d/%d" % (top, answers[top], found["n"])
    return mask_outbound(head)


def _split_inputs(found: dict, path: Optional[str]) -> dict:
    analysis = departures.analyze(found)
    split = analysis["majority"] if analysis["majority"] is not None else analysis["recommended"]
    if split is None:
        return {"split": None, "conditional": False, "departure": None, "split_purity": None, "majority_band": None,
                "departure_rows": analysis.get("departure_rows") or []}
    answered = [row for row in found.get("rows") or [] if departures._row_answer(row) is not None]
    maj_rows = [{"session_id": row.get("session_id"), "repo": row.get("repo")}
                for row, features in zip(answered, departures.feature_rows(found)) if not _satisfies(split, features)]
    return {"split": split, "conditional": True, "departure": departures.sentence(split),
            "split_purity": split["purity"], "majority_band": confidence.majority_band(maj_rows, path),
            "departure_rows": []}


def _record(kind: str, found: dict, items: List[_Item], scope_name: str, skill_name: Optional[str], where: dict,
            band_value: str, band_inputs: dict, split: dict, audit_text: str, summary: str, quotes: List[str],
            withheld: dict, ask_fields: dict, rule_sketch: Optional[str], topic: Optional[str]) -> dict:
    return {
        "id": found["id"],
        "kind": kind,
        "scope": scope_name,
        "target": where["target"],
        "target_status": where["target_status"],
        "target_git": where["target_git"],
        "target_shared_groups": where["target_shared_groups"],
        "recommend": where["recommend"],
        "alt_targets": where["alt_targets"],
        "cluster_key": ask_fields["cluster_key"],
        "skill": skill_name,
        "repos": where["repos"],
        "band": band_value,
        "band_inputs": band_inputs,
        "conditional": split["conditional"],
        "departure": split["departure"],
        "departure_rows": split["departure_rows"],
        "audit": audit_text,
        "summary": summary,
        "quotes": quotes,
        "topic": topic,
        "text_withheld": withheld,
        "evidence_ids": _evidence_ids(items),
        "first_seen": found.get("first_seen"),
        "last_seen": found.get("last_seen"),
        "header": ask_fields["header"],
        "stem": ask_fields["stem"],
        "labels": ask_fields["labels"],
        "display_questions": ask_fields["display_questions"],
        "free_texts": ask_fields["free_texts"],
        "rule_sketch": rule_sketch,
        "inherited_from": None,
    }


def _no_split() -> dict:
    return {"split": None, "conditional": False, "departure": None, "split_purity": None, "majority_band": None,
            "departure_rows": []}


def build_pack(home: str, env: Mapping, cfg: dict, since: Optional[str], repo: Optional[str], scope: Optional[str],
               include_all: bool, limit: int, real_home: str) -> Tuple[dict, Dict[str, dict]]:
    """The review pack and its private sidecar mapping (token sets and ledger fields per listed candidate)."""
    now = config.utc_now_iso()
    floor = since_epoch(since)
    raw = [event for event in events.iter_events(home)
           if floor is None or (config.ts_epoch(event.get("ts")) or 0.0) >= floor]
    profile = config.current_profile(env)
    labels = label_profiles(home, raw, cfg)
    if profile != "unknown" and profile not in labels and profile not in _grouped_profiles(cfg):
        labels = config.assign_group_labels(home, {profile: now})
    current_group = config.group_of_profile(profile, cfg, labels)
    cleaned = clean(raw, cfg, current_group, labels)
    signal_events = [event for event in cleaned if not event.get("count_only")]
    run = _Run(home, cfg, real_home, labels, now, current_group)
    run.df = audit.document_frequency(_df_texts(cleaned))

    def visible(event: dict) -> bool:
        return bool(event.get("text_visible"))

    clusters = cluster.build_ask_clusters(signal_events, cfg, visible)
    pairs = cluster.select_pairs(signal_events, visible=visible)
    preclass = {}  # type: Dict[str, dict]
    for pair in pairs:
        verdict = read_preclass(home, pair["event"].get("id")) if pair["visible"] else None
        if verdict is not None:
            preclass[pair["event"]["id"]] = verdict
    groups = cluster.group_pairs(pairs, preclass)
    links = cluster.link_groups_to_clusters(groups, clusters, signal_events)
    cluster_links = {}  # type: Dict[str, List[str]]
    for group_id in sorted(links):
        for cluster_id in links[group_id]:
            cluster_links.setdefault(cluster_id, []).append(group_id)
    for found in clusters:
        stamps = [row["ts"] for row in found["rows"] if row.get("ts")]
        if stamps:
            run.last_ts_by_key[cluster.key_string(found["key"])] = max(stamps, key=_ts_key)

    remaining = {key: 0 for key in _REMAINING_KEYS}
    unknown_withheld = False
    built = []  # type: List[Tuple[dict, dict, List[_Item], dict]]

    for found in clusters:
        items = _ask_items(found)
        path = confidence.two_signal("ask", found["rows"], cluster_links.get(found["id"], []))
        if path is None:
            remaining["single_signal"] += 1
            continue
        if not found["text_visible"] and not found["skill"]:
            remaining["text_withheld"] += 1
            unknown_withheld = unknown_withheld or any(item.profile == "unknown" for item in items)
            continue
        split = _split_inputs(found, path)
        band_value, band_inputs = confidence.band(
            found["n"], len(found["sessions"]), len(found["repos"]), confidence.consistency(found, split["split"]),
            cluster.reversal_30d(found, now), bool(found["reversals"]), path, split["split_purity"],
            split["majority_band"])
        mains = run.evidence_mains(items)
        scope_name, named_skill = scoping.infer_scope("ask", found, cfg, run.skill_names, mains)
        skill_name = found["skill"] or named_skill
        where = run.targets("ask", found, scope_name, skill_name, items, found["text_visible"], mains)
        summary = _ask_summary(found)
        quote_ok = scope_name in ("global", "skill") or found["text_visible"]
        rows_newest = sorted(found["rows"], key=lambda row: (_ts_key(row.get("ts")), row.get("event_id") or ""),
                             reverse=True)
        quotes = _quotes([row["free_text"] for row in rows_newest if row.get("visible") and row.get("free_text")])
        ask_fields = {"cluster_key": cluster.key_string(found["key"]), "header": found["header"],
                      "stem": found["stem"], "labels": found["labels"],
                      "display_questions": found["display_questions"], "free_texts": found["free_texts"]}
        record = _record("ask", found, items, scope_name, skill_name, where, band_value, band_inputs, split,
                         audit.audit_target(summary, where["target"], quote_ok, run.df, cfg), summary, quotes,
                         {"events": found["withheld_rows"], "free_text": found["free_text_withheld"]},
                         ask_fields, None, None)
        match = {"id": found["id"], "kind": "ask", "key": found["key"], "first_seen": found["first_seen"],
                 "evidence_ids": record["evidence_ids"]}
        private = {"tokens": None, "cluster_key": ask_fields["cluster_key"], "kind": "ask", "topic": None,
                   "evidence_ids": record["evidence_ids"], "n": band_inputs["n"]}
        built.append((record, match, items, private))

    for group in groups:
        items = _pair_items(group)
        path = confidence.two_signal("correction", group["pairs"], links.get(group["id"], []))
        if path is None:
            remaining["single_signal"] += 1
            continue
        visible_pairs = [pair for pair in group["pairs"] if pair["visible"]]
        if not visible_pairs:
            remaining["text_withheld"] += 1
            unknown_withheld = unknown_withheld or any(item.profile == "unknown" for item in items)
            continue
        split = _no_split()
        band_value, band_inputs = confidence.band(
            len(group["pairs"]), len(group["sessions"]), len(group["repos"]), confidence.consistency(group, None),
            False, False, path, None, None)
        mains = run.evidence_mains(items)
        scope_name, skill_name = scoping.infer_scope("correction", group, cfg, run.skill_names, mains)
        where = run.targets("correction", group, scope_name, skill_name, items, True, mains)
        newest = sorted(visible_pairs, key=lambda pair: (_ts_key(pair.get("ts")), pair["event"].get("id") or ""),
                        reverse=True)
        summary = _clip(newest[0]["event"].get("text") or "", SUMMARY_CHARS)
        quotes = _quotes([pair["event"].get("text") or "" for pair in newest])
        sketch = None
        for pair in newest:
            verdict = preclass.get(pair["event"].get("id")) or {}
            hint = verdict.get("rule_sketch") or verdict.get("rule")
            if isinstance(hint, str) and hint:
                sketch = _clip(hint, SUMMARY_CHARS)
                break
        withheld_pairs = len(group["pairs"]) - len(visible_pairs)
        ask_fields = {"cluster_key": None, "header": None, "stem": None, "labels": [], "display_questions": [],
                      "free_texts": []}
        record = _record("correction", group, items, scope_name, skill_name, where, band_value, band_inputs, split,
                         audit.audit_target(summary, where["target"], True, run.df, cfg), summary, quotes,
                         {"events": withheld_pairs, "free_text": withheld_pairs}, ask_fields, sketch, group["topic"])
        match = {"id": group["id"], "kind": "correction", "pairs": group["pairs"], "topic": group["topic"],
                 "evidence_ids": record["evidence_ids"]}
        # the seed pair goes first: seed_token_set ranks the seed's tokens by how many pairs share them
        private = {"tokens": [ledger.seed_token_set([set(pair["tokens"]) for pair in group["pairs"]])],
                   "cluster_key": None, "kind": "correction", "topic": group["topic"],
                   "evidence_ids": record["evidence_ids"], "n": band_inputs["n"]}
        built.append((record, match, items, private))

    proposable, weak, covered, missing = [], [], [], []
    private_out = {}  # type: Dict[str, dict]
    for record, match, items, private in built:
        if scope is not None and record["scope"] != scope:
            continue
        if repo is not None and repo not in record["repos"]:
            continue
        outcome = run.ledger_outcome(record, match, items)
        if outcome in ("hidden-rejected", "hidden-snoozed"):
            remaining["hidden_by_ledger"] += 1
            continue
        if outcome == "covered":
            covered.append((record, private))
        elif outcome == "missing":
            missing.append((record, private))
        elif record["band"] == "low" and not include_all:
            weak.append((record, private))
        else:
            proposable.append((record, private))

    def rank(entry: Tuple[dict, dict]) -> Tuple[int, int, float, str, str]:
        record = entry[0]
        return (-_BAND_RANK.get(record["band"], 0), 0 if record["conditional"] else 1,
                -_ts_key(record["last_seen"])[0], record["last_seen"] or "", record["id"])

    for bucket in (proposable, weak, covered, missing):
        bucket.sort(key=rank)
    shown = proposable[:max(limit, 0)]
    remaining["beyond_cap"] = len(proposable) - len(shown)
    remaining["weak"] = len(weak)
    remaining["covered"] = len(covered)
    if unknown_withheld:
        remaining["hint"] = WITHHELD_HINT
    for record, private in shown + weak + covered + missing:
        private_out[record["id"]] = private

    current_events = sum(1 for event in cleaned if event["text_visible"])
    pack = {
        "v": SCHEMA_VERSION,
        "generated_at": now,
        "inputs": {
            "events": len(cleaned),
            "since": since,
            "preclass": bool(preclass),
            "profile": profile,
            "group": current_group,
            "group_profiles": config.group_profiles(current_group, cfg, labels) if current_group else [],
            "unknown_text_visible": config.text_visible("unknown", current_group, cfg, labels),
            "events_current_group": current_events,
            "events_other_groups": len(cleaned) - current_events,
        },
        "candidates": [record for record, _ in shown],
        "weak": [record for record, _ in weak],
        "covered": [record for record, _ in covered],
        "missing": [record for record, _ in missing],
        "remaining": remaining,
    }
    return pack, private_out


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def _block(index: int, cand: dict) -> List[str]:
    inputs = cand.get("band_inputs") or {}
    lines = ["## %d. %s" % (index, _one_line(cand.get("summary")) or "(no summary)"), ""]
    lines.append("- Rule: %s" % (_one_line(cand.get("rule_sketch") or cand.get("summary")) or "(write one)"))
    lines.append("- id `%s` · %s · recommend %s" % (cand.get("id"), cand.get("kind"), cand.get("recommend")))
    lines.append("- Scope: %s · target: %s · status: %s" % (cand.get("scope"), cand.get("target") or "none",
                                                            cand.get("target_status")))
    if cand.get("scope") == "global" and cand.get("target_shared_groups"):
        lines.append("- Shared with account groups: %s" % ", ".join(cand["target_shared_groups"]))
    git = cand.get("target_git") or {}
    if cand.get("scope") == "skill" and git.get("tracked"):
        lines.append("- Tracking: tracked in git (%s, %s)" % (git.get("remote") or "no remote",
                                                             git.get("visibility") or "visibility unknown"))
    lines.append("- Band: %s · n %s · %s sessions · %s repos · %s to %s" % (
        cand.get("band"), inputs.get("n"), inputs.get("sessions"), inputs.get("repos"),
        (cand.get("first_seen") or "?")[:10], (cand.get("last_seen") or "?")[:10]))
    if cand.get("departure"):
        lines.append("- Departure: %s" % cand["departure"])
    elif cand.get("departure_rows"):
        lines.append("- Departures:")
        for row in cand["departure_rows"]:
            lines.append("  - %s %s → %s" % ((row.get("ts") or "?")[:10], row.get("repo") or "-", row.get("answer")))
    for quote in cand.get("quotes") or []:
        lines.append("> %s" % _one_line(quote))
    withheld = (cand.get("text_withheld") or {}).get("events") or 0
    if withheld:
        lines.append("- %d more from other account groups (text withheld)" % withheld)
    lines.append("- Audit: %s" % cand.get("audit"))
    lines.append("")
    return lines


def _short(cand: dict) -> str:
    return "- `%s` %s (%s)" % (cand.get("id"), _one_line(cand.get("summary")), cand.get("band"))


def render_markdown(pack: dict) -> str:
    inputs = pack.get("inputs") or {}
    lines = ["# Apprentice review pack", "",
             "Generated %s for profile %s (group %s): %s events, %s from this group, %s from other groups." % (
                 pack.get("generated_at"), inputs.get("profile"), inputs.get("group"), inputs.get("events"),
                 inputs.get("events_current_group"), inputs.get("events_other_groups")), ""]
    candidates = pack.get("candidates") or []
    if not candidates:
        lines.extend(["No candidates this run.", ""])
    for index, cand in enumerate(candidates, 1):
        lines.extend(_block(index, cand))
    for title, key in (("Weak", "weak"), ("Covered", "covered"), (MISSING_TITLE, "missing")):
        items = pack.get(key) or []
        if items:
            lines.extend(["## %s" % title, ""] + [_short(cand) for cand in items] + [""])
    remaining = pack.get("remaining") or {}
    lines.extend(["## Remaining", ""])
    lines.extend("- %s: %s" % (key, remaining.get(key, 0)) for key in _REMAINING_KEYS)
    if remaining.get("hint"):
        lines.append("- Hint: %s" % remaining["hint"])
    lines.append("")
    return "\n".join(lines)


def _write_file(path: str, text: str) -> None:
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


def _next_number(folder: str, day: str) -> int:
    pattern = re.compile(r"^" + re.escape(day) + r"-(\d+)\.(?:json|md)$")
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    taken = [int(match.group(1)) for match in (pattern.match(name) for name in names) if match]
    return max(taken) + 1 if taken else 1


def write_pack(home: str, pack: dict, private: Dict[str, dict]) -> Tuple[str, str]:
    """Write the private sidecar first, then `review/<day>-<n>.json` and `.md`; return the two review paths."""
    config.ensure_dirs(home)
    folder = os.path.join(home, "review")
    day = (pack.get("generated_at") or config.utc_now_iso())[:10]
    base = os.path.join(folder, "%s-%d" % (day, _next_number(folder, day)))
    json_path, md_path = base + ".json", base + ".md"
    ledger.write_private(home, json_path, private)
    _write_file(json_path, json.dumps(pack, ensure_ascii=False, indent=2) + "\n")
    _write_file(md_path, render_markdown(pack))
    return json_path, md_path
