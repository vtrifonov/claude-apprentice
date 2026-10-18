"""Scope inference, target resolution and alternative targets for review candidates."""
from __future__ import annotations

import glob
import json
import os
import re

from apprentice import apply, config, gitinfo, lexicon, status

if False:  # annotations only
    from typing import Dict, List, Optional, Set, Tuple

_ARTIFACTS = {}  # type: Dict[str, Set[str]]
_SKIPPED_DIRS = frozenset(["node_modules", "__pycache__", "dist", "build", "venv", ".venv"])
_NAME_RE = re.compile(r"""^\s*name\s*=\s*["']([^"']+)["']""")
_PUNCT = ".,;:()\"'"
_WS_SPLIT_RE = re.compile(r"\s+")
_GIT_FIELDS = ("tracked", "root", "remote", "visibility", "public_mirror")
_LOCAL_FILE = "CLAUDE.local.md"


def find_skill_dir(name: str, real_home: str) -> Optional[str]:
    claude = os.path.join(real_home, ".claude")
    candidates = [os.path.join(claude, "skills", name), os.path.join(claude, "local-skills", name)]
    candidates.extend(sorted(glob.glob(os.path.join(glob.escape(claude), "skills", "synced", "*", name))))
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "SKILL.md")):
            return candidate
    return None


def is_readonly_skill(path: str) -> bool:
    parts = os.path.realpath(path).split(os.sep)
    if "synced" in parts:
        return True
    for index in range(1, len(parts)):
        if parts[index] == "plugins" and parts[index - 1].startswith(".claude"):
            return True
    return False


def _package_names(main: str) -> Set[str]:
    try:
        with open(os.path.join(main, "package.json"), "r", encoding="utf-8") as fh:
            name = json.load(fh).get("name")
    except (OSError, ValueError, AttributeError):
        return set()
    if not isinstance(name, str) or not name:
        return set()
    name = name.lower()
    found = {name}
    if name.startswith("@") and "/" in name:
        found.add(name.split("/", 1)[1])
    return found


