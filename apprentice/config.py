import os
import json
import re
import datetime
import fcntl
from typing import Dict, List, Mapping, Optional, Tuple

DEFAULTS = {
    "display_name": "the user",
    "own_email_domains": [],
    "skill_hints": [],
    "number_patterns": [],
    "header_aliases": {"model set": "models", "quick mode": "mode"},
    "risk_words": [],
    "phase_words": [],
    "capture_entrypoints": ["cli", "claude-desktop"],
    "profile_rules": [],
    "account_groups": {},
    "unknown_group": None,
    "public_skills": [],
    "worktree_suffix": "-worktrees",
    "extra_redact_patterns": [],
    "retention_months": 24,
}

_DIRS = ("events", "state", "state/pending-ask", "state/tmp", "state/preclass",
         "ledger", "review", "profile", "backups")

SKILL_HINTS = [
    ("quick mode?", "stem", "oneshot-pipeline"),
    ("use fable", "stem", "oneshot-pipeline"),
    ("keep quick", "stem", "oneshot-pipeline"),
    ("use frontend-design", "stem", "oneshot-pipeline"),
    ("pr size budget", "header_or_stem", "oneshot-pipeline"),
    ("provisional lane", "header_or_stem", "oneshot-pipeline"),
    ("approve|go autonomous", "both", "oneshot-pipeline"),
]  # type: List[Tuple[str, str, str]]

RISK_WORDS = ["auth", "trust", "security", "prod", "production", "migration", "persistence",
              "concurrency", "deploy", "contract", "public api", "secret"]
NEGATIONS = ("no", "not", "without")

_PCT = r"(\d+(?:\.\d+)?)"
NUMBER_EXTRACTORS = [
    (re.compile(_PCT + r"%\s*(?:of\s+(?:the\s+)?weekly(?:\s+limit)?\s+)?used", re.I), "used_pct", "number"),
    (re.compile(r"used\s+" + _PCT + "%", re.I), "used_pct", "number"),
    (re.compile(r"weekly\s+usage(?:\s+is)?\s+" + _PCT + "%", re.I), "used_pct", "number"),
    (re.compile(_PCT + r"%\s*(?:/\s*day|per\s+day)", re.I), "per_day_pct", "number"),
    (re.compile(r"(\d+)\s+working\s+days?\s+(?:left|until)", re.I), "working_days_left", "number"),
    (re.compile(r"(\d+)\s+(?:changed\s+)?lines\b", re.I), "lines", "number"),
    (re.compile(r"(\d+)\s+files\b", re.I), "files", "number"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*KB\b", re.I), "kb", "number"),
    (re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=([^\s,;)]+)", re.I), "KEYVAL", "auto"),
]  # type: List[Tuple[object, str, str]]

RESET_AT_RE = re.compile(
    r"\breset(?:s)?\s+(?:at\s+|on\s+)?(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?(?:\s*[+-]\d{2}:?\d{2}|Z)?)?)",
    re.I)
PHASE_WORDS = ["shepherd", "checkpoint", "panel", "lane", "quick mode"]
STRONG_MARKERS = ["but i said", "i thought we", "store in your memory", "remember to",
                  "remember that", "from now on"]

_RESERVED_GROUP_RE = re.compile(r"^group-\d+$")
_HINT_FIELDS = ("stem", "header_or_stem", "both")


def home() -> str:
    env = os.environ.get("APPRENTICE_HOME")
    if env:
        return os.path.expanduser(env)
    return os.path.expanduser(os.path.join("~", ".claude", "apprentice"))


def ensure_dirs(home_dir: str) -> None:
    os.makedirs(home_dir, mode=0o700, exist_ok=True)
    for sub in _DIRS:
        path = os.path.join(home_dir, sub)
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)


def utc_now_iso() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


