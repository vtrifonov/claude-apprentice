import os
import tempfile
import unittest
from unittest import mock

from apprentice import apply, gitinfo, scope
from tests.base import REPO_ROOT, ApprenticeTestCase
from tests.fixtures import gitrepo, skills

_FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")
_FAKE_GH = os.path.join(_FIXTURES, "fake-gh")
_FAKE_GIT = os.path.join(_FIXTURES, "fake-git")


class IsTrackedTest(ApprenticeTestCase):
    def fake_git_on_path(self):
        bindir = os.path.realpath(tempfile.mkdtemp(dir=self.home))
        os.symlink(_FAKE_GIT, os.path.join(bindir, "git"))
        return {"PATH": bindir + os.pathsep + os.environ.get("PATH", "")}

    def test_is_tracked_true_on_committed(self):
        repo = gitrepo.init_committed_local(self.home)
        if repo is None:
            self.skipTest("git not installed")
        self.assertIs(gitinfo.is_tracked(repo, "CLAUDE.local.md"), True)

    def test_is_tracked_false_on_untracked_excluded(self):
        repo = gitrepo.init_committed_local(self.home)
        if repo is None:
            self.skipTest("git not installed")
        self.write(os.path.join(repo, "scratch.md"), "x\n")
        self.write(os.path.join(repo, ".git", "info", "exclude"), "scratch.md\n")
        self.assertIs(gitinfo.is_tracked(repo, "scratch.md"), False)

    def test_is_tracked_none_with_slow_fake_git(self):
        env = dict(self.fake_git_on_path(), FAKE_GIT_MODE="sleep")
        with mock.patch.dict(os.environ, env), mock.patch.object(gitinfo, "IS_TRACKED_TIMEOUT", 0.5):
            self.assertIsNone(gitinfo.is_tracked(self.home, "CLAUDE.local.md"))

    def test_is_tracked_none_on_exit128(self):
        env = dict(self.fake_git_on_path(), FAKE_GIT_MODE="exit128")
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(gitinfo.is_tracked(self.home, "CLAUDE.local.md"))


class InspectTargetTest(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.tree = skills.build(self.home, self.fake_home)
        self.cfg = {"public_skills": []}

    def inspect(self, path, mode=None, gh=_FAKE_GH, cfg=None):
        env = {"APPRENTICE_GH_BIN": gh}
        if mode is not None:
            env["FAKE_GH_MODE"] = mode
        with mock.patch.dict(os.environ, env):
            return apply.inspect_target(path, cfg or self.cfg, timeout=1.0)

    def test_inspect_tracked_skill_through_symlink_public_private_unknown(self):
        path = os.path.join(self.tree["tracked"], "SKILL.md")
        for mode, expected in (("public", "public"), ("private", "private"), ("missing-json", "unknown")):
            result = self.inspect(path, mode)
            self.assertIs(result["tracked"], True)
            self.assertEqual(result["root"], self.tree["repo"])
            self.assertEqual(result["remote"], "acme/skills")
            self.assertEqual(result["visibility"], expected)
            self.assertIs(result["public_mirror"], False)
        self.assertEqual(self.inspect(path, "sleep")["visibility"], "unknown")
        missing = os.path.join(self.home, "no-such-gh")
        self.assertEqual(self.inspect(path, "public", gh=missing)["visibility"], "unknown")

    def test_inspect_plain_skill_untracked(self):
        result = self.inspect(os.path.join(self.tree["plain"], "SKILL.md"), "public")
        self.assertEqual(result, {"tracked": False, "root": None, "remote": None, "visibility": None,
                                  "public_mirror": False, "excluded": None})

    def test_inspect_public_skills_mirror(self):
        cfg = {"public_skills": ["mirrored-skill"]}
        result = self.inspect(os.path.join(self.tree["mirrored"], "SKILL.md"), "private", cfg=cfg)
        self.assertIs(result["tracked"], True)
        self.assertEqual(result["visibility"], "public")
        self.assertIs(result["public_mirror"], True)
        other = self.inspect(os.path.join(self.tree["mirrored"], "OTHER.md"), "private", cfg=cfg)
        self.assertIs(other["public_mirror"], False)

    def test_inspect_claude_local_excluded_flag(self):
        main = os.path.join(self.home, "proj")
        self.write(os.path.join(main, ".git", "config"), "[core]\n")
        local = self.write(os.path.join(main, "CLAUDE.local.md"), "x\n")
        self.assertIs(self.inspect(local)["excluded"], False)
        self.write(os.path.join(main, ".git", "info", "exclude"), "# local\nCLAUDE.local.md\n")
        self.assertIs(self.inspect(local)["excluded"], True)
        other = self.write(os.path.join(main, "notes.md"), "x\n")
        self.assertIsNone(self.inspect(other)["excluded"])


class SkillScopeTest(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.tree = skills.build(self.home, self.fake_home)

    def test_find_skill_dir_order_and_synced(self):
        home = self.fake_home
        self.assertEqual(scope.find_skill_dir("plain-skill", home), self.tree["plain"])
        self.assertEqual(scope.find_skill_dir("synced-skill", home), self.tree["synced"])
        self.assertIsNone(scope.find_skill_dir("absent-skill", home))
        local_root = os.path.join(home, ".claude", "local-skills")
        self.write(os.path.join(local_root, "plain-skill", "SKILL.md"), "x\n")
        self.assertEqual(scope.find_skill_dir("plain-skill", home), self.tree["plain"])
        self.assertIsNone(scope.find_skill_dir("only-local", home))
        self.write(os.path.join(local_root, "only-local", "SKILL.md"), "x\n")
        self.assertEqual(scope.find_skill_dir("only-local", home), os.path.join(local_root, "only-local"))
        os.makedirs(os.path.join(home, ".claude", "skills", "no-file"))
        self.assertIsNone(scope.find_skill_dir("no-file", home))

    def test_is_readonly_skill_cache_and_synced(self):
        self.assertIs(scope.is_readonly_skill(self.tree["cache"]), True)
        self.assertIs(scope.is_readonly_skill(self.tree["synced"]), True)
        self.assertIs(scope.is_readonly_skill(self.tree["plain"]), False)
        self.assertIs(scope.is_readonly_skill(self.tree["tracked"]), False)


if __name__ == "__main__":
    unittest.main()
