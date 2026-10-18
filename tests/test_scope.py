import os
import stat
import tempfile
import unittest
from unittest import mock

from apprentice import config, scope
from tests.base import REPO_ROOT, ApprenticeTestCase
from tests.fixtures import gitrepo, skills, worktrees

_FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")
_FAKE_GH = os.path.join(_FIXTURES, "fake-gh")
_FAKE_GIT = os.path.join(_FIXTURES, "fake-git")

NEWER = "2026-03-05T10:00:00.000Z"
OLDER = "2026-03-02T10:00:00.000Z"


def _row(repo=None, hint=None):
    return {"repo": repo, "skill_hint": hint}


def _ask(rows, skill=None, questions=None, free_texts=None):
    return {"skill": skill, "rows": rows, "display_questions": questions or [], "free_texts": free_texts or []}


def _pair(event_id, text, ts=NEWER, repo=None, visible=True):
    event = {"id": event_id, "text": text} if visible else {"id": event_id}
    return {"event": event, "visible": visible, "ts": ts, "repo": repo}


def _group(pairs):
    return {"pairs": pairs}


class ScopeTestCase(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        scope._ARTIFACTS.clear()
        self.cfg = config.load_config()
        self.trees = worktrees.build(self.home)
        # the worktree fixtures are not real repos: a git that reports "not tracked" keeps them resolvable
        self.real_path = os.environ.get("PATH", "")
        patcher = mock.patch.dict(os.environ, self.path_with(self.fake_git_dir("#!/bin/sh\nexit 1\n")))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        scope._ARTIFACTS.clear()
        super().tearDown()

    def path_with(self, bindir):
        return {"PATH": bindir + os.pathsep + os.environ.get("PATH", "")}

    def fake_git_dir(self, script_text=None):
        bindir = os.path.realpath(tempfile.mkdtemp(dir=self.home))
        if script_text is None:
            os.symlink(_FAKE_GIT, os.path.join(bindir, "git"))
        else:
            path = self.write(os.path.join(bindir, "git"), script_text)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return bindir

    def resolver(self, labels=None, cache=None):
        return scope.TargetResolver(self.cfg, self.fake_home, labels or {}, {} if cache is None else cache)


class InferScopeTest(ScopeTestCase):
    def infer(self, kind, found, names=None, mains=None):
        return scope.infer_scope(kind, found, self.cfg, names or [], mains or [])

    def test_scope_skill_from_hint(self):
        found = _ask([_row(hint="oneshot-pipeline"), _row()], skill="oneshot-pipeline")
        self.assertEqual(self.infer("ask", found), ("skill", "oneshot-pipeline"))

    def test_scope_skill_from_phase_word(self):
        found = _group([_pair("e1", "the shepherd should stop after one push")])
        self.assertEqual(self.infer("correction", found), ("skill", None))
        named = _group([_pair("e1", "update /plain-skill so it asks first")])
        self.assertEqual(self.infer("correction", named, names=["plain-skill"]), ("skill", "plain-skill"))
        self.assertEqual(self.infer("correction", named), ("global", None))

    def test_skill_name_needs_slash_skill_word_or_backticks(self):
        for text in ("run /review again", "the review skill is too noisy", "the `review` step is noisy"):
            found = _group([_pair("e1", text)])
            self.assertEqual(self.infer("correction", found, names=["review"]), ("skill", "review"), text)
        plain = _group([_pair("e1", "do a quick review of the diff before pushing")])
        self.assertEqual(self.infer("correction", plain, names=["review"]), ("global", None))

    def test_generic_directory_words_do_not_force_repo_scope(self):
        main = self.trees["main"]
        for name in ("tests", "docs", "scripts", "src", "config"):
            os.makedirs(os.path.join(main, name), exist_ok=True)
        for text in ("always write tests first", "update the docs when behaviour changes", "keep config small"):
            found = _group([_pair("e1", text, repo="acme/widgets"), _pair("e2", "x", ts=OLDER, repo="acme/gadgets")])
            self.assertEqual(self.infer("correction", found, mains=[main]), ("global", None), text)
        for text in ("keep helpers under `scripts` small", "put new cases in tests/", "edit src/app.py less"):
            self.write(os.path.join(main, "src", "app.py"), "x\n")
            found = _group([_pair("e1", text, repo="acme/widgets"), _pair("e2", "x", ts=OLDER, repo="acme/gadgets")])
            self.assertEqual(self.infer("correction", found, mains=[main]), ("repo", None), text)

    def test_scope_repo_single_repo_evidence(self):
        found = _ask([_row(repo="acme/widgets"), _row(repo="acme/widgets")])
        self.assertEqual(self.infer("ask", found), ("repo", None))
        mixed = _ask([_row(repo="acme/widgets"), _row(repo=None)])
        self.assertEqual(self.infer("ask", mixed), ("global", None))

    def test_scope_global_default(self):
        found = _ask([_row(repo="acme/widgets"), _row(repo="acme/gadgets")], questions=["Which model?"])
        self.assertEqual(self.infer("ask", found, mains=[self.trees["main"]]), ("global", None))

    def test_scope_repo_from_named_package(self):
        self.write(os.path.join(self.trees["main"], "package.json"), '{"name": "widgets-api"}\n')
        found = _group([_pair("e1", "the widgets-api package should keep tests next to code",
                              repo="acme/widgets"),
                        _pair("e2", "unrelated", ts=OLDER, repo="acme/gadgets")])
        self.assertEqual(self.infer("correction", found, mains=[self.trees["main"]]), ("repo", None))
        hidden = _group([_pair("e1", "x", repo="acme/widgets", visible=False),
                         _pair("e2", "y", ts=OLDER, repo="acme/gadgets", visible=False)])
        self.assertEqual(self.infer("correction", hidden, mains=[self.trees["main"]]), ("global", None))

    def test_scope_repo_from_top_level_dir(self):
        main = self.trees["main"]
        os.makedirs(os.path.join(main, "services"))
        rows = [_row(repo="acme/widgets"), _row(repo="acme/gadgets")]
        named = _ask(rows, questions=["Should `services` own the retry policy?"])
        self.assertEqual(self.infer("ask", named, mains=[main]), ("repo", None))
        bare = _ask(rows, questions=["Should services own the retry policy?"])
        self.assertEqual(self.infer("ask", bare, mains=[main]), ("global", None))
        missing = _ask(rows, questions=["Is src/nope.py the right place?"])
        self.assertEqual(self.infer("ask", missing, mains=[main]), ("global", None))
        escape = _ask(rows, questions=["Read ../etc please"])
        self.assertEqual(self.infer("ask", escape, mains=[main]), ("global", None))
        spoken = _ask(rows, free_texts=["put it under services/"])
        self.assertEqual(self.infer("ask", spoken, mains=[main]), ("repo", None))

    def test_repo_artifacts_pyproject_regex_and_scoped_name(self):
        main = self.trees["main"]
        self.write(os.path.join(main, "pyproject.toml"),
                   '[build-system]\nname = "not-this"\n\n[project]\nversion = "1"\nname = \'Widgets-Core\'\n'
                   '\n[tool.x]\nname = "nor-this"\n')
        self.write(os.path.join(main, "package.json"), '{"name": "@acme/widgets"}\n')
        os.makedirs(os.path.join(main, "node_modules"))
        os.makedirs(os.path.join(main, "services"))
        found = scope.repo_artifacts(main)
        self.assertIn("widgets-core", found)
        self.assertIn("@acme/widgets", found)
        self.assertIn("widgets", found)
        self.assertIn("services", found)
        self.assertNotIn("not-this", found)
        self.assertNotIn("nor-this", found)
        self.assertNotIn("node_modules", found)
        self.assertNotIn(".git", found)
        self.assertEqual(scope.repo_artifacts(os.path.join(self.home, "absent")), set())

    def test_path_exists_in_rejects_escapes(self):
        main = self.trees["main"]
        self.write(os.path.join(main, "src", "a.py"), "x\n")
        self.assertTrue(scope.path_exists_in(main, "src/a.py"))
        self.assertFalse(scope.path_exists_in(main, "src/nope.py"))
        self.assertFalse(scope.path_exists_in(main, "a.py"))
        self.assertFalse(scope.path_exists_in(main, "../main/src/a.py"))
        self.assertFalse(scope.path_exists_in(main, os.path.join(main, "src", "a.py")))


class RepoTargetTest(ScopeTestCase):
    def test_repo_target_deleted_worktree_then_main_resolves(self):
        found = self.resolver().repo_target([self.trees["gone"], self.trees["wt1"]], "acme/widgets")
        self.assertEqual(found, {"target": os.path.join(self.trees["main"], "CLAUDE.local.md"),
                                 "target_status": "resolved"})

    def test_repo_target_no_checkout(self):
        found = self.resolver().repo_target([self.trees["wt1"]], "acme/other")
        self.assertEqual(found, {"target": None, "target_status": "no-checkout"})
        self.assertEqual(self.resolver().repo_target([], "acme/widgets")["target_status"], "no-checkout")

    def test_repo_target_git_tracked_local_recommend_rescope(self):
        with mock.patch.dict(os.environ, {"PATH": self.real_path}):
            repo = gitrepo.init_committed_local(self.home)
            if repo is None:
                self.skipTest("git not installed")
            config_path = os.path.join(repo, ".git", "config")
            self.write(config_path, '[remote "origin"]\n\turl = https://example.test/acme/widgets.git\n')
            found = self.resolver().repo_target([repo], "acme/widgets")
        self.assertEqual(found["target_status"], "git-tracked-local")
        self.assertIsNone(found["target"])
        self.assertEqual(scope.TargetResolver.recommend(found["target_status"], None), "rescope-global")

    def test_repo_target_unknown_tracking_with_fake_git(self):
        env = dict(self.path_with(self.fake_git_dir()), FAKE_GIT_MODE="exit128")
        with mock.patch.dict(os.environ, env):
            found = self.resolver().repo_target([self.trees["main"]], "acme/widgets")
        self.assertEqual(found["target_status"], "unknown-tracking")
        self.assertIsNone(found["target"])
        self.assertEqual(scope.TargetResolver.recommend("unknown-tracking", None), "rescope-global")
        self.assertEqual(scope.TargetResolver.recommend("resolved", None), "accept")

    def test_is_tracked_called_once_per_main(self):
        counter = os.path.join(self.home, "git-calls")
        script = '#!/bin/sh\necho x >> "%s"\nexit 1\n' % counter
        resolver = self.resolver()
        with mock.patch.dict(os.environ, self.path_with(self.fake_git_dir(script))):
            first = resolver.repo_target([self.trees["wt1"]], "acme/widgets")
            second = resolver.repo_target([self.trees["wt2"]], "acme/widgets")
            resolver.alt_targets("repo", None, {"acme/widgets": [self.trees["wt1"]]})
        self.assertEqual(first["target_status"], "resolved")
        self.assertEqual(second["target_status"], "resolved")
        with open(counter, "r", encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().splitlines()), 1)


