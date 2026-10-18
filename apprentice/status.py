"""Markdown status report: a health verdict, a profile table, a kinds table, conditional notes and actions."""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import time

from apprentice import config, events, gitinfo

if False:  # annotations only
    from typing import Dict, List, Mapping, Optional, Tuple

# A profile without the plugin needs the marketplace (marketplaces are per profile) and then install; a profile
# whose enabledPlugins entry is false already has it and needs enable.
INSTALL_VERB = "install"
ENABLE_VERB = "enable"
PLUGIN_KEY = "apprentice@vtrifonov"
GITHUB_SOURCE = "vtrifonov/claude-apprentice"
_CACHE_PART = os.sep + os.path.join("plugins", "cache") + os.sep
STALE_SECONDS = 24 * 3600
RECENT_SECONDS = 7 * 24 * 3600
MAX_MONTHS = 3
_TAIL_BYTES = 262144
_INTERPRETER_RE = re.compile(r"^(\S+) \S+ interpreter-missing")
_SKIPPED_RE = re.compile(r"skipped \(([^)]*)\)")
_KINDS_RE = re.compile(r"events by kind \(([^)]*)\)")
_LOCAL_NAME = "CLAUDE.local.md"
_LINE_CAP = 300
_KIND_LABELS = {"machine": "command", "ask_rejected": "ask rejected"}
_ENABLED_CELL = {True: "✅", False: "⛔ disabled", None: "⚪ not installed"}
_LINK_PROBLEMS = ("skipped-not-excluded", "skipped-file-exists", "no-main-checkout")
_PYTHON_REMEDY = "`xcode-select --install` (or set `APPRENTICE_PYTHON` to a Python 3.9 or newer interpreter)"


def _read_tail(path: str, nbytes: int = _TAIL_BYTES) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - nbytes))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return None


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def profile_dirs(real_home: str) -> List[str]:
    """`~/.claude` (always) and every `~/.claude-*` directory that holds a settings.json and either a
    projects/ directory or a .claude.json. Data directories such as `~/.claude-mem` fail that test."""
    try:
        names = sorted(os.listdir(real_home))
    except OSError:
        return []
    out = [os.path.join(real_home, ".claude")]
    for name in names:
        path = os.path.join(real_home, name)
        if name.startswith(".claude-") and os.path.isfile(os.path.join(path, "settings.json")):
            if os.path.isdir(os.path.join(path, "projects")) \
                    or os.path.isfile(os.path.join(path, ".claude.json")):
                out.append(path)
    return out


def plugin_enabled(settings_path: str) -> Optional[bool]:
    data = _read_json(settings_path)
    plugins = data.get("enabledPlugins") if data is not None else None
    if not isinstance(plugins, dict):
        return None
    value = plugins.get(PLUGIN_KEY)
    return value if isinstance(value, bool) else None


def _profile_name(path: str) -> str:
    base = os.path.basename(path.rstrip("/"))
    return "default" if base == ".claude" else base


def marketplace_source(plugin_root: Optional[str] = None) -> str:
    """What `claude plugin marketplace add` takes for this plugin: the checkout it runs from, or the GitHub repo
    when it runs from a profile's plugin cache."""
    root = plugin_root or os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    return GITHUB_SOURCE if _CACHE_PART in root + os.sep else root


def _plugin_commands(name: str, path: str, enabled: Optional[bool]) -> List[str]:
    prefix = "env -u CLAUDE_CONFIG_DIR" if name == "default" else "CLAUDE_CONFIG_DIR=%s" % path
    if enabled is False:
        return ["%s claude plugin %s %s" % (prefix, ENABLE_VERB, PLUGIN_KEY)]
    return ["%s claude plugin marketplace add %s" % (prefix, marketplace_source()),
            "%s claude plugin %s %s" % (prefix, INSTALL_VERB, PLUGIN_KEY)]


