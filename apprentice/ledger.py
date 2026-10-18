from __future__ import annotations

import json
import os

from apprentice import SCHEMA_VERSION, config, events, scrub

if False:  # annotations only
    from typing import Dict, List, Optional, Set

DECISIONS = ("accepted", "rejected", "snoozed", "undone", "accept_failed")
STATUSES = ("shown", "accepted", "rejected", "snoozed", "edited", "accept_failed", "undone")
SEED_TOKEN_CAP = 64
# Only these decided statuses hide a candidate from the next run. `accept_failed` stays eligible (the apply did
# not land) and `undone` is recorded but no longer applied; both appear in current_states()/totals() as decided
# without hiding anything.
HIDING = frozenset(["rejected", "snoozed", "accepted"])


def is_hiding(status: Optional[str]) -> bool:
    return status in HIDING


# Fields copied from the private sidecar (or keying its non-visible text); never printed to a session.
PRIVATE_FIELDS = frozenset(["tokens", "cluster_key", "evidence_ids"])


def strip_private(value):
    """A copy of `value` with PRIVATE_FIELDS removed from every dict at any nesting level."""
    if isinstance(value, dict):
        return {k: strip_private(v) for k, v in value.items() if k not in PRIVATE_FIELDS}
    if isinstance(value, list):
        return [strip_private(v) for v in value]
    return value


def _ledger_file(home_dir: str, name: str) -> str:
    return os.path.join(home_dir, "ledger", name)


def _append(home_dir: str, name: str, entry: dict) -> None:
    out = dict(entry)
    out.setdefault("v", SCHEMA_VERSION)
    if "ts" not in out:
        out["ts"] = config.utc_now_iso()
    path = _ledger_file(home_dir, name)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    events.append(path, out)


def append_proposal(home_dir: str, entry: dict) -> None:
    _append(home_dir, "proposals.jsonl", entry)


def append_applied(home_dir: str, entry: dict) -> None:
    _append(home_dir, "applied.jsonl", entry)


def _read(home_dir: str, name: str) -> List[dict]:
    out = []
    try:
        fh = open(_ledger_file(home_dir, name), "rb")
    except OSError:
        return out
    with fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                obj = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def read_proposals(home_dir: str) -> List[dict]:
    return _read(home_dir, "proposals.jsonl")


def read_applied(home_dir: str) -> List[dict]:
    return _read(home_dir, "applied.jsonl")


def current_states(proposals: List[dict]) -> Dict[str, dict]:
    decided = {}
    shown = {}
    for entry in proposals:
        rule_id = entry.get("id")
        status = entry.get("status")
        if not isinstance(rule_id, str):
            continue
        if status in DECISIONS:
            decided[rule_id] = entry
        elif status == "shown":
            ts = entry.get("ts")
            if isinstance(ts, str) and ts > shown.get(rule_id, ""):
                shown[rule_id] = ts
    return {rule_id: {"decided": entry, "last_shown_ts": shown.get(rule_id)}
            for rule_id, entry in decided.items()}


def aliases(proposals: List[dict]) -> Dict[str, str]:
    out = {}
    for entry in proposals:
        merged = entry.get("merged_ids")
        if isinstance(merged, list) and isinstance(entry.get("id"), str):
            for member in merged:
                if isinstance(member, str):
                    out[member] = entry["id"]
    return out


def old_keys(proposals: List[dict]) -> Dict[str, Set[str]]:
    out = {}
    for entry in proposals:
        key = entry.get("cluster_key")
        if isinstance(key, str) and isinstance(entry.get("id"), str):
            out.setdefault(entry["id"], set()).add(key)
    return out


def seed_token_set(pairs: List[Set[str]]) -> List[str]:
    if not pairs:
        return []
    counts = {token: sum(1 for pair in pairs if token in pair) for token in pairs[0]}
    ranked = sorted(counts, key=lambda token: (-counts[token], token))
    return sorted(ranked[:SEED_TOKEN_CAP])


def totals(proposals: List[dict]) -> Dict[str, int]:
    out = {}
    for state in current_states(proposals).values():
        status = state["decided"]["status"]
        out[status] = out.get(status, 0) + 1
    return out


def private_path(home_dir: str, pack_json_path: str) -> str:
    return os.path.join(home_dir, "state", "pack-private", os.path.basename(pack_json_path))


