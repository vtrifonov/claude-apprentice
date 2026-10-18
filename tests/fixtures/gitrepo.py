import os
import shutil
import subprocess


def _identity_env(tmp):
    email = "alice" + chr(64) + "example.test"
    env = dict(os.environ)
    env.update({
        "HOME": tmp,
        "GIT_AUTHOR_NAME": "Alice",
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": "Alice",
        "GIT_COMMITTER_EMAIL": email,
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return env


def init_committed_local(tmp):
    """Create a real repo under tmp holding a committed CLAUDE.local.md; None when git is missing."""
    if shutil.which("git") is None:
        return None
    tmp = os.path.realpath(tmp)
    env = _identity_env(tmp)
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    with open(os.path.join(repo, "CLAUDE.local.md"), "w", encoding="utf-8") as fh:
        fh.write("local notes\n")
    for args in (["init", "-q"], ["add", "CLAUDE.local.md"],
                 ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", repo] + args, env=env, check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return repo