def _transcripts(profile_dir: str, seen: set) -> List[tuple]:
    """(realpath, mtime) of each transcript under `<profile_dir>/projects` not already in `seen`."""
    root = os.path.realpath(os.path.join(profile_dir, "projects"))
    try:
        slugs = os.listdir(root)
    except OSError:
        return []
    out = []
    for slug in slugs:
        folder = os.path.join(root, slug)
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.realpath(os.path.join(folder, name))
            if path in seen:
                continue
            seen.add(path)
            try:
                out.append((path, os.stat(path).st_mtime))
            except OSError:
                continue
    return out


def _newest_transcript(profile_dir: str) -> Optional[float]:
    return max((mtime for _path, mtime in _transcripts(profile_dir, set())), default=None)


def _shared_projects(dirs: List[str]) -> set:
    """Profile dirs whose projects/ resolves to the same real directory as another listed profile's."""
    by_real = {}  # type: Dict[str, List[str]]
    for profile_dir in dirs:
        root = os.path.realpath(os.path.join(profile_dir, "projects"))
        if os.path.isdir(root):
            by_real.setdefault(root, []).append(profile_dir)
    return set(p for group in by_real.values() if len(group) > 1 for p in group)


def _newest_transcript_by_rule(dirs: List[str], cfg: dict) -> Dict[str, float]:
    """Newest transcript per profile name, from each file's head `cwd` and `profile_rules`: profiles may
    share one projects/ directory."""
    from apprentice import backfill
    seen = set()  # type: set
    newest = {}  # type: Dict[str, float]
    for profile_dir in dirs:
        for path, mtime in _transcripts(profile_dir, seen):
            cwd = next((o["cwd"] for o in backfill._head_lines(path) if isinstance(o.get("cwd"), str)), None)
            name = backfill._profile_for(cwd, cfg)
            if mtime > newest.get(name, 0.0):
                newest[name] = mtime
    return newest


def _newest_transcript_any(dirs: List[str]) -> Optional[float]:
    seen = set()  # type: set
    return max((mtime for d in dirs for _path, mtime in _transcripts(d, seen)), default=None)


def _interpreter_missing_ts(home_dir: str) -> Optional[str]:
    text = _read_tail(os.path.join(home_dir, "hook-errors.log"))
    if text is None:
        return None
    newest = None
    newest_epoch = None
    for line in text.splitlines():
        match = _INTERPRETER_RE.match(line)
        if match is None:
            continue
        epoch = config.ts_epoch(match.group(1))
        if epoch is not None and (newest_epoch is None or epoch > newest_epoch):
            newest, newest_epoch = match.group(1), epoch
    return newest


def _recent_errors(home_dir: str, now: float) -> int:
    """hook-errors.log lines stamped within the last 7 days."""
    text = _read_tail(os.path.join(home_dir, "hook-errors.log"))
    count = 0
    for line in (text or "").splitlines():
        epoch = config.ts_epoch(line.split(" ", 1)[0])
        if epoch is not None and now - epoch <= RECENT_SECONDS:
            count += 1
    return count