def write_private(home_dir: str, pack_json_path: str, mapping: Dict[str, dict]) -> str:
    path = private_path(home_dir, pack_json_path)
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh, ensure_ascii=False, separators=(",", ":"))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    return path


def read_private(home_dir: str, pack_json_path: str) -> Dict[str, dict]:
    try:
        with open(private_path(home_dir, pack_json_path), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_pack(pack_json_path: str) -> dict:
    try:
        with open(pack_json_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def pack_candidates(pack: dict) -> Dict[str, dict]:
    out = {}
    items = pack.get("candidates")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                out[item["id"]] = item
    return out


def _sets(tokens) -> List[list]:
    if not isinstance(tokens, list):
        return []
    return [list(group) for group in tokens if isinstance(group, list)]


def _union(*lists) -> List[str]:
    seen = []
    for items in lists:
        for item in items or []:
            if item not in seen:
                seen.append(item)
    return seen


def _stored_pack(home_dir: str, pack_json_path: str) -> str:
    """The rendered pack path, relative to the data root when it lies under it."""
    path = pack_json_path[:-len(".json")] + ".md" if pack_json_path.endswith(".json") else pack_json_path
    root = os.path.realpath(home_dir)
    real = os.path.realpath(path)
    if real == root or real.startswith(root + os.sep):
        return os.path.relpath(real, root)
    return path


def record_decision(home_dir: str, pack_json_path: Optional[str], rule_id: str, status: str,
                    text: Optional[str] = None, scope=None, target=None, user_edit=None, reason=None,
                    n_at_decision=None, merged_ids: Optional[List[str]] = None,
                    profile: Optional[str] = None, cfg: Optional[dict] = None) -> dict:
    if status not in STATUSES:
        raise ValueError("unknown status: %s" % status)
    if cfg is None:
        cfg = config.load_config(home_dir)
    text, user_edit, reason = (scrub.scrub(v, cfg)[0] for v in (text, user_edit, reason))
    decided_at = config.utc_now_iso() if status in DECISIONS else None
    if pack_json_path is None:
        entry = {
            "id": rule_id, "status": status, "text": text, "scope": scope, "target": target,
            "evidence_ids": None, "merged_ids": list(merged_ids) if merged_ids else None, "kind": None,
            "cluster_key": None, "topic": None, "tokens": None, "profile": profile, "group": None,
            "n_at_decision": n_at_decision, "decided_at": decided_at, "user_edit": user_edit, "pack": None,
            "reason": reason,
        }
        append_proposal(home_dir, entry)
        return entry
    pack = load_pack(pack_json_path)
    candidates = pack_candidates(pack)
    private = read_private(home_dir, pack_json_path)
    cand = candidates.get(rule_id, {})
    own = private.get(rule_id) if isinstance(private.get(rule_id), dict) else {}
    merged = list(merged_ids) if merged_ids else None
    tokens = _sets(own.get("tokens")) or None
    evidence = list(cand.get("evidence_ids") or own.get("evidence_ids") or [])
    for member in merged or []:
        member_private = private.get(member) if isinstance(private.get(member), dict) else {}
        member_cand = candidates.get(member, {})
        extra = _sets(member_private.get("tokens"))
        if extra:
            tokens = (tokens or []) + extra
        evidence = _union(evidence, member_cand.get("evidence_ids") or member_private.get("evidence_ids"))
    inputs = pack.get("inputs")
    entry = {
        "id": rule_id,
        "status": status,
        "text": text,
        "scope": scope if scope is not None else cand.get("scope"),
        "target": target if target is not None else cand.get("target"),
        "evidence_ids": evidence,
        "merged_ids": merged,
        "kind": cand.get("kind", own.get("kind")),
        "cluster_key": own.get("cluster_key"),
        "topic": cand.get("topic", own.get("topic")),
        "tokens": tokens,
        "profile": profile,
        "group": inputs.get("group") if isinstance(inputs, dict) else None,
        "n_at_decision": n_at_decision if n_at_decision is not None else own.get("n"),
        "decided_at": decided_at,
        "user_edit": user_edit,
        "pack": _stored_pack(home_dir, pack_json_path),
        "reason": reason,
    }
    append_proposal(home_dir, entry)
    return entry