class SkillTargetTest(ScopeTestCase):
    def setUp(self):
        super().setUp()
        self.tree = skills.build(self.home, self.fake_home)

    def skill_target(self, name, mode="public", cfg=None):
        if cfg is not None:
            self.cfg = cfg
        with mock.patch.dict(os.environ, {"APPRENTICE_GH_BIN": _FAKE_GH, "FAKE_GH_MODE": mode}):
            return self.resolver().skill_target(name)

    def test_skill_target_tracked_public_private_unknown_recommend(self):
        expected = {"public": "rescope-global", "private": "accept", "missing-json": "rescope-global"}
        for mode, advice in sorted(expected.items()):
            found = self.skill_target("tracked-skill", mode)
            self.assertEqual(found["name"], "tracked-skill")
            self.assertEqual(found["target"], os.path.join(self.tree["tracked"], "SKILL.md"))
            self.assertEqual(found["target_status"], "resolved")
            git = found["target_git"]
            self.assertEqual(sorted(git), ["public_mirror", "remote", "root", "tracked", "visibility"])
            self.assertIs(git["tracked"], True)
            self.assertEqual(git["remote"], "acme/skills")
            self.assertEqual(scope.TargetResolver.recommend(found["target_status"], git), advice, mode)
        plain = self.skill_target("plain-skill")
        self.assertEqual(sorted(plain["target_git"]), ["public_mirror", "remote", "root", "tracked"])
        self.assertIs(plain["target_git"]["tracked"], False)
        self.assertEqual(scope.TargetResolver.recommend(plain["target_status"], plain["target_git"]), "accept")
        self.assertIsNone(self.skill_target("absent-skill"))
        self.assertIsNone(self.skill_target(None))

    def test_skill_target_synced_and_cache_rescoped_readonly(self):
        os.symlink(self.tree["cache"], os.path.join(self.tree["claude"], "skills", "cache-skill"))
        global_path = os.path.realpath(os.path.join(self.fake_home, ".claude", "CLAUDE.md"))
        for name in ("synced-skill", "cache-skill"):
            found = self.skill_target(name)
            self.assertEqual(found["target"], global_path)
            self.assertEqual(found["target_status"], "rescoped-readonly")
            self.assertIsNone(found["target_git"])

    def test_skill_target_public_skills_mirror(self):
        cfg = dict(self.cfg, public_skills=["mirrored-skill"])
        found = self.skill_target("mirrored-skill", "private", cfg=cfg)
        self.assertIs(found["target_git"]["public_mirror"], True)
        self.assertEqual(found["target_git"]["visibility"], "public")
        self.assertEqual(scope.TargetResolver.recommend(found["target_status"], found["target_git"]),
                         "rescope-global")


