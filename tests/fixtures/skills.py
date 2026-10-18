import os


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def build(tmp, real_home):
    """Create skill trees under <real_home>/.claude; return paths by name."""
    tmp = os.path.realpath(tmp)
    claude = os.path.join(real_home, ".claude")
    out = {"claude": claude}
    out["plain"] = os.path.join(claude, "skills", "plain-skill")
    _write(os.path.join(out["plain"], "SKILL.md"), "# plain\n")
    repo = os.path.join(tmp, "skillrepo")
    _write(os.path.join(repo, ".git", "config"),
           '[remote "origin"]\n\turl = https://example.test/acme/skills.git\n')
    _write(os.path.join(repo, ".git", "HEAD"), "ref: refs/heads/main\n")
    _write(os.path.join(repo, "tracked-skill", "SKILL.md"), "# tracked\n")
    out["tracked_target"] = os.path.join(repo, "tracked-skill")
    out["repo"] = repo
    out["tracked"] = os.path.join(claude, "skills", "tracked-skill")
    os.makedirs(os.path.dirname(out["tracked"]), exist_ok=True)
    os.symlink(out["tracked_target"], out["tracked"])
    out["synced"] = os.path.join(claude, "skills", "synced", "abc123", "synced-skill")
    _write(os.path.join(out["synced"], "SKILL.md"), "# synced\n")
    out["cache"] = os.path.join(claude, "plugins", "cache", "x", "skills", "cache-skill")
    _write(os.path.join(out["cache"], "SKILL.md"), "# cache\n")
    out["mirrored"] = os.path.join(claude, "skills", "mirrored-skill")
    _write(os.path.join(out["mirrored"], "SKILL.md"), "# mirrored\n")
    return out
