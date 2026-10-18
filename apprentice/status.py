"""Plain-text status report: capture counts, backfill state, account groups and per-profile enablement."""
from __future__ import annotations

import fcntl
import json
import os
import re
import time

from apprentice import config, events, gitinfo

if False:  # annotations only
    from typing import Dict, List, Mapping, Optional

# C1/V13 may change the verb to "enable"; the tests read this constant rather than a literal.
ENABLE_VERB = "install"
PLUGIN_KEY = "apprentice@vtrifonov"
STALE_SECONDS = 24 * 3600
_TAIL_BYTES = 262144
_INTERPRETER_RE = re.compile(r"^(\S+) \S+ interpreter-missing")
_SKIPPED_RE = re.compile(r"skipped \(([^)]*)\)")
_LOCAL_NAME = "CLAUDE.local.md"
_LINE_CAP = 300


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


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def profile_dirs(real_home: str) -> List[str]:
    """`~/.claude` plus every `~/.claude-*` directory that holds a settings.json."""
    try:
        names = sorted(os.listdir(real_home))
    except OSError:
        return []
    out = []
    for name in names:
        if name != ".claude" and not name.startswith(".claude-"):
            continue
        path = os.path.join(real_home, name)
        if os.path.isfile(os.path.join(path, "settings.json")):
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


