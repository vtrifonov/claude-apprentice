import os

_BRANCH_HEAD = "ref: refs/heads/feat/XX-123-x\n"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _https_config():
    return '[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = https://example.test/acme/widgets.git\n'


def _ssh_config():
    url = "git" + chr(64) + "example" + ".test:acme/widgets.git"
    return '[core]\n\tbare = false\n[remote "origin"]\n\turl = %s\n' % url


def _linked(gitdir_parent, name, wt):
    admin = os.path.join(gitdir_parent, "worktrees", name)
    _write(os.path.join(admin, "commondir"), "../..\n")
    _write(os.path.join(admin, "gitdir"), os.path.join(wt, ".git") + "\n")
    _write(os.path.join(admin, "HEAD"), _BRANCH_HEAD)
    _write(os.path.join(wt, ".git"), "gitdir: %s\n" % admin)


def build(tmp):
    """Create git-shaped trees under tmp without running git; return paths by name."""
    tmp = os.path.realpath(tmp)
    main = os.path.join(tmp, "main")
    _write(os.path.join(main, ".git", "config"), _https_config())
    _write(os.path.join(main, ".git", "HEAD"), "ref: refs/heads/main\n")
    os.makedirs(os.path.join(main, ".git", "info"))
    wt1 = os.path.join(tmp, "main-worktrees", "wt1")
    wt2 = os.path.join(tmp, "main-worktrees", "wt2")
    _linked(os.path.join(main, ".git"), "wt1", wt1)
    _linked(os.path.join(main, ".git"), "wt2", wt2)

    bare = os.path.join(tmp, "bare.git")
    _write(os.path.join(bare, "config"), "[core]\n\tbare = true\n")
    _write(os.path.join(bare, "HEAD"), "ref: refs/heads/main\n")
    os.makedirs(os.path.join(bare, "info"))
    barewt = os.path.join(tmp, "barewt")
    _linked(bare, "barewt", barewt)

    sup = os.path.join(tmp, "super")
    sub = os.path.join(sup, "sub")
    modules = os.path.join(sup, ".git", "modules", "sub")
    _write(os.path.join(sup, ".git", "config"), _https_config())
    _write(os.path.join(sup, ".git", "HEAD"), "ref: refs/heads/main\n")
    _write(os.path.join(modules, "config"), "[core]\n\tworktree = ../../../sub\n")
    _write(os.path.join(modules, "HEAD"), "ref: refs/heads/main\n")
    _write(os.path.join(sub, ".git"), "gitdir: ../.git/modules/sub\n")

    sep = os.path.join(tmp, "sep")
    sepgit = os.path.join(tmp, "sepgit")
    _write(os.path.join(sepgit, "config"), _ssh_config())
    _write(os.path.join(sepgit, "HEAD"), "ref: refs/heads/main\n")
    _write(os.path.join(sep, ".git"), "gitdir: %s\n" % sepgit)

    return {
        "main": main,
        "wt1": wt1,
        "wt2": wt2,
        "bare": bare,
        "barewt": barewt,
        "super": sup,
        "sub": sub,
        "sep": sep,
        "sepgit": sepgit,
        "gone": os.path.join(tmp, "main-worktrees", "gone"),
    }
