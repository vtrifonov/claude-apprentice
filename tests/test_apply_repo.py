import os
import tempfile
from unittest import mock

from apprentice import apply, config, gitinfo, ledger, worktrees
from tests.base import REPO_ROOT, ApprenticeTestCase
from tests.fixtures import gitrepo
from tests.fixtures import worktrees as wt_fixture

_FAKE_GIT = os.path.join(REPO_ROOT, "tests", "fixtures", "fake-git")
R1 = "r_0000000a"
TEXT = "Run the linter before pushing."
REPO = "acme/widgets"
LOCAL = "CLAUDE.local.md"


class RepoCase(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.tree = wt_fixture.build(os.path.join(self.fake_home, "trees"))
        wt_fixture.make_git_ready(self.tree)
        self.cfg = config.load_config(self.home)

    def local(self, name="main"):
        return os.path.join(self.tree[name], LOCAL)

    def apply_repo(self, name="main", repo=REPO, rule_id=R1, text=TEXT, dry_run=False, **kw):
        return apply.apply_repo(self.local(name), repo, rule_id, text, 4, "high", "2026-10-18", self.home,
                                self.cfg, dry_run, **kw)

    def read(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def exclude_lines(self, name):
        text = self.read(gitinfo.exclude_path(self.tree[name]))
        return [line for line in text.splitlines() if line == LOCAL]


class ExcludeLineTest(RepoCase):
    def test_exclude_written_before_file_on_linked_bare_submodule_sepgit(self):
        sub_config = os.path.join(self.tree["super"], ".git", "modules", "sub", "config")
        with open(sub_config, "a", encoding="utf-8") as fh:
            fh.write('[remote "origin"]\n\turl = https://example.test/acme/widgets.git\n')
        for name in ("main", "sub", "sep"):
            out = self.apply_repo(name)
            self.assertEqual(out["result"], "written", (name, out))
            self.assertEqual(self.exclude_lines(name), [LOCAL], name)
            self.assertTrue(os.path.isfile(self.local(name)), name)
            self.assertIn(TEXT, self.read(self.local(name)))
        self.assertEqual(os.stat(self.local("main")).st_mode & 0o777, 0o600)
        bare = self.apply_repo("barewt")
        self.assertEqual(bare["result"], "skipped:no-main-checkout")
        self.assertFalse(os.path.lexists(self.local("barewt")))
        bare_exclude = os.path.join(self.tree["bare"], "info", "exclude")
        self.assertFalse(os.path.exists(bare_exclude))

    def test_exclude_failure_creates_no_file(self):
        info = os.path.join(self.tree["main"], ".git", "info")
        os.rmdir(info)
        self.write(info, "not a directory\n")
        out = self.apply_repo()
        self.assertEqual(out["result"], "skipped:exclude-failed")
        self.assertFalse(os.path.lexists(self.local()))
        self.assertEqual(ledger.read_applied(self.home), [])

    def test_ensure_exclude_line_added_present_failed_and_info_created(self):
        main = self.tree["main"]
        info = os.path.join(main, ".git", "info")
        os.rmdir(info)
        self.assertEqual(worktrees.ensure_exclude_line(main), "added")
        self.assertEqual(worktrees.ensure_exclude_line(main), "present")
        self.assertEqual(self.exclude_lines("main"), [LOCAL])
        self.write(os.path.join(info, "exclude"), "*.log")
        self.assertEqual(worktrees.ensure_exclude_line(main), "added")
        self.assertEqual(self.read(os.path.join(info, "exclude")), "*.log\n" + LOCAL + "\n")
        self.assertEqual(worktrees.ensure_exclude_line(os.path.join(self.fake_home, "nowhere")), "failed")


class FanOutTest(RepoCase):
    def test_fan_out_links_two_worktrees_absolute_target(self):
        self.apply_repo()
        for name in ("wt1", "wt2"):
            link = self.local(name)
            self.assertTrue(os.path.islink(link), name)
            self.assertEqual(os.readlink(link), self.local("main"))
            self.assertTrue(os.path.isabs(os.readlink(link)))

    def test_fan_out_skips_existing_file_and_foreign_symlink(self):
        self.write(self.local("main"), "mine\n")
        self.write(self.local("wt1"), "worktree notes\n")
        elsewhere = self.write(os.path.join(self.fake_home, "other.md"), "other\n")
        os.symlink(elsewhere, self.local("wt2"))
        out = self.apply_repo()
        self.assertEqual(out["links"], [{"worktree": self.tree["wt1"], "result": "skipped-file-exists"},
                                        {"worktree": self.tree["wt2"], "result": "skipped-foreign-symlink"}])
        self.assertEqual(self.read(self.local("wt1")), "worktree notes\n")
        self.assertEqual(os.readlink(self.local("wt2")), elsewhere)

    def test_fan_out_reports_linked_then_present(self):
        self.write(self.local("main"), "mine\n")
        first = worktrees.fan_out(self.tree["main"])
        self.assertEqual([r["result"] for r in first], ["linked", "linked"])
        second = worktrees.fan_out(self.tree["main"])
        self.assertEqual([r["result"] for r in second], ["present", "present"])

    def test_git_clean_removed_link_restored_by_sessionstart(self):
        self.apply_repo()
        os.unlink(self.local("wt1"))
        status, content = worktrees.ensure_link(self.tree["wt1"])
        self.assertEqual(status, "created")
        self.assertIn(TEXT, content)
        self.assertEqual(os.readlink(self.local("wt1")), self.local("main"))

    def test_link_entries_in_applied_ledger(self):
        self.apply_repo()
        rows = ledger.read_applied(self.home)
        self.assertEqual([r["action"] for r in rows], ["apply", "link", "link"])
        links = rows[1:]
        self.assertEqual(sorted(r["file"] for r in links), [self.local("wt1"), self.local("wt2")])
        self.assertEqual({r["result"] for r in links}, {"linked"})
        self.assertEqual({r["id"] for r in links}, {R1})


class GuardTest(RepoCase):
    def test_hand_made_local_gets_exclude_then_rules(self):
        self.write(self.local(), "# my notes\n")
        out = self.apply_repo()
        self.assertEqual(out["result"], "written")
        self.assertEqual(self.exclude_lines("main"), [LOCAL])
        text = self.read(self.local())
        self.assertTrue(text.startswith("# my notes\n"))
        self.assertIn(TEXT, text)
        accepted = [e for e in ledger.read_proposals(self.home) if e["id"] == R1]
        self.assertEqual(accepted[0]["status"], "accepted")
        self.assertEqual(accepted[0]["scope"], "repo")
        self.assertEqual(accepted[0]["target"], self.local())

    def test_committed_local_refused_with_and_without_confirm_exclude_byte_identical(self):
        repo = gitrepo.init_committed_local(self.fake_home)
        if repo is None:
            self.skipTest("git not installed")
        with open(os.path.join(repo, ".git", "config"), "a", encoding="utf-8") as fh:
            fh.write('[remote "origin"]\n\turl = https://example.test/acme/widgets.git\n')
        target = os.path.join(repo, LOCAL)
        exclude = os.path.join(repo, ".git", "info", "exclude")

        def snapshot():
            with open(target, "rb") as fh, open(exclude, "rb") as fx:
                return fh.read(), fx.read()

        before = snapshot()
        for _ in range(2):
            out = apply.apply_repo(target, REPO, R1, TEXT, 4, "high", "2026-10-18", self.home, self.cfg, False)
            self.assertEqual(out["result"], "skipped:git-tracked-local")
            self.assertIn("global", out["message"])
            self.assertEqual(snapshot(), before)
        self.assertEqual(ledger.read_applied(self.home), [])
        self.assertEqual(ledger.read_proposals(self.home), [])

    def test_fake_git_failure_gives_index_unknown(self):
        bindir = os.path.realpath(tempfile.mkdtemp(dir=self.home))
        os.symlink(_FAKE_GIT, os.path.join(bindir, "git"))
        env = {"PATH": bindir + os.pathsep + os.environ.get("PATH", ""), "FAKE_GIT_MODE": "exit128"}
        with mock.patch.dict(os.environ, env):
            out = self.apply_repo()
        self.assertEqual(out["result"], "skipped:git-index-unknown")
        self.assertIn("global", out["message"])
        self.assertFalse(os.path.lexists(self.local()))
        self.assertFalse(os.path.exists(gitinfo.exclude_path(self.tree["main"])))

    def test_bad_target_wrong_repo_or_not_main(self):
        self.assertEqual(self.apply_repo(repo="acme/other")["result"], "skipped:bad-target")
        self.assertEqual(self.apply_repo("wt1")["result"], "skipped:bad-target")
        wrong_name = os.path.join(self.tree["main"], "CLAUDE.md")
        out = apply.apply_repo(wrong_name, REPO, R1, TEXT, 4, "high", "2026-10-18", self.home, self.cfg, False)
        self.assertEqual(out["result"], "skipped:bad-target")
        sub_dir = os.path.join(self.tree["main"], "src", LOCAL)
        out = apply.apply_repo(sub_dir, REPO, R1, TEXT, 4, "high", "2026-10-18", self.home, self.cfg, False)
        self.assertEqual(out["result"], "skipped:no-main-checkout")
        self.assertFalse(os.path.lexists(self.local()))
        self.assertFalse(os.path.lexists(self.local("wt1")))

    def test_sensitive_text_refused_before_creating_file(self):
        token = "ghp_" + "b" * 24
        out = self.apply_repo(text="Use token " + token)
        self.assertTrue(out["result"].startswith("skipped:sensitive-text:"), out["result"])
        self.assertFalse(os.path.lexists(self.local()))

    def test_dry_run_writes_neither_exclude_nor_file(self):
        out = self.apply_repo(dry_run=True)
        self.assertEqual(out["result"], "written")
        self.assertIn("+- " + TEXT, out["diff"])
        self.assertFalse(os.path.lexists(self.local()))
        self.assertFalse(os.path.exists(gitinfo.exclude_path(self.tree["main"])))
        self.assertFalse(os.path.lexists(self.local("wt1")))
        self.assertEqual(ledger.read_applied(self.home), [])

    def test_second_apply_is_unchanged_and_exclude_stays_single(self):
        self.apply_repo()
        again = self.apply_repo()
        self.assertEqual(again["result"], "unchanged")
        self.assertEqual(self.exclude_lines("main"), [LOCAL])


class TwoRepoTest(RepoCase):
    def test_two_repo_candidate_alt_target_writes_to_that_checkout(self):
        config_path = os.path.join(self.tree["sepgit"], "config")
        with open(config_path, "w", encoding="utf-8") as fh:
            fh.write('[remote "origin"]\n\turl = https://example.test/acme/gadgets.git\n')
        out = self.apply_repo("sep", repo="acme/gadgets")
        self.assertEqual(out["result"], "written")
        self.assertEqual(out["file"], self.local("sep"))
        self.assertIn(TEXT, self.read(self.local("sep")))
        self.assertFalse(os.path.lexists(self.local("main")))
        self.assertFalse(os.path.lexists(self.local("wt1")))
        mismatch = self.apply_repo("sep", repo=REPO)
        self.assertEqual(mismatch["result"], "skipped:bad-target")