def _pyproject_names(main: str) -> Set[str]:
    try:
        with open(os.path.join(main, "pyproject.toml"), "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return set()
    in_project = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_project = stripped == "[project]"
            continue
        if in_project:
            match = _NAME_RE.match(line)
            if match:
                return {match.group(1).lower()}
    return set()


def repo_artifacts(main: str) -> Set[str]:
    cached = _ARTIFACTS.get(main)
    if cached is not None:
        return cached
    try:
        entries = sorted(os.listdir(main))
    except OSError:
        return set()
    found = _package_names(main) | _pyproject_names(main)
    for entry in entries:
        if entry.startswith(".") or entry in _SKIPPED_DIRS:
            continue
        if os.path.isdir(os.path.join(main, entry)):
            found.add(entry.lower())
    _ARTIFACTS[main] = found
    return found


def path_exists_in(main: str, token: str) -> bool:
    if "/" not in token or os.path.isabs(token) or ".." in token.split("/"):
        return False
    return os.path.lexists(os.path.join(main, token))


def names_repo_artifact(text: str, mains: List[str]) -> Optional[str]:
    """A directory name counts only when written as a path or in backticks; package names count bare."""
    stop = lexicon.stopwords()
    tokens = []  # type: List[Tuple[str, bool]]
    for raw in _WS_SPLIT_RE.split((text or "").lower()):
        token = raw.replace("`", "").strip(_PUNCT)
        if len(token) >= 3 and token not in stop:
            tokens.append((token, "`" in raw))
    for main in mains:
        artifacts = repo_artifacts(main)
        packages = _package_names(main) | _pyproject_names(main)
        for token, ticked in tokens:
            if token in packages or (ticked and token in artifacts) or path_exists_in(main, token):
                return main
    return None


def _skill_named(text: str, names: List[str]) -> Optional[str]:
    """A skill name counts as a match only as `/name`, `name skill` or in backticks."""
    lowered = (text or "").lower()
    for name in names:
        if not name:
            continue
        escaped = re.escape(name.lower())
        pattern = (r"(?<![\w-])/" + escaped + r"(?![\w-])|(?<![\w-])" + escaped + r"\s+skill\b|`" + escaped + "`")
        if re.search(pattern, lowered):
            return name
    return None


def _names_in(text: str, names: List[str]) -> Optional[str]:
    lowered = (text or "").lower()
    for name in names:
        if name and re.search(r"(?<![\w-])" + re.escape(name.lower()) + r"(?![\w-])", lowered):
            return name
    return None


def _ts_key(ts) -> Tuple[float, str]:
    return (config.ts_epoch(ts) or 0.0, ts or "")


def _visible_text(kind: str, found: dict) -> str:
    if kind == "correction":
        visible = [pair for pair in found.get("pairs") or [] if pair.get("visible")]
        if not visible:
            return ""
        newest = max(visible, key=lambda pair: _ts_key(pair.get("ts")))
        return newest["event"].get("text") or ""
    parts = list(found.get("display_questions") or []) + list(found.get("free_texts") or [])
    return "\n".join(parts)


def _evidence_repos(kind: str, found: dict) -> List[Optional[str]]:
    rows = found.get("pairs") if kind == "correction" else found.get("rows")
    return [row.get("repo") for row in rows or []]


def infer_scope(kind: str, cluster_or_group: dict, cfg: dict, known_skill_names: List[str],
                evidence_mains: List[str]) -> Tuple[str, Optional[str]]:
    found = cluster_or_group
    text = _visible_text(kind, found)
    if kind == "correction":
        named = _skill_named(text, known_skill_names)
        if named is not None:
            return "skill", named
        if _names_in(text, config.effective_tables(cfg)["phase_words"]) is not None:
            return "skill", None
    else:
        hint = found.get("skill") or next((row["skill_hint"] for row in found.get("rows") or []
                                           if row.get("skill_hint")), None)
        if hint:
            return "skill", hint
    repos = _evidence_repos(kind, found)
    if repos and None not in repos and "" not in repos and len(set(repos)) == 1:
        return "repo", None
    if names_repo_artifact(text, evidence_mains) is not None:
        return "repo", None
    return "global", None


class TargetResolver:
    def __init__(self, cfg: dict, real_home: str, labels: Dict[str, str], tracked_cache: dict) -> None:
        self.cfg = cfg
        self.real_home = real_home
        self.labels = labels
        self.tracked_cache = tracked_cache
        self._profile_dirs = None  # type: Optional[List[str]]
        self._global_git = None  # type: Optional[dict]
        self._global_git_done = False

    def _tracked(self, main: str) -> Optional[bool]:
        if main not in self.tracked_cache:
            self.tracked_cache[main] = gitinfo.is_tracked(main, _LOCAL_FILE)
        return self.tracked_cache[main]

    def repo_target(self, cwds_newest_first: List[str], repo: str, visible_any: bool) -> dict:
        result = {"target": None, "target_status": "no-checkout", "text_visible": visible_any}
        suffix = self.cfg.get("worktree_suffix") or ""
        for cwd in cwds_newest_first:
            main = gitinfo.resolve_main_for_cwd(cwd, suffix)
            if main is None or not os.path.isdir(main) or gitinfo.origin(main) != repo:
                continue
            tracked = self._tracked(main)
            if tracked is True:
                result["target_status"] = "git-tracked-local"
            elif tracked is None:
                result["target_status"] = "unknown-tracking"
            else:
                result["target_status"] = "resolved"
                result["target"] = os.path.join(main, _LOCAL_FILE)
            return result
        return result

    def skill_target(self, name: Optional[str]) -> Optional[dict]:
        directory = find_skill_dir(name, self.real_home) if name else None
        if directory is None:
            return None
        if is_readonly_skill(directory):
            return {"name": name, "target": self.global_target()["target"],
                    "target_status": "rescoped-readonly", "target_git": self.global_git()}
        target = os.path.join(directory, "SKILL.md")
        info = apply.inspect_target(target, self.cfg)
        git = {key: info[key] for key in _GIT_FIELDS if key in info}
        if not info["tracked"]:
            git.pop("visibility", None)
        return {"name": name, "target": target, "target_status": "resolved", "target_git": git}

    def global_target(self) -> dict:
        target = os.path.realpath(os.path.join(self.real_home, ".claude", "CLAUDE.md"))
        current = config.group_of_profile(config.current_profile(), self.cfg, self.labels)
        shared = set()
        if self._profile_dirs is None:
            self._profile_dirs = status.profile_dirs(self.real_home)
        for path in self._profile_dirs:
            base = os.path.basename(path.rstrip("/"))
            group = config.group_of_profile("default" if base == ".claude" else base, self.cfg, self.labels)
            claude_md = os.path.join(path, "CLAUDE.md")
            if group is None or group == current or not os.path.lexists(claude_md):
                continue
            if os.path.realpath(claude_md) == target:
                shared.add(group)
        return {"target": target, "target_shared_groups": sorted(shared)}

    def global_git(self) -> Optional[dict]:
        """`target_git` of the global CLAUDE.md: set only when it sits inside a git work tree (a dotfiles or
        config repo), so the tracked block, `--confirm-tracked` and `recommend` apply to it; else None."""
        if not self._global_git_done:
            target = self.global_target()["target"]
            info = apply.inspect_target(target, self.cfg)
            self._global_git = {key: info[key] for key in _GIT_FIELDS if key in info} if info["tracked"] else None
            self._global_git_done = True
        return self._global_git

    def alt_targets(self, candidate_scope: str, skill_name: Optional[str], repo_cwds: Dict[str, List[str]],
                    repo_visible: Dict[str, bool]) -> dict:
        repos = {repo: self.repo_target(cwds, repo, bool(repo_visible.get(repo)))
                 for repo, cwds in sorted(repo_cwds.items())}
        return {"global": dict(self.global_target(), target_git=self.global_git()), "repo": repos,
                "skill": self.skill_target(skill_name)}

    @staticmethod
    def recommend(target_status: str, target_git: Optional[dict]) -> str:
        if target_status in ("git-tracked-local", "unknown-tracking"):
            return "rescope-global"
        if target_git and target_git.get("tracked") and target_git.get("visibility") in ("public", "unknown"):
            return "rescope-global"
        return "accept"