class GlobalTargetTest(ScopeTestCase):
    def profile(self, name, link_to=None):
        path = os.path.join(self.fake_home, name)
        self.write(os.path.join(path, "settings.json"), "{}\n")
        os.makedirs(os.path.join(path, "projects"), exist_ok=True)
        if link_to is not None:
            os.symlink(link_to, os.path.join(path, "CLAUDE.md"))
        return path

    def setUp(self):
        super().setUp()
        self.target = self.write(os.path.join(self.fake_home, ".claude", "CLAUDE.md"), "rules\n")
        self.profile(".claude-aaa", self.target)
        self.profile(".claude-bbb", self.target)
        self.profile(".claude-ccc")
        self.write(os.path.join(self.fake_home, ".claude-ccc", "CLAUDE.md"), "own rules\n")

    def test_global_target_shared_groups_opaque_labels(self):
        labels = {"default": "group-1", ".claude-aaa": "group-2", ".claude-bbb": "group-3",
                  ".claude-ccc": "group-4"}
        found = self.resolver(labels).global_target()
        self.assertEqual(found["target"], os.path.realpath(self.target))
        self.assertEqual(found["target_shared_groups"], ["group-2", "group-3"])
        self.assertNotIn(".claude-aaa", repr(found))

    def test_profile_dirs_listed_once_per_resolver(self):
        resolver = self.resolver()
        with mock.patch.object(scope.status, "profile_dirs", wraps=scope.status.profile_dirs) as listed:
            resolver.global_target()
            resolver.global_target()
            resolver.alt_targets("global", None, {})
        self.assertEqual(listed.call_count, 1)

    def test_global_target_reads_the_profile_from_the_env_it_is_given(self):
        labels = {"default": "group-1", ".claude-aaa": "group-2", ".claude-bbb": "group-3"}
        env = {"CLAUDE_CONFIG_DIR": os.path.join(self.fake_home, ".claude-aaa")}
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(self.fake_home, ".claude-bbb")}):
            found = scope.TargetResolver(self.cfg, self.fake_home, labels, {}, env).global_target()
        self.assertEqual(found["target_shared_groups"], ["group-1", "group-3"])

    def test_global_target_shared_groups_configured_names(self):
        self.cfg = dict(self.cfg, account_groups={"work": [".claude-aaa"], "home": [".claude-bbb", "default"]})
        found = self.resolver({".claude-aaa": "group-2"}).global_target()
        self.assertEqual(found["target_shared_groups"], ["work"])
        self.assertNotIn(".claude-bbb", repr(found))


class AltTargetsTest(ScopeTestCase):
    def test_alt_targets_two_repos_one_no_checkout(self):
        found = self.resolver().alt_targets(
            "repo", None,
            {"acme/widgets": [self.trees["wt1"]], "acme/other": [self.trees["wt2"]]})
        self.assertEqual(sorted(found), ["global", "repo", "skill"])
        self.assertIn("target", found["global"])
        self.assertEqual(sorted(found["repo"]), ["acme/other", "acme/widgets"])
        self.assertEqual(found["repo"]["acme/widgets"]["target_status"], "resolved")
        self.assertEqual(found["repo"]["acme/other"]["target_status"], "no-checkout")
        self.assertEqual(sorted(found["repo"]["acme/other"]), ["target", "target_status"])

    def test_alt_targets_skill_none(self):
        found = self.resolver().alt_targets("global", None, {})
        self.assertIsNone(found["skill"])
        self.assertEqual(found["repo"], {})
        self.assertIsNone(self.resolver().alt_targets("skill", "absent-skill", {})["skill"])


if __name__ == "__main__":
    unittest.main()
