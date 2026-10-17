import os
import unittest

from apprentice import gitinfo
from tests.base import ApprenticeTestCase
from tests.fixtures import worktrees


class GitInfoTest(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.tree = worktrees.build(self.home)

    def test_linked_worktree_main_and_exclude(self):
        t = self.tree
        self.assertEqual(gitinfo.main_checkout(t["wt1"]), t["main"])
        self.assertEqual(gitinfo.exclude_path(t["wt1"]), os.path.join(t["main"], ".git", "info", "exclude"))
        self.assertEqual(gitinfo.worktrees(t["main"]), sorted([t["wt1"], t["wt2"]]))

    def test_bare_worktree_no_main(self):
        t = self.tree
        self.assertIsNone(gitinfo.main_checkout(t["barewt"]))
        self.assertEqual(gitinfo.exclude_path(t["barewt"]), os.path.join(t["bare"], "info", "exclude"))

    def test_submodule_main_is_sub(self):
        t = self.tree
        self.assertEqual(gitinfo.main_checkout(t["sub"]), t["sub"])

    def test_separate_git_dir_main_is_self(self):
        t = self.tree
        self.assertEqual(gitinfo.main_checkout(t["sep"]), t["sep"])

    def test_origin_https_and_ssh(self):
        t = self.tree
        self.assertEqual(gitinfo.origin(t["main"]), "acme/widgets")
        self.assertEqual(gitinfo.origin(t["wt1"]), "acme/widgets")
        self.assertEqual(gitinfo.origin(t["sep"]), "acme/widgets")
        url = "ssh://" + "git" + chr(64) + "host" + ".test/acme/widgets.git"
        self.assertEqual(gitinfo.parse_origin_url(url), "acme/widgets")

    def test_branch_and_detached(self):
        t = self.tree
        self.assertEqual(gitinfo.branch(t["main"]), "main")
        self.assertEqual(gitinfo.branch(t["wt1"]), "feat/XX-123-x")
        self.write(os.path.join(t["main"], ".git", "HEAD"), "0123456789abcdef0123456789abcdef01234567\n")
        self.assertIsNone(gitinfo.branch(t["main"]))

    def test_find_git_depth_limit(self):
        root = os.path.join(self.home, "deep")
        os.makedirs(os.path.join(root, ".git"))
        near = os.path.join(root, *["d"] * 12)
        far = os.path.join(root, *["d"] * 13)
        os.makedirs(far)
        self.assertEqual(gitinfo.find_git(near), root)
        self.assertIsNone(gitinfo.find_git(far))

    def test_resolve_main_for_deleted_worktree(self):
        t = self.tree
        gone_src = os.path.join(t["gone"], "src")
        self.assertEqual(gitinfo.resolve_main_for_cwd(gone_src, "-worktrees"), t["main"])

    def test_resolve_main_nearest_ancestor(self):
        t = self.tree
        missing = os.path.join(t["main"], "does", "not", "exist")
        self.assertEqual(gitinfo.resolve_main_for_cwd(missing, "-worktrees"), t["main"])

    def test_not_a_repo_context_all_none(self):
        plain = os.path.join(self.home, "plain")
        os.makedirs(plain)
        self.assertEqual(
            gitinfo.repo_context(plain), {"root": None, "main": None, "repo": None, "branch": None}
        )


if __name__ == "__main__":
    unittest.main()
