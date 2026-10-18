"""Apply-path hardening: replay over edits, symlinked and hard-linked targets, backups, refusals in the ledger."""
import argparse
import errno
import fcntl
import os
import stat
import threading
from unittest import mock

from apprentice import apply, config, gitinfo, ledger, worktrees
from tests.base import ApprenticeTestCase
from tests.fixtures import gitrepo, skills
from tests.fixtures import worktrees as wt_fixture

R1 = "r_0000000a"
R2 = "r_0000000b"
TEXT = "Always run the tests."
REPO = "acme/widgets"
LOCAL = "CLAUDE.local.md"


def ns(**kw):
    values = dict(id=R1, text=TEXT, scope="global", target=None, pack=None, confirm_tracked=False, dry_run=False,
                  skill=None)
    values.update(kw)
    return argparse.Namespace(**values)


class HardeningCase(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = config.load_config(self.home)
        self.claude_md = self.write(os.path.join(self.fake_home, ".claude", "CLAUDE.md"), "# mine\nkeep me\n")

    def read(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def accept(self, **kw):
        return apply.apply_candidate(ns(**kw), self.home, self.cfg, self.fake_home)

    def replay(self, dry_run=False):
        return apply.replay(self.home, self.cfg, False, dry_run)


class ReplayEditedMovedTest(HardeningCase):
    def test_replay_leaves_hand_edited_line_and_records_edited(self):
        self.assertEqual(self.accept()["result"], "written")
        edited = self.read(self.claude_md).replace(TEXT, "Always run the unit tests, never e2e.")
        self.write(self.claude_md, edited)
        results = self.replay()
        self.assertEqual([r["result"] for r in results], ["edited"])
        self.assertEqual(self.read(self.claude_md), edited)
        row = ledger.read_applied(self.home)[-1]
        self.assertEqual((row["action"], row["id"], row["result"]), ("replay", R1, "edited"))

    def test_replay_reports_moved_line_and_writes_nothing(self):
        self.accept()
        lines = self.read(self.claude_md).splitlines(True)
        rule = [line for line in lines if R1 in line][0]
        lines.remove(rule)
        lines.insert(1, rule)
        moved = "".join(lines)
        self.write(self.claude_md, moved)
        results = self.replay()
        self.assertEqual([r["result"] for r in results], ["moved"])
        self.assertEqual(self.read(self.claude_md), moved)
        self.assertEqual(self.read(self.claude_md).count(R1), 1)


class SkillTargetNameTest(HardeningCase):
    def test_skill_target_must_be_skill_md(self):
        config_file = self.write(os.path.join(self.fake_home, ".ssh", "config"), "Host *\n")
        out = self.accept(scope="skill", target=config_file)
        self.assertEqual(out["result"], "skipped:bad-target")
        self.assertEqual(self.read(config_file), "Host *\n")
        link = os.path.join(self.fake_home, "SKILL.md")
        os.symlink(config_file, link)
        self.assertEqual(self.accept(scope="skill", target=link)["result"], "skipped:bad-target")
        tree = skills.build(self.home, self.fake_home)
        plain = os.path.join(tree["plain"], "SKILL.md")
        self.assertEqual(self.accept(scope="skill", target=plain)["result"], "written")


class WriteGuardTest(HardeningCase):
    def test_hardlinked_target_refused(self):
        sibling = os.path.join(self.fake_home, "hard.md")
        os.link(self.claude_md, sibling)
        before = self.read(self.claude_md)
        out = self.accept()
        self.assertEqual(out["result"], "skipped:hardlinked")
        self.assertEqual(self.read(self.claude_md), before)

    def test_restat_before_replace_catches_late_edit(self):
        calls = []
        real_same = apply._same

        def late_edit(real, data, st):
            ok = real_same(real, data, st)
            calls.append(1)
            if len(calls) == 1:
                with open(real, "a", encoding="utf-8") as fh:
                    fh.write("late save\n")
            return ok

        with mock.patch.object(apply, "_same", late_edit):
            out = self.accept()
        self.assertEqual(out["result"], "written")
        self.assertEqual(len(calls), 2)
        text = self.read(self.claude_md)
        self.assertIn("late save\n", text)
        self.assertIn(TEXT, text)

    def test_dry_run_creates_nothing_under_home(self):
        fresh = os.path.join(self.home, "fresh")
        out = apply.apply_candidate(ns(dry_run=True), fresh, self.cfg, self.fake_home)
        self.assertEqual(out["result"], "written")
        self.assertFalse(os.path.exists(fresh))

    def test_backups_pruned_to_newest(self):
        with mock.patch.object(apply, "BACKUP_KEEP", 3):
            for index in range(5):
                self.accept(id="r_000000%02x" % (index + 16), text="Rule %d." % index)
        directory = apply._backup_dir(self.home, os.path.realpath(self.claude_md))
        names = sorted(os.listdir(directory))
        self.assertEqual(len(names), 3)
        newest = self.read(os.path.join(directory, names[-1]))
        self.assertIn("Rule 3.", newest)
        self.assertNotIn("Rule 4.", newest)

    def test_backup_dirs_created_private(self):
        fresh = os.path.join(self.home, "fresh")
        apply.apply_candidate(ns(), fresh, self.cfg, self.fake_home)
        directory = apply._backup_dir(fresh, os.path.realpath(self.claude_md))
        for path in (fresh, os.path.join(fresh, "backups"), directory, os.path.join(fresh, "state")):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700, path)

    def test_long_slug_hashed(self):
        real = "/" + "/".join(["d" * 40] * 8) + "/CLAUDE.md"
        directory = apply._backup_dir(self.home, real)
        slug = os.path.basename(directory)
        self.assertLessEqual(len(slug.encode("utf-8")), 200)
        self.assertTrue(slug.startswith("CLAUDE.md__"))
        self.assertEqual(len(slug), len("CLAUDE.md__") + 12)
        short = apply._backup_dir(self.home, "/a/b/CLAUDE.md")
        self.assertEqual(os.path.basename(short), "__a__b__CLAUDE.md")

    def test_os_error_becomes_io_error(self):
        before = self.read(self.claude_md)

        def full(*_args, **_kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(apply, "_backup", full):
            out = self.accept()
        self.assertEqual(out["result"], "skipped:io-error")
        self.assertEqual(self.read(self.claude_md), before)
        self.assertEqual(ledger.read_applied(self.home)[-1]["result"], "skipped:io-error")


class SensitiveTest(HardeningCase):
    def test_allowlisted_prefixes_owner_and_home_path(self):
        check = apply.sensitive_check
        self.assertIsNone(check("Write files as UTF-8.", True, [], self.cfg))
        self.assertIsNone(check("Hash with SHA-256.", True, [], self.cfg))
        self.assertIsNone(check("Use TLS-1 never, RFC-3339 dates, ISO-8601.", True, [], self.cfg))
        self.assertEqual(check("See XX-123 first.", True, [], self.cfg), "sensitive-text:ticket")
        self.assertEqual(check("Acme services use pnpm.", True, [REPO], self.cfg), "sensitive-text:repo")
        self.assertIsNone(check("Use the ab tool.", True, ["ab/widgets"], self.cfg))
        self.assertEqual(check("Scripts live in /Users/alice/code/bin.", True, [], self.cfg),
                         "sensitive-text:home-path")
        self.assertEqual(check("Scripts live in /home/bob/bin.", True, [], self.cfg), "sensitive-text:home-path")
        self.assertIsNone(check("Scripts live in /Users/alice/code/bin.", False, [REPO], self.cfg))


class RefusalLedgerTest(HardeningCase):
    def test_sensitive_and_tracked_refusals_recorded(self):
        token = "ghp_" + "c" * 24
        out = self.accept(text="Use token %s." % token)
        self.assertEqual(out["result"], "skipped:sensitive-text:github-token")
        row = ledger.read_applied(self.home)[-1]
        self.assertEqual((row["action"], row["id"], row["result"]), ("apply", R1, "skipped:sensitive-text:github-token"))
        self.assertEqual(self.accept(scope="nowhere")["result"], "skipped:bad-scope")
        self.assertEqual(ledger.read_applied(self.home)[-1]["result"], "skipped:bad-scope")

    def test_git_tracked_local_refusal_recorded(self):
        repo = gitrepo.init_committed_local(self.fake_home)
        if repo is None:
            self.skipTest("git not installed")
        with open(os.path.join(repo, ".git", "config"), "a", encoding="utf-8") as fh:
            fh.write('[remote "origin"]\n\turl = https://example.test/acme/widgets.git\n')
        target = os.path.join(repo, LOCAL)
        out = apply.apply_repo(target, REPO, R1, TEXT, 4, "high", "2026-10-18", self.home, self.cfg, False)
        self.assertEqual(out["result"], "skipped:git-tracked-local")
        rows = ledger.read_applied(self.home)
        self.assertEqual([(r["action"], r["id"], r["result"]) for r in rows], [("apply", R1, "skipped:git-tracked-local")])
        apply.apply_repo(target, REPO, R1, TEXT, 4, "high", "2026-10-18", self.home, self.cfg, True)
        self.assertEqual(len(ledger.read_applied(self.home)), 1)


class UndoAllFilesTest(HardeningCase):
    def test_undo_after_rescope_removes_from_every_written_file(self):
        tree = skills.build(self.home, self.fake_home)
        plain = os.path.join(tree["plain"], "SKILL.md")
        self.assertEqual(self.accept()["result"], "written")
        self.assertEqual(self.accept(scope="skill", target=plain)["result"], "written")
        out = apply.undo(R1, self.home, self.cfg)
        self.assertEqual([(r["file"], r["result"]) for r in out["results"]],
                         [(os.path.realpath(self.claude_md), "removed"), (plain, "removed")])
        self.assertNotIn(R1, self.read(self.claude_md))
        self.assertNotIn(R1, self.read(plain))
        undos = [r for r in ledger.read_applied(self.home) if r["action"] == "undo"]
        self.assertEqual(len(undos), 2)
        again = apply.undo(R1, self.home, self.cfg)
        self.assertEqual(again["result"], "skipped:not-applied")


class RepoCase(HardeningCase):
    def setUp(self):
        super().setUp()
        self.tree = wt_fixture.build(os.path.join(self.fake_home, "trees"))
        wt_fixture.make_git_ready(self.tree)
        self.main = self.tree["main"]
        self.local = os.path.join(self.main, LOCAL)
        self.exclude = gitinfo.exclude_path(self.main)

    def apply_repo(self, rule_id=R1, text=TEXT, dry_run=False):
        return apply.apply_repo(self.local, REPO, rule_id, text, 4, "high", "2026-10-18", self.home, self.cfg,
                                dry_run)

    def accept_repo(self):
        out = self.apply_repo()
        self.assertEqual(out["result"], "written")
        ledger.record_decision(self.home, None, R1, "accepted", text=TEXT, scope="repo", target=self.local)
        return out


class RepoSymlinkTest(RepoCase):
    def test_symlinked_local_refused_without_exclude(self):
        shared = self.write(os.path.join(self.fake_home, "shared", "CLAUDE.md"), "# team rules\n")
        os.symlink(shared, self.local)
        for dry_run in (True, False):
            out = self.apply_repo(dry_run=dry_run)
            self.assertEqual(out["result"], "skipped:local-is-symlink")
        self.assertEqual(self.read(shared), "# team rules\n")
        self.assertFalse(os.path.exists(self.exclude))

    def test_replay_refuses_symlinked_local(self):
        self.accept_repo()
        shared = self.write(os.path.join(self.fake_home, "shared", "CLAUDE.md"), "# team rules\n")
        os.unlink(self.local)
        os.symlink(shared, self.local)
        self.write(self.exclude, "*.log\n")
        results = self.replay()
        self.assertEqual([r["result"] for r in results], ["skipped:local-is-symlink"])
        self.assertEqual(self.read(shared), "# team rules\n")
        self.assertEqual(self.read(self.exclude), "*.log\n")


class RepoReplayMissingTest(RepoCase):
    def test_replay_recreates_deleted_local_with_exclude(self):
        self.accept_repo()
        os.unlink(self.local)
        self.write(self.exclude, "")
        dry = self.replay(dry_run=True)
        self.assertEqual([r["result"] for r in dry], ["written"])
        self.assertIn("+- " + TEXT, dry[0]["diff"])
        self.assertFalse(os.path.lexists(self.local))
        self.assertEqual(self.read(self.exclude), "")
        results = self.replay()
        self.assertEqual([r["result"] for r in results], ["written"])
        self.assertIn(TEXT, self.read(self.local))
        self.assertEqual(self.read(self.exclude), LOCAL + "\n")
        self.assertEqual(ledger.read_applied(self.home)[-1]["action"], "replay")


class ExcludeLineTest(RepoCase):
    def test_shared_predicate_accepts_slash_form(self):
        self.assertTrue(worktrees.is_exclude_line("CLAUDE.local.md"))
        self.assertTrue(worktrees.is_exclude_line("  /CLAUDE.local.md \n"))
        self.assertFalse(worktrees.is_exclude_line("!CLAUDE.local.md"))
        self.write(self.exclude, "/CLAUDE.local.md\n")
        self.assertEqual(worktrees.ensure_exclude_line(self.main), "present")
        self.assertEqual(self.read(self.exclude), "/CLAUDE.local.md\n")
        self.assertEqual(self.apply_repo()["result"], "written")
        self.assertEqual(self.read(self.exclude), "/CLAUDE.local.md\n")

    def test_exclude_line_written_under_apply_lock(self):
        lock = os.path.join(self.home, "state", "apply.lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        held, release = threading.Event(), threading.Event()

        def holder():
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            held.set()
            release.wait(5)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            held.wait(5)
            with mock.patch.object(apply, "LOCK_DEADLINE", 0.3):
                out = self.apply_repo()
        finally:
            release.set()
            thread.join()
        self.assertEqual(out["result"], "skipped:lock-timeout")
        self.assertFalse(os.path.exists(self.exclude))
        self.assertFalse(os.path.lexists(self.local))