def _backfill_running(home_dir: str) -> bool:
    try:
        fd = os.open(os.path.join(home_dir, "state", "backfill.lock"), os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _pairs(counts: Dict[str, int]) -> List[str]:
    return ["  %s %d" % (k, counts[k]) for k in sorted(counts)] or ["  none"]


def _plural(count: int, word: str) -> str:
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def _when(epoch: Optional[float], now: float) -> str:
    if epoch is None:
        return "—"
    day = datetime.date.fromtimestamp(epoch)
    today = datetime.date.fromtimestamp(now)
    clock = time.strftime("%H:%M", time.localtime(epoch))
    if day == today:
        return "today " + clock
    if day == today - datetime.timedelta(days=1):
        return "yesterday " + clock
    return day.isoformat()


def _md_table(header: List[str], rows: List[List[str]]) -> List[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out.extend("| " + " | ".join(row) + " |" for row in rows)
    return out


class _Tally(object):
    def __init__(self):
        self.by_kind_month = {}  # type: Dict[str, Dict[str, int]]
        self.months = set()
        self.sources = {"hook": 0, "backfill": 0}
        self.by_profile = {}  # type: Dict[str, int]
        self.by_entrypoint = {}  # type: Dict[str, int]
        self.entrypoint_unknown = 0
        self.newest_hook = {}  # type: Dict[str, float]
        self.newest_hook_any = None  # type: Optional[float]

    def add(self, obj: dict) -> None:
        kind = str(obj.get("kind"))
        epoch = config.ts_epoch(obj.get("ts"))
        month = events.month_of(obj["ts"]) if epoch is not None else "unknown"
        self.months.add(month)
        row = self.by_kind_month.setdefault(kind, {})
        row[month] = row.get(month, 0) + 1
        source = obj.get("source")
        if source in self.sources:
            self.sources[source] += 1
        profile = obj.get("profile") if isinstance(obj.get("profile"), str) else "unknown"
        self.by_profile[profile] = self.by_profile.get(profile, 0) + 1
        entry = obj.get("entrypoint")
        if isinstance(entry, str) and entry:
            self.by_entrypoint[entry] = self.by_entrypoint.get(entry, 0) + 1
        else:
            self.entrypoint_unknown += 1
        if source == "hook" and epoch is not None:
            if epoch > self.newest_hook.get(profile, 0.0):
                self.newest_hook[profile] = epoch
            if self.newest_hook_any is None or epoch > self.newest_hook_any:
                self.newest_hook_any = epoch

    def total(self) -> int:
        return sum(self.sources.values())


def _kind_table(tally: _Tally) -> List[str]:
    months = sorted(tally.months)[-MAX_MONTHS:]
    rows = []
    for kind, per_month in tally.by_kind_month.items():
        counts = [per_month.get(m, 0) for m in months]
        rows.append((-sum(counts), _KIND_LABELS.get(kind, kind), counts))
    if not rows:
        return []
    header = ["kind"] + months + (["total"] if len(months) > 1 else [])
    body = [[label] + [str(c) for c in counts] + ([str(sum(counts))] if len(months) > 1 else [])
            for _neg, label, counts in sorted(rows, key=lambda r: (r[0], r[1]))]
    return _md_table(header, body)


class _Profiles(object):
    """Per-profile rows plus what the verdict, notes and actions need to know about them."""

    def __init__(self):
        self.rows = []  # type: List[List[str]]
        self.flags = []  # type: List[str]
        self.commands = []  # type: List[str]
        self.shared = set()  # type: set
        self.stale = False


def _profiles(real_home: str, cfg: dict, tally: _Tally, missing: bool, now: float) -> _Profiles:
    out = _Profiles()
    dirs = profile_dirs(real_home)
    by_rule = _newest_transcript_by_rule(dirs, cfg) if cfg.get("profile_rules") else None
    out.shared = _shared_projects(dirs) if by_rule is None else set()
    for path in dirs:
        name = _profile_name(path)
        enabled = plugin_enabled(os.path.join(path, "settings.json"))
        live = tally.newest_hook.get(name)
        transcript = by_rule.get(name) if by_rule is not None else _newest_transcript(path)
        flag = ""
        if enabled is True and transcript is not None and (live is None or transcript - live > STALE_SECONDS):
            if missing:
                flag = "INTERPRETER MISSING"
            elif path not in out.shared:
                flag = "STALE"
                out.stale = out.stale or by_rule is not None
        out.flags.append(flag)
        # the exact profile name account_groups lists
        out.rows.append([name, _ENABLED_CELL[enabled], _when(live, now)])
        if enabled is not True:
            out.commands.extend(_plugin_commands(name, path, enabled))
    return out


def _profile_table(profiles: _Profiles) -> List[str]:
    header = ["profile", "enabled", "last event"]
    rows = [list(r) for r in profiles.rows]
    if any(profiles.flags):
        header.append("flag")
        for row, flag in zip(rows, profiles.flags):
            row.append(flag)
    return _md_table(header, rows)


def _verdict(disabled: bool, tally: _Tally, newest_transcript: Optional[float], attention: bool,
             now: float) -> str:
    if disabled:
        return "⏸ disabled"
    live = tally.newest_hook_any
    if live is None or (now - live > RECENT_SECONDS and newest_transcript is not None
                        and newest_transcript > live):
        return "❌ not capturing"
    return "⚠ needs attention" if attention else "✅ capturing"


def _group_parts(home_dir: str, env: Mapping, cfg: dict, tally: _Tally) -> Tuple[List[str], List[str], str]:
    """(current profile and group lines, one line per group, unknown text line or "")."""
    labels = config.read_group_labels(home_dir)
    profile = config.current_profile(env)
    current = config.group_of_profile(profile, cfg, labels)
    head = ["current profile: %s" % profile,
            "current group: %s" % (current if current is not None else "no label yet")]
    names = set((cfg.get("account_groups") or {}).keys()) | set(labels.values())
    per_group = {}  # type: Dict[str, int]
    unlabelled = 0
    for name, count in tally.by_profile.items():
        group = config.group_of_profile(name, cfg, labels)
        if group is None:
            # unknown is never labelled; the withheld line reports it
            if name != "unknown":
                unlabelled += count
        else:
            per_group[group] = per_group.get(group, 0) + count
    names |= set(per_group)
    groups = []
    for name in sorted(names):
        members = config.group_profiles(name, cfg, labels)
        groups.append("%s: profiles %s; events %d" % (name, ", ".join(members) or "none", per_group.get(name, 0)))
    if unlabelled:
        groups.append("no label yet: events %d" % unlabelled)
    unknown_events = tally.by_profile.get("unknown", 0)
    unknown = ""
    if cfg.get("unknown_group") is not None:
        head.append("unknown_group: %s" % cfg["unknown_group"])
    elif unknown_events:
        unknown = "unknown text withheld (%d events with an unknown profile)" % unknown_events
    return head, groups, unknown


def _group_note(home_dir: str, env: Mapping, cfg: dict, tally: _Tally) -> Optional[str]:
    _head, groups, unknown = _group_parts(home_dir, env, cfg, tally)
    labelled = [g for g in groups if not g.startswith("no label yet:")]
    if not labelled and not unknown:
        return None
    return "ℹ " + " · ".join(labelled + ([unknown] if unknown else []))


def _group_detail_lines(home_dir: str, env: Mapping, cfg: dict, tally: _Tally) -> List[str]:
    head, groups, unknown = _group_parts(home_dir, env, cfg, tally)
    return head + groups + ([unknown] if unknown else [])


def _backfill_note(home_dir: str) -> str:
    log_path = os.path.join(home_dir, "state", "backfill.log")
    text = _read_tail(log_path)
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if _backfill_running(home_dir):
        return "backfill: running" + (" — " + lines[-1][:_LINE_CAP] if lines else "")
    for line in reversed(lines):
        if line.startswith("backfill done") or line.startswith("backfill interrupted"):
            match = _KINDS_RE.search(line)
            total = sum(int(n) for n in re.findall(r"\s(\d+)(?:,|$)", match.group(1))) if match else 0
            try:
                stamp = os.stat(log_path).st_mtime
            except OSError:
                stamp = time.time()
            return "backfill: last run %s%s, %s" % (datetime.date.fromtimestamp(stamp).isoformat(),
                                                    " (interrupted)" if line.startswith("backfill interrupted") else "",
                                                    _plural(total, "event"))
    return "backfill: not run yet — /apprentice:backfill"


def _last_error_note(home_dir: str) -> Optional[str]:
    last = _read_json(os.path.join(home_dir, "state", "last-error.json"))
    if last is None:
        return None
    return "last hook error: %s %s %s" % (last.get("ts", "?"), last.get("event", "?"),
                                           str(last.get("message", ""))[:_LINE_CAP])


def _same_group(info: dict, profile: str, group: Optional[str]) -> bool:
    """Whether the session that wrote last-sessionstart.json is in the current account group. Any profile's
    session may have written it, and its cwd is that group's verbatim text. Two sessions without a group match
    only when they are the same profile."""
    if "group" not in info:
        return False
    if group is not None:
        return info["group"] == group
    return info["group"] is None and info.get("profile") == profile


def _session_start_problem(home_dir: str, profile: str, group: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(note, action) when the last SessionStart link result is an error."""
    info = _read_json(os.path.join(home_dir, "state", "last-sessionstart.json"))
    link = info.get("link") if info is not None else None
    if link not in _LINK_PROBLEMS:
        return None, None
    if not _same_group(info, profile, group):
        return "SessionStart link: %s (a session in another account group)" % link, None
    if link != "skipped-not-excluded":
        return "SessionStart link: %s" % link, None
    root = gitinfo.find_git(info["cwd"]) if isinstance(info.get("cwd"), str) else None
    path = gitinfo.exclude_path(root) if root else None
    if path is None:
        return "SessionStart link: %s (exclude file not found)" % link, None
    return ("SessionStart link: %s — add the line %s to %s, then start a new session" % (link, _LOCAL_NAME, path),
            "echo %s >> %s" % (_LOCAL_NAME, path))


def _cutoff_month(months: int, now: float) -> str:
    stamp = time.gmtime(now)
    index = stamp.tm_year * 12 + stamp.tm_mon - 1 - months
    return "%04d-%02d" % (index // 12, index % 12 + 1)


def _retention_note(cfg: dict, tally: _Tally, now: float) -> Optional[str]:
    months = cfg["retention_months"]
    if months <= 0:
        return None
    cutoff = _cutoff_month(months, now)
    old = sum(count for per_month in tally.by_kind_month.values()
              for month, count in per_month.items() if month != "unknown" and month < cutoff)
    if not old:
        return None
    return "retention: %s older than %s (retention_months %d)" % (_plural(old, "event"), cutoff, months)


_PACK_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d+)\.json$")
_BAND_ORDER = ("high", "medium", "low")


def _newest_pack(home_dir: str, group: Optional[str]) -> Tuple[bool, Optional[Tuple[str, dict]]]:
    """(any pack exists, (file name, pack) of the newest pack built for `group` or None). `review/` is shared
    by every account group, so another group's pack is never counted as this one's."""
    try:
        names = os.listdir(os.path.join(home_dir, "review"))
    except OSError:
        return False, None
    ranked = sorted((m.group(1), int(m.group(2)), n) for n in names for m in [_PACK_RE.match(n)] if m)
    for _day, _num, name in reversed(ranked):
        pack = _read_json(os.path.join(home_dir, "review", name))
        inputs = pack.get("inputs") if isinstance(pack, dict) else None
        if isinstance(inputs, dict) and inputs.get("group") == group:
            return True, (name, pack)
    return bool(ranked), None


def _band_counts(candidates: list, hidden: set) -> str:
    counts = {}  # type: Dict[str, int]
    for cand in candidates:
        if not isinstance(cand, dict) or cand.get("id") in hidden:
            continue
        band = cand.get("band") if isinstance(cand.get("band"), str) else "unknown"
        counts[band] = counts.get(band, 0) + 1
    order = [b for b in _BAND_ORDER if b in counts] + sorted(b for b in counts if b not in _BAND_ORDER)
    return ", ".join("%s %d" % (b, counts[b]) for b in order) or "none"


def _age_days(epoch: float, now: float) -> str:
    return _plural(max(0, int((now - epoch) // 86400)), "day") + " old"


def _hidden_ids(proposals: List[dict], candidates: list) -> set:
    """Candidate ids hidden by the ledger: their own decided status hides them, or that of the primary they
    were merged into."""
    from apprentice import ledger
    states = ledger.current_states(proposals)
    alias = ledger.aliases(proposals)

    def hiding(rule_id: Optional[str]) -> bool:
        return rule_id in states and ledger.is_hiding(states[rule_id]["decided"].get("status"))

    ids = [c.get("id") for c in candidates if isinstance(c, dict)]
    return set(rid for rid in ids if hiding(rid) or hiding(alias.get(rid)))


def _review_rows(home_dir: str, now: float, group: Optional[str]) -> List[List[str]]:
    """Milestone 2 rows: counts, dates and file names only, never candidate, rule or event text."""
    from apprentice import ledger
    rows = []  # type: List[List[str]]
    proposals = ledger.read_proposals(home_dir)
    any_pack, found = _newest_pack(home_dir, group)
    if found is None:
        rows.append(["review pack", "no pack yet for this group" if any_pack else "no pack yet"])
    else:
        name, pack = found
        stamp = pack.get("generated_at")
        day = stamp[:10] if isinstance(stamp, str) else _PACK_RE.match(name).group(1)
        rows.append(["review pack", "%s (%s)" % (day, name)])
        cands = pack.get("candidates")
        cands = cands if isinstance(cands, list) else []
        rows.append(["pending", _band_counts(cands, _hidden_ids(proposals, cands))])
    counts = ledger.totals(proposals)
    order = [s for s in ledger.STATUSES if s in counts] + sorted(s for s in counts if s not in ledger.STATUSES)
    rows.append(["ledger", ", ".join("%s %d" % (s, counts[s]) for s in order) or "none"])
    profile_dir = os.path.join(home_dir, "profile")
    try:
        tones = sorted(n for n in os.listdir(profile_dir) if n.startswith("tone-") and n.endswith(".md"))
    except OSError:
        tones = []
    for name in tones:
        try:
            rows.append([name, _age_days(os.stat(os.path.join(profile_dir, name)).st_mtime, now)])
        except OSError:
            continue
    try:
        cached = sum(1 for n in os.listdir(os.path.join(home_dir, "state", "preclass")) if n.endswith(".json"))
    except OSError:
        cached = None
    cell = "none" if cached is None else "%d %s" % (cached, "entry" if cached == 1 else "entries")
    rows.append(["preclassify cache", cell])
    return rows


def _details(home_dir: str, env: Mapping, cfg: dict, tally: _Tally, counters: Dict[str, int],
             now: float) -> List[str]:
    out = ["events: live %d, backfilled %d, total %d"
           % (tally.sources["hook"], tally.sources["backfill"], tally.total()),
           "skipped lines: bad %d, future %d" % (counters.get("bad", 0), counters.get("future", 0))]
    try:
        size = os.path.getsize(os.path.join(home_dir, "hook-errors.log"))
    except OSError:
        size = 0
    out.append("hook-errors.log: %d bytes" % size)
    text = _read_tail(os.path.join(home_dir, "state", "backfill.log"))
    for line in reversed([l for l in (text or "").splitlines() if l.strip()]):
        if line.startswith("backfill done") or line.startswith("backfill interrupted"):
            match = _SKIPPED_RE.search(line)
            if match:
                out.append("last backfill skipped: %s" % match.group(1))
            break
    out.append("")
    out.append("events by profile:")
    out.extend(_pairs(tally.by_profile))
    out.append("events by entrypoint:")
    out.extend(_pairs(tally.by_entrypoint))
    out.append("entrypoint_unknown: %d" % tally.entrypoint_unknown)
    out.append("")
    out.append("account groups:")
    out.extend("  " + line for line in _group_detail_lines(home_dir, env, cfg, tally))
    out.append("")
    info = _read_json(os.path.join(home_dir, "state", "last-sessionstart.json"))
    if info is None:
        out.append("last SessionStart: none")
    else:
        out.append("last SessionStart: ts %s" % info.get("ts", "?"))
        out.append("  link: %s" % info.get("link"))
        out.append("  tone_line: %s" % ("yes" if info.get("tone_line") else "no"))
    files = events.month_files(home_dir)
    oldest = os.path.basename(files[0])[:-len(".jsonl")] if files else "none"
    months = cfg["retention_months"]
    if months <= 0:
        out.append("retention: unlimited, oldest month kept %s" % oldest)
    else:
        out.append("retention: %d months, cutoff month %s, oldest month kept %s"
                   % (months, _cutoff_month(months, now), oldest))
    return ["<details><summary>details</summary>", "", "```"] + out + ["```", "", "</details>"]


def report(home: str, env: Mapping, home_dir_for_profiles: Optional[str] = None, full: bool = False,
           now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    cfg = config.load_config(home)
    real_home = home_dir_for_profiles if home_dir_for_profiles is not None else os.path.expanduser("~")
    counters = {}  # type: Dict[str, int]
    tally = _Tally()
    for obj in events.iter_events(home, counters=counters):
        tally.add(obj)
    missing_ts = _interpreter_missing_ts(home)
    missing = False
    if missing_ts is not None:
        missing_epoch = config.ts_epoch(missing_ts)
        missing = tally.newest_hook_any is None or (missing_epoch is not None
                                                    and missing_epoch > tally.newest_hook_any)
    disabled = env.get("APPRENTICE_DISABLE") == "1" or os.path.exists(os.path.join(home, "disabled"))
    errors = _recent_errors(home, now)
    profiles = _profiles(real_home, cfg, tally, missing, now)
    bad = counters.get("bad", 0)
    attention = bool(errors or profiles.stale or missing or bad)
    newest_transcript = _newest_transcript_any(profile_dirs(real_home))
    verdict = _verdict(disabled, tally, newest_transcript, attention, now)

    blocks = ["**apprentice** %s · %s · %s" % (verdict, _plural(tally.total(), "event"), _plural(errors, "error"))]
    blocks.append("\n".join(_profile_table(profiles)))
    kinds = _kind_table(tally)
    if kinds:
        blocks.append("\n".join(kinds))
    profile = config.current_profile(env)
    group = config.group_of_profile(profile, cfg, config.read_group_labels(home))
    blocks.append("\n".join(_md_table(["review", "state"], _review_rows(home, now, group))))
    notes = []  # type: List[Optional[str]]
    if profiles.shared:
        notes.append("ℹ transcripts are shared across %d profiles; set profile_rules in config.json "
                     "to attribute them per profile." % len(profiles.shared))
    notes.append(_backfill_note(home))
    notes.append(_group_note(home, env, cfg, tally))
    notes.append(_last_error_note(home))
    link_note, link_action = _session_start_problem(home, profile, group)
    notes.append(link_note)
    notes.append(_retention_note(cfg, tally, now))
    if bad:
        notes.append("⚠ %s skipped in the event store" % _plural(bad, "unreadable line"))
    for key in ("extra_redact_patterns", "machine_prompt_patterns"):
        ignored = config.ignored_pattern_count(home, key)
        if ignored:
            notes.append("⚠ %s: %s ignored" % (key, _plural(ignored, "invalid pattern")))
    blocks.extend(n for n in notes if n)

    actions = ["`%s`" % c for c in profiles.commands]
    if missing:
        actions.append(_PYTHON_REMEDY)
    if link_action:
        actions.append("`%s`" % link_action)
    if disabled:
        actions.append("`unset APPRENTICE_DISABLE`" if env.get("APPRENTICE_DISABLE") == "1"
                       else "`rm %s`" % os.path.join(home, "disabled"))
    if actions:
        blocks.append("**Actions:**\n\n" + "\n".join("%d. %s" % (i, a) for i, a in enumerate(actions, 1)))
    else:
        blocks.append("**Actions:** none")
    if full:
        blocks.append("\n".join(_details(home, env, cfg, tally, counters, now)))
    return "\n\n".join(blocks) + "\n"