def _table(header: List[str], rows: List[List[str]]) -> List[str]:
    grid = [header] + rows
    widths = [max(len(r[i]) for r in grid) for i in range(len(header))]
    return ["  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)).rstrip() for r in grid]


def _pairs(counts: Dict[str, int]) -> List[str]:
    return ["  %s %d" % (k, counts[k]) for k in sorted(counts)] or ["  none"]


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


def _kind_month_lines(tally: _Tally) -> List[str]:
    months = sorted(tally.months)
    rows = [[kind] + [str(tally.by_kind_month[kind].get(m, 0)) for m in months]
            for kind in sorted(tally.by_kind_month)]
    if not rows:
        return ["  none"]
    return _table(["kind"] + months, rows)


def _error_lines(home_dir: str) -> List[str]:
    last = _read_json(os.path.join(home_dir, "state", "last-error.json"))
    if last is None:
        out = ["last hook error: none"]
    else:
        out = ["last hook error: %s %s %s" % (last.get("ts", "?"), last.get("event", "?"),
                                               str(last.get("message", ""))[:_LINE_CAP])]
    try:
        size = os.path.getsize(os.path.join(home_dir, "hook-errors.log"))
    except OSError:
        size = 0
    out.append("hook-errors.log: %d bytes" % size)
    return out


def _backfill_lines(home_dir: str) -> List[str]:
    text = _read_tail(os.path.join(home_dir, "state", "backfill.log"))
    lines = [l for l in (text or "").splitlines() if l.strip()]
    out = ["backfill running: %s" % ("yes" if _backfill_running(home_dir) else "no")]
    if lines:
        out.append("backfill log last line: %s" % lines[-1][:_LINE_CAP])
    for line in reversed(lines):
        if line.startswith("backfill done") or line.startswith("backfill interrupted"):
            match = _SKIPPED_RE.search(line)
            if match:
                out.append("last backfill skipped: %s" % match.group(1))
            break
    return out


def _group_lines(home_dir: str, env: Mapping, cfg: dict, tally: _Tally) -> List[str]:
    labels = config.read_group_labels(home_dir)
    profile = config.current_profile(env)
    current = config.group_of_profile(profile, cfg, labels)
    out = ["current profile: %s" % profile, "current group: %s" % (current if current is not None else "no label yet")]
    names = set((cfg.get("account_groups") or {}).keys()) | set(labels.values())
    per_group = {}  # type: Dict[str, int]
    unlabelled = 0
    for name, count in tally.by_profile.items():
        group = config.group_of_profile(name, cfg, labels)
        if group is None:
            # unknown is never labelled; the withheld line below reports it
            if name != "unknown":
                unlabelled += count
        else:
            per_group[group] = per_group.get(group, 0) + count
    names |= set(per_group)
    for name in sorted(names):
        members = config.group_profiles(name, cfg, labels)
        out.append("%s: profiles %s; events %d" % (name, ", ".join(members) or "none", per_group.get(name, 0)))
    if unlabelled:
        out.append("no label yet: events %d" % unlabelled)
    unknown_events = tally.by_profile.get("unknown", 0)
    if cfg.get("unknown_group") is not None:
        out.append("unknown_group: %s" % cfg["unknown_group"])
    elif unknown_events:
        out.append("unknown text withheld (%d events with an unknown profile)" % unknown_events)
    return out


def _session_start_lines(home_dir: str) -> List[str]:
    info = _read_json(os.path.join(home_dir, "state", "last-sessionstart.json"))
    if info is None:
        return ["last SessionStart: none"]
    link = info.get("link")
    out = ["last SessionStart: ts %s" % info.get("ts", "?"), "  link: %s" % link]
    if link == "skipped-not-excluded":
        root = gitinfo.find_git(info["cwd"]) if isinstance(info.get("cwd"), str) else None
        path = gitinfo.exclude_path(root) if root else None
        out.append("  exclude_path: %s" % (path or "unknown"))
        out.append("  add the line %s to that file, then start a new session" % _LOCAL_NAME)
    out.append("  tone_line: %s" % ("yes" if info.get("tone_line") else "no"))
    return out


def _retention_lines(home_dir: str, cfg: dict) -> List[str]:
    files = events.month_files(home_dir)
    oldest = os.path.basename(files[0])[:-len(".jsonl")] if files else "none"
    months = cfg["retention_months"]
    if months <= 0:
        return ["retention: unlimited, oldest month kept %s" % oldest]
    now = time.gmtime()
    index = now.tm_year * 12 + now.tm_mon - 1 - months
    return ["retention: %d months, cutoff month %04d-%02d, oldest month kept %s"
            % (months, index // 12, index % 12 + 1, oldest)]


def _enablement_lines(home_dir_for_profiles: str, cfg: dict, tally: _Tally, missing: bool) -> List[str]:
    rows = []
    commands = []
    dirs = profile_dirs(home_dir_for_profiles)
    by_rule = _newest_transcript_by_rule(dirs, cfg) if cfg.get("profile_rules") else None
    for path in dirs:
        name = _profile_name(path)
        enabled = plugin_enabled(os.path.join(path, "settings.json"))
        live = tally.newest_hook.get(name)
        transcript = by_rule.get(name) if by_rule is not None else _newest_transcript(path)
        flags = []
        if enabled is False:
            flags.append("NOT ENABLED")
        elif transcript is not None and (live is None or transcript - live > STALE_SECONDS):
            flags.append("INTERPRETER MISSING" if missing else "STALE")
        rows.append([name, {True: "yes", False: "no", None: "unknown"}[enabled],
                     _iso(live) if live is not None else "-",
                     _iso(transcript) if transcript is not None else "-", " ".join(flags)])
        if enabled is not True:
            commands.append("CLAUDE_CONFIG_DIR=%s claude plugin %s %s" % (path, ENABLE_VERB, PLUGIN_KEY))
    if not rows:
        return ["  no profile directories found"]
    out = _table(["profile", "enabled", "newest live event", "newest transcript", "flag"], rows)
    if commands:
        out.append("to enable a profile, run:")
        out.extend("  " + c for c in commands)
    return out


def report(home: str, env: Mapping, home_dir_for_profiles: Optional[str] = None) -> str:
    cfg = config.load_config(home)
    real_home = home_dir_for_profiles if home_dir_for_profiles is not None else os.path.expanduser("~")
    counters = {}  # type: Dict[str, int]
    tally = _Tally()
    for obj in events.iter_events(home, counters=counters):
        tally.add(obj)
    out = []  # type: List[str]
    missing_ts = _interpreter_missing_ts(home)
    missing = False
    if missing_ts is not None:
        missing_epoch = config.ts_epoch(missing_ts)
        missing = tally.newest_hook_any is None or (missing_epoch is not None
                                                    and missing_epoch > tally.newest_hook_any)
    if missing:
        out.append("interpreter missing since %s (hooks are not running; reinstall the Command Line Tools "
                   "or set APPRENTICE_PYTHON)" % missing_ts)
    disabled = env.get("APPRENTICE_DISABLE") == "1" or os.path.exists(os.path.join(home, "disabled"))
    out.append("disabled: %s" % ("yes" if disabled else "no"))
    out.append("")
    out.append("events by kind and month:")
    out.extend(_kind_month_lines(tally))
    out.append("events: live %d, backfilled %d, total %d"
               % (tally.sources["hook"], tally.sources["backfill"], sum(tally.sources.values())))
    out.append("skipped lines: bad %d, future %d" % (counters.get("bad", 0), counters.get("future", 0)))
    out.extend(_error_lines(home))
    out.append("")
    out.extend(_backfill_lines(home))
    out.append("")
    out.append("events by profile:")
    out.extend(_pairs(tally.by_profile))
    out.append("events by entrypoint:")
    out.extend(_pairs(tally.by_entrypoint))
    out.append("entrypoint_unknown: %d" % tally.entrypoint_unknown)
    out.append("")
    out.append("account groups:")
    out.extend("  " + line for line in _group_lines(home, env, cfg, tally))
    out.append("")
    out.extend(_session_start_lines(home))
    out.extend(_retention_lines(home, cfg))
    out.append("")
    out.append("profile enablement:")
    out.extend(_enablement_lines(real_home, cfg, tally, missing))
    return "\n".join(out) + "\n"