def parse_iso(ts: str) -> Optional[datetime.datetime]:
    try:
        text = ts.strip()
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def log_error(event: str, message: str, home_dir: Optional[str] = None) -> None:
    try:
        base = home_dir if home_dir is not None else home()
        ts = utc_now_iso()
        msg = " ".join(str(message).split())[:300]
        event = "-".join(str(event).split())
        os.makedirs(base, mode=0o700, exist_ok=True)
        line = ("%s %s %s\n" % (ts, event, msg)).encode("utf-8", "replace")
        fd = os.open(os.path.join(base, "hook-errors.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        state = os.path.join(base, "state")
        os.makedirs(state, mode=0o700, exist_ok=True)
        tmp = os.path.join(state, "last-error.json.%d.tmp" % os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps({"ts": ts, "event": event, "message": msg}).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, os.path.join(state, "last-error.json"))
    except Exception:
        pass


def group_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", name.lower()).lstrip("-")


def current_profile(env: Optional[Mapping] = None) -> str:
    source = os.environ if env is None else env
    raw = source.get("CLAUDE_CONFIG_DIR") or ""
    if not raw:
        return "default"
    stripped = raw.rstrip("/")
    if not stripped:
        return "default"
    default_dir = os.path.realpath(os.path.expanduser(os.path.join("~", ".claude")))
    if os.path.realpath(os.path.expanduser(stripped)) == default_dir:
        return "default"
    base = os.path.basename(stripped) or "default"
    return "unknown-dir" if base == "unknown" else base


def _labels_file(home_dir: str) -> str:
    return os.path.join(home_dir, "state", "group-labels.json")


def read_group_labels(home_dir: str) -> Dict[str, str]:
    try:
        with open(_labels_file(home_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def assign_group_labels(home_dir: str, first_seen: Dict[str, str]) -> Dict[str, str]:
    state = os.path.join(home_dir, "state")
    os.makedirs(state, mode=0o700, exist_ok=True)
    lock_fd = os.open(os.path.join(state, "group-labels.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if os.path.exists(_labels_file(home_dir)):
            try:
                with open(_labels_file(home_dir), "r", encoding="utf-8") as fh:
                    parsed = json.load(fh)
                if not isinstance(parsed, dict):
                    raise ValueError("not an object")
            except Exception:
                log_error("distill", "group-labels.json unreadable, not assigning labels (rule: group-labels-corrupt)",
                          home_dir)
                return {}
        labels = read_group_labels(home_dir)
        highest = 0
        for label in labels.values():
            if _RESERVED_GROUP_RE.match(label):
                highest = max(highest, int(label.split("-", 1)[1]))
        fresh = sorted((p for p in first_seen if p not in labels),
                       key=lambda p: (first_seen[p], p))
        if not fresh:
            return labels
        for profile in fresh:
            highest += 1
            labels[profile] = "group-%d" % highest
        tmp = os.path.join(state, "group-labels.json.%d.tmp" % os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(labels, sort_keys=True).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, _labels_file(home_dir))
        return labels
    finally:
        os.close(lock_fd)


def group_of_profile(profile: str, cfg: dict, labels: Dict[str, str]) -> Optional[str]:
    if profile == "unknown":
        return cfg.get("unknown_group")
    for name, profiles in (cfg.get("account_groups") or {}).items():
        if profile in profiles:
            return name
    return labels.get(profile)


def group_profiles(group: str, cfg: dict, labels: Dict[str, str]) -> List[str]:
    explicit = (cfg.get("account_groups") or {}).get(group)
    if explicit is not None:
        return list(explicit)
    return sorted(p for p, label in labels.items() if label == group and group_of_profile(p, cfg, labels) == group)


def text_visible(event_profile: str, current_group: Optional[str], cfg: dict,
                 labels: Dict[str, str]) -> bool:
    if current_group is None:
        return False
    return group_of_profile(event_profile, cfg, labels) == current_group


def _warn_once_per_day(home_dir: str, message: str) -> None:
    try:
        day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        state = os.path.join(home_dir, "state")
        os.makedirs(state, mode=0o700, exist_ok=True)
        guard = os.path.join(state, "config-warned-" + day)
        fd = os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError:
        return
    except Exception:
        return
    log_error("config", message, home_dir)


def _account_groups_problem(groups) -> Optional[str]:
    if not isinstance(groups, dict):
        return "account-groups-shape"
    for name, profiles in groups.items():
        if not isinstance(name, str) or not isinstance(profiles, list):
            return "account-groups-shape"
        if not all(isinstance(p, str) for p in profiles):
            return "account-groups-shape"
    seen = set()
    for profiles in groups.values():
        for p in profiles:
            if p in seen:
                return "profile-in-two-groups"
            seen.add(p)
    if "unknown" in seen:
        return "unknown-listed"
    slugs = set()
    for name in groups:
        slug = group_slug(name)
        if slug in slugs:
            return "slug-collision"
        slugs.add(slug)
    for name in groups:
        if _RESERVED_GROUP_RE.match(name) or _RESERVED_GROUP_RE.match(group_slug(name)):
            return "reserved-group-name"
    return None


def load_config(home_dir: Optional[str] = None) -> dict:
    base = home_dir if home_dir is not None else home()
    cfg = json.loads(json.dumps(DEFAULTS))
    path = os.path.join(base, "config.json")
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except Exception:
        _warn_once_per_day(base, "config.json malformed, using defaults (rule: malformed-file)")
        return cfg
    user_aliases = data.pop("header_aliases", None)
    cfg.update(data)
    if isinstance(user_aliases, dict):
        cfg["header_aliases"].update(user_aliases)
    elif user_aliases is not None:
        _warn_once_per_day(base, "header_aliases ignored (rule: header-aliases-shape)")

    problem = _account_groups_problem(cfg["account_groups"])
    if problem is not None:
        cfg["account_groups"] = {}
        _warn_once_per_day(base, "account_groups ignored (rule: %s)" % problem)

    unknown = cfg["unknown_group"]
    if unknown is not None:
        if not (isinstance(unknown, str)
                and (unknown in cfg["account_groups"] or _RESERVED_GROUP_RE.match(unknown))):
            cfg["unknown_group"] = None
            _warn_once_per_day(base, "unknown_group ignored (rule: unknown-group-name)")

    months = cfg["retention_months"]
    if not isinstance(months, int) or isinstance(months, bool) or months < 0:
        cfg["retention_months"] = DEFAULTS["retention_months"]
        _warn_once_per_day(base, "retention_months invalid (rule: retention-months)")

    entry = cfg["capture_entrypoints"]
    if not isinstance(entry, list) or not all(isinstance(e, str) for e in entry):
        cfg["capture_entrypoints"] = list(DEFAULTS["capture_entrypoints"])
        _warn_once_per_day(base, "capture_entrypoints invalid (rule: capture-entrypoints)")
    return cfg


def effective_tables(cfg: dict) -> dict:
    def listed(key: str) -> list:
        value = cfg.get(key)
        return value if isinstance(value, list) else []

    hints = list(SKILL_HINTS)
    for item in listed("skill_hints"):
        if (isinstance(item, dict) and isinstance(item.get("phrase"), str)
                and item.get("fields") in _HINT_FIELDS and isinstance(item.get("skill"), str)):
            hints.append((item["phrase"], item["fields"], item["skill"]))
    extractors = []
    for item in listed("number_patterns"):
        if not (isinstance(item, dict) and all(isinstance(item.get(k), str) for k in ("regex", "name", "type"))):
            continue
        try:
            extractors.append((re.compile(item["regex"], re.I), item["name"], item["type"]))
        except re.error:
            continue
    extractors.extend(NUMBER_EXTRACTORS)
    risk = list(RISK_WORDS) + [w for w in listed("risk_words") if isinstance(w, str)]
    phase = list(PHASE_WORDS) + [w for w in listed("phase_words") if isinstance(w, str)]
    return {
        "skill_hints": hints,
        "number_extractors": extractors,
        "risk_words": risk,
        "header_aliases": dict(cfg.get("header_aliases") or {}),
        "phase_words": phase,
    }
