import os

from apprentice import worktrees
from tests.base import ApprenticeTestCase
from tests.fixtures import worktrees as tree_fixture

_NAME = "CLAUDE.local.md"


class WorktreesTest(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.tree = tree_fixture.build(self.home)
        self.main_file = os.path.join(self.tree["main"], _NAME)
        self.exclude = os.path.join(self.tree["main"], ".git", "info", "exclude")

    def seed(self, text="# alice rules\n", excluded=True):
        self.write(self.main_file, text)
        if excluded:
            self.write(self.exclude, "# comment\n" + _NAME + "\n")

    def test_created_when_excluded_and_source_exists(self):
        self.seed()
        status, content = worktrees.ensure_link(self.tree["wt1"])
        self.assertEqual((status, content), ("created", "# alice rules\n"))
        link = os.path.join(self.tree["wt1"], _NAME)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), self.main_file)
        self.assertTrue(os.path.isabs(os.readlink(link)))

    def test_second_run_present_no_content(self):
        self.seed()
        worktrees.ensure_link(self.tree["wt1"])
        self.assertEqual(worktrees.ensure_link(self.tree["wt1"]), ("present", None))

    def test_not_excluded_never_links(self):
        self.seed(excluded=False)
        self.assertEqual(worktrees.ensure_link(self.tree["wt1"]), ("skipped-not-excluded", None))
        self.assertFalse(os.path.lexists(os.path.join(self.tree["wt1"], _NAME)))
        self.assertFalse(os.path.exists(self.exclude))

    def test_regular_file_in_way(self):
        self.seed()
        self.write(os.path.join(self.tree["wt1"], _NAME), "mine\n")
        self.assertEqual(worktrees.ensure_link(self.tree["wt1"]), ("skipped-file-exists", None))
        self.assertFalse(os.path.islink(os.path.join(self.tree["wt1"], _NAME)))

    def test_no_source(self):
        self.write(self.exclude, _NAME + "\n")
        self.assertEqual(worktrees.ensure_link(self.tree["wt1"]), ("no-source", None))

    def test_main_checkout_cwd_not_a_repo_status(self):
        self.seed()
        self.assertEqual(worktrees.ensure_link(self.tree["main"]), ("not-a-repo", None))
        self.assertEqual(worktrees.ensure_link(os.path.join(self.home, "nowhere-at-all")), ("not-a-repo", None))
        self.assertFalse(worktrees.is_worktree(self.tree["main"]))
        self.assertTrue(worktrees.is_worktree(self.tree["wt1"]))

    def test_bare_worktree_no_main_checkout(self):
        self.assertEqual(worktrees.ensure_link(self.tree["barewt"]), ("no-main-checkout", None))

    def test_content_cap_8192(self):
        self.seed(text="x" * 10000)
        status, content = worktrees.ensure_link(self.tree["wt1"])
        self.assertEqual(status, "created")
        self.assertEqual(len(content), 8192)
