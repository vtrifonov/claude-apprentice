import json
import os
import shlex
import subprocess

from apprentice import apply, config, ledger
from tests.base import REPO_ROOT, ApprenticeTestCase
from tests.fixtures import gitrepo, skills, worktrees

_SCRIPT = os.path.join(REPO_ROOT, "scripts", "apply.py")
_FAKE_GH = os.path.join(REPO_ROOT, "tests", "fixtures", "fake-gh")
A, B, T, Z = "r_0000000a", "r_0000000b", "r_0000000c", "r_0000000f"
TEXT = "Run the linter before pushing."


class ApplyCliCase(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.claude_md = self.write(os.path.join(self.fake_home, ".claude", "CLAUDE.md"), "# Mine\n")
        self.alt = os.path.join(self.fake_home, ".claude-alt")
        os.makedirs(self.alt)
        os.symlink(self.claude_md, os.path.join(self.alt, "CLAUDE.md"))
        self.pack = os.path.join(self.home, "review", "2026-10-18-1.json")
        os.makedirs(os.path.dirname(self.pack))

    def run_script(self, *args):
        env = dict(os.environ, APPRENTICE_HOME=self.home, HOME=self.fake_home, APPRENTICE_GH_BIN=_FAKE_GH,
                   FAKE_GH_MODE="private")
        env.pop("CLAUDE_CONFIG_DIR", None)
        interp = '"${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S %s' % shlex.quote(_SCRIPT)
        cmd = interp + " " + " ".join(shlex.quote(a) for a in args)
        return subprocess.run(["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    def text_file(self, text=TEXT, name="rule.txt"):
        return self.write(os.path.join(self.home, "state", "tmp", name), text)

    def write_pack(self, candidates, private=None):
        self.write(self.pack, json.dumps({"inputs": {"group": "g1"}, "candidates": candidates}))
        if private is not None:
            ledger.write_private(self.home, self.pack, private)

    def out(self, result):
        return result.stdout.decode()

    def read(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def applied_global(self, rule_id=A, *extra):
        return self.run_script("--id", rule_id, "--text-file", self.text_file(), "--scope", "global", *extra)

    def proposals(self, rule_id):
        return [e for e in ledger.read_proposals(self.home) if e["id"] == rule_id]


class GlobalApplyTest(ApplyCliCase):
    def test_global_apply_writes_and_ledgers_accepted(self):
        self.write_pack([{"id": A, "kind": "correction", "topic": "lint", "evidence_ids": ["e1", "e2"],
                          "scope": "global", "target": "claude_md", "band": "high", "band_inputs": {"n": 5}}],
                        {A: {"tokens": [["alpha", "beta"]], "cluster_key": "ck", "kind": "correction",
                               "topic": "lint", "evidence_ids": ["e1", "e2"], "n": 5}})
        result = self.applied_global(A, "--pack", self.pack)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.out(result).strip(), "result=written file=%s" % self.claude_md)
        self.assertIn(TEXT, self.read(self.claude_md))
        entry = self.proposals(A)[0]
        self.assertEqual(entry["status"], "accepted")
        self.assertEqual(entry["kind"], "correction")
        self.assertEqual(entry["cluster_key"], "ck")
        self.assertEqual(entry["tokens"], [["alpha", "beta"]])
        self.assertEqual(entry["evidence_ids"], ["e1", "e2"])
        self.assertEqual(entry["n_at_decision"], 5)
        self.assertEqual(entry["group"], "g1")
        self.assertEqual(entry["profile"], "default")
        self.assertEqual(entry["text"], TEXT)
        self.assertTrue(entry["decided_at"])
        row = ledger.read_applied(self.home)[0]
        self.assertEqual((row["action"], row["id"], row["result"], row["tracked"]), ("apply", A, "written", False))
        self.assertIn(" n=5 high ", row["line"])

    def test_global_apply_without_pack_minimal_accepted_entry(self):
        result = self.applied_global(T)
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.proposals(T)[0]
        self.assertEqual(entry["status"], "accepted")
        self.assertEqual(entry["text"], TEXT)
        self.assertEqual(entry["scope"], "global")
        self.assertIsNone(entry["kind"])
        self.assertIsNone(entry["tokens"])
        self.assertIsNone(entry["pack"])

    def test_global_apply_merged_ids_stores_aliases_and_seed_sets(self):
        self.write_pack([{"id": A, "kind": "correction", "topic": "a", "evidence_ids": ["e1"]},
                         {"id": B, "kind": "correction", "topic": "b", "evidence_ids": ["e2", "e1"]}],
                        {A: {"tokens": [["a1", "a2"]], "cluster_key": None, "n": 2},
                         B: {"tokens": [["b1"]], "cluster_key": None, "n": 3}})
        result = self.applied_global(A, "--pack", self.pack, "--merged-ids", B)
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.proposals(A)[0]
        self.assertEqual(entry["merged_ids"], [B])
        self.assertEqual(entry["tokens"], [["a1", "a2"], ["b1"]])
        self.assertEqual(sorted(entry["evidence_ids"]), ["e1", "e2"])
        self.assertEqual(ledger.aliases(ledger.read_proposals(self.home)), {B: A})
        unknown = self.applied_global(A, "--pack", self.pack, "--merged-ids", "X")
        self.assertEqual(unknown.returncode, 2)
        no_pack = self.applied_global(A, "--merged-ids", B)
        self.assertEqual(no_pack.returncode, 2)

    def test_text_file_outside_tmp_rejected(self):
        outside = self.write(os.path.join(self.home, "elsewhere.txt"), TEXT)
        result = self.run_script("--id", A, "--text-file", outside, "--scope", "global")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.read(self.claude_md), "# Mine\n")

    def test_dry_run_prints_diff_no_write(self):
        result = self.applied_global(A, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("result=written", self.out(result))
        self.assertIn("+- " + TEXT, self.out(result))
        self.assertEqual(self.read(self.claude_md), "# Mine\n")
        self.assertEqual(ledger.read_applied(self.home), [])
        self.assertEqual(ledger.read_proposals(self.home), [])

    def test_exit_codes(self):
        self.assertEqual(self.applied_global(A).returncode, 0)
        again = self.applied_global(A)
        self.assertEqual(again.returncode, 0)
        self.assertIn("result=unchanged", self.out(again))
        undone = self.run_script("--undo", A)
        self.assertEqual(undone.returncode, 0)
        self.assertIn("result=removed", self.out(undone))
        skipped = self.run_script("--id", A, "--text-file", self.text_file(), "--scope", "skill")
        self.assertEqual(skipped.returncode, 1)
        self.assertIn("result=skipped:no-target", self.out(skipped))
        self.assertEqual(self.run_script("--id", A).returncode, 2)
        self.assertEqual(self.run_script("--scope", "nowhere").returncode, 2)

    def test_global_in_work_tree_needs_confirm_and_applies(self):
        self.write(os.path.join(self.fake_home, ".claude", ".git", "HEAD"), "ref: refs/heads/main\n")
        self.write_pack([{"id": A, "kind": "correction", "topic": "t", "evidence_ids": [], "repos": []}], {})
        refused = self.applied_global(A, "--pack", self.pack)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("result=skipped:git-tracked", self.out(refused))
        self.assertEqual(self.read(self.claude_md), "# Mine\n")
        written = self.applied_global(A, "--pack", self.pack, "--confirm-tracked")
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertIn(TEXT, self.read(self.claude_md))
        self.assertIs(ledger.read_applied(self.home)[-1]["tracked"], True)

    def test_tracked_dry_run_shows_diff_without_confirm_and_writes_nothing(self):
        self.write(os.path.join(self.fake_home, ".claude", ".git", "HEAD"), "ref: refs/heads/main\n")
        self.write_pack([{"id": A, "kind": "correction", "topic": "t", "evidence_ids": [], "repos": []}], {})
        result = self.applied_global(A, "--pack", self.pack, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("result=written", self.out(result))
        self.assertIn("+- " + TEXT, self.out(result))
        self.assertEqual(self.read(self.claude_md), "# Mine\n")
        self.assertEqual(ledger.read_applied(self.home), [])
        self.assertEqual(ledger.read_proposals(self.home), [])

    def test_removed_flags(self):
        for flag in ("--include-unknown", "--all-profiles"):
            result = self.run_script(flag)
            self.assertEqual(result.returncode, 2)
            self.assertIn("removed flag", result.stderr.decode())



class RepoScopeCliTest(ApplyCliCase):
    def setUp(self):
        super().setUp()
        self.tree = worktrees.build(os.path.join(self.fake_home, "trees"))
        worktrees.make_git_ready(self.tree)
        self.main = self.tree["main"]
        self.local = os.path.join(self.main, "CLAUDE.local.md")
        self.exclude = os.path.join(self.main, ".git", "info", "exclude")
        alt = {"repo": {"acme/widgets": {"target": self.local, "status": "ok"}}}
        self.write_pack([{"id": A, "kind": "correction", "topic": "a", "evidence_ids": ["e1"], "repos": ["acme/widgets"],
                          "scope": "repo", "alt_targets": alt, "band": "high", "band_inputs": {"n": 4}},
                         {"id": B, "kind": "correction", "topic": "b", "evidence_ids": ["e2"]}],
                        {A: {"tokens": [["a1", "a2"]], "cluster_key": None, "n": 4},
                         B: {"tokens": [["b1"]], "cluster_key": None, "n": 3}})

    def repo_apply(self, *extra, target=None, pack=True):
        args = ["--id", A, "--text-file", self.text_file(), "--scope", "repo", "--target", target or self.local]
        if pack:
            args += ["--pack", self.pack]
        return self.run_script(*(args + list(extra)))

    def exclude_lines(self):
        return [line for line in self.read(self.exclude).splitlines() if line == "CLAUDE.local.md"]

    def test_repo_scope_via_cli_creates_excluded_local_and_links(self):
        result = self.repo_apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("result=written file=%s" % self.local, self.out(result))
        self.assertEqual(self.exclude_lines(), ["CLAUDE.local.md"])
        text = self.read(self.local)
        self.assertIn("<!-- apprentice:start -->", text)
        self.assertIn(TEXT, text)
        for name in ("wt1", "wt2"):
            self.assertEqual(os.readlink(os.path.join(self.tree[name], "CLAUDE.local.md")), self.local)
        entry = self.proposals(A)[0]
        self.assertEqual(entry["status"], "accepted")
        self.assertEqual(entry["scope"], "repo")
        self.assertEqual(entry["tokens"], [["a1", "a2"]])
        self.assertIn(" n=4 high ", ledger.read_applied(self.home)[0]["line"])

    def test_repo_scope_merged_ids_store_both_token_sets(self):
        result = self.repo_apply("--merged-ids", B)
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.proposals(A)[0]
        self.assertEqual(entry["merged_ids"], [B])
        self.assertEqual(entry["tokens"], [["a1", "a2"], ["b1"]])

    def test_repo_scope_without_pack_or_matching_target(self):
        no_pack = self.repo_apply(pack=False)
        self.assertEqual(no_pack.returncode, 1)
        self.assertIn("result=skipped:no-pack", self.out(no_pack))
        stranger = os.path.join(self.tree["sep"], "CLAUDE.local.md")
        alt = {"repo": {"acme/widgets": {"target": self.local, "status": "ok"}}}
        self.write_pack([{"id": A, "kind": "correction", "topic": "a", "evidence_ids": [],
                          "repos": ["acme/widgets", "acme/gadgets"], "alt_targets": alt}], {})
        other = self.repo_apply(target=stranger)
        self.assertEqual(other.returncode, 1)
        self.assertIn("result=skipped:bad-target", self.out(other))
        self.assertFalse(os.path.lexists(self.local))
        self.assertFalse(os.path.lexists(stranger))

    def test_single_repo_candidate_needs_no_alt_target_entry(self):
        self.write_pack([{"id": A, "kind": "correction", "topic": "a", "evidence_ids": [], "repos": ["acme/widgets"]}], {})
        result = self.repo_apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(TEXT, self.read(self.local))

    def test_repo_scope_dry_run_writes_nothing(self):
        result = self.repo_apply("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("+- " + TEXT, self.out(result))
        self.assertFalse(os.path.lexists(self.local))
        self.assertFalse(os.path.exists(self.exclude))

    def test_replay_repo_target_adds_exclude_then_line(self):
        self.assertEqual(self.repo_apply().returncode, 0)
        with_rule = self.read(self.local)
        self.write(self.local, "# notes\n")
        self.write(self.exclude, "*.log\n")
        replay = self.run_script("--replay")
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertIn("result=written", self.out(replay))
        self.assertEqual(self.exclude_lines(), ["CLAUDE.local.md"])
        self.assertIn(TEXT, self.read(self.local))
        self.assertNotEqual(self.read(self.local), with_rule)
        again = self.run_script("--replay")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("result=unchanged", self.out(again))
        self.assertEqual(self.exclude_lines(), ["CLAUDE.local.md"])

    def test_replay_dry_run_leaves_exclude_alone(self):
        self.assertEqual(self.repo_apply().returncode, 0)
        self.write(self.local, "# notes\n")
        self.write(self.exclude, "*.log\n")
        dry = self.run_script("--replay", "--dry-run")
        self.assertIn("+- " + TEXT, self.out(dry))
        self.assertEqual(self.read(self.exclude), "*.log\n")
        self.assertEqual(self.read(self.local), "# notes\n")

    def test_replay_committed_local_skipped_git_tracked_local(self):
        repo = gitrepo.init_committed_local(self.fake_home)
        if repo is None:
            self.skipTest("git not installed")
        with open(os.path.join(repo, ".git", "config"), "a", encoding="utf-8") as fh:
            fh.write('[remote "origin"]\n\turl = https://example.test/acme/widgets.git\n')
        target = os.path.join(repo, "CLAUDE.local.md")
        exclude = os.path.join(repo, ".git", "info", "exclude")
        ledger.record_decision(self.home, None, A, "accepted", text=TEXT, scope="repo", target=target)
        ledger.append_applied(self.home, {"action": "apply", "id": A, "file": target, "result": "written",
                                          "line": apply.render_line(A, TEXT, 4, "high", "2026-10-18")})
        with open(target, "rb") as fh, open(exclude, "rb") as fx:
            before = (fh.read(), fx.read())
        for extra in ((), ("--confirm-tracked",)):
            result = self.run_script("--replay", *extra)
            self.assertEqual(result.returncode, 1)
            self.assertIn("result=skipped:git-tracked-local", self.out(result))
            with open(target, "rb") as fh, open(exclude, "rb") as fx:
                self.assertEqual((fh.read(), fx.read()), before)


class SkillTargetTest(ApplyCliCase):
    def setUp(self):
        super().setUp()
        self.tree = skills.build(self.home, self.fake_home)

    def skill_apply(self, path, *extra):
        return self.run_script("--id", A, "--text-file", self.text_file(), "--scope", "skill", "--target", path,
                               *extra)

    def test_skill_in_cache_falls_back_to_global(self):
        target = os.path.join(self.tree["cache"], "SKILL.md")
        result = self.skill_apply(target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("file=%s" % self.claude_md, self.out(result))
        self.assertIn(TEXT, self.read(self.claude_md))
        self.assertEqual(self.read(target), "# cache\n")

    def test_synced_skill_falls_back_to_global(self):
        target = os.path.join(self.tree["synced"], "SKILL.md")
        result = self.skill_apply(target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("file=%s" % self.claude_md, self.out(result))
        self.assertEqual(self.read(target), "# synced\n")

    def test_plain_skill_written_in_place(self):
        target = os.path.join(self.tree["plain"], "SKILL.md")
        result = self.skill_apply(target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(TEXT, self.read(target))
        self.assertEqual(self.read(self.claude_md), "# Mine\n")

    def test_tracked_skill_refused_without_confirm_written_with_it_never_staged(self):
        target = os.path.join(self.tree["tracked"], "SKILL.md")
        self.write_pack([{"id": A, "kind": "ask", "topic": "t", "evidence_ids": ["e1"], "repos": []}], {})
        refused = self.skill_apply(target, "--pack", self.pack)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("result=skipped:git-tracked", self.out(refused))
        self.assertEqual(self.read(os.path.join(self.tree["tracked_target"], "SKILL.md")), "# tracked\n")
        written = self.skill_apply(target, "--pack", self.pack, "--confirm-tracked")
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertIn(TEXT, self.read(os.path.join(self.tree["tracked_target"], "SKILL.md")))
        self.assertFalse(os.path.exists(os.path.join(self.tree["repo"], ".git", "index")))
        row = ledger.read_applied(self.home)[-1]
        self.assertIs(row["tracked"], True)
        self.assertEqual(row["remote_visibility"], "private")

    def test_tracked_requires_pack(self):
        target = os.path.join(self.tree["tracked"], "SKILL.md")
        result = self.skill_apply(target, "--confirm-tracked")
        self.assertEqual(result.returncode, 1)
        self.assertIn("result=skipped:no-pack", self.out(result))

    def test_sensitive_ticket_email_uuid_repo_token(self):
        target = os.path.join(self.tree["tracked"], "SKILL.md")
        self.write_pack([{"id": A, "kind": "ask", "topic": "t", "evidence_ids": [], "repos": ["acme/widgets"]}], {})
        email = "alice" + "@" + "acme" + ".test"
        cases = (("Check XX-123 first.", "ticket"), ("Ask %s about it." % email, "email"),
                 ("Use id 123e4567-e89b-12d3-a456-426614174000 here.", "uuid"),
                 ("Never touch widgets internals.", "repo"))
        for text, kind in cases:
            result = self.run_script("--id", A, "--text-file", self.text_file(text), "--scope", "skill",
                                     "--target", target, "--pack", self.pack, "--confirm-tracked")
            self.assertEqual(result.returncode, 1)
            self.assertIn("result=skipped:sensitive-text:%s" % kind, self.out(result))
        secret = "ghp_" + "a" * 24
        untracked = self.run_script("--id", A, "--text-file", self.text_file("Use %s now." % secret),
                                    "--scope", "global")
        self.assertEqual(untracked.returncode, 1)
        self.assertIn("result=skipped:sensitive-text:github-token", self.out(untracked))
        self.assertEqual(self.read(self.claude_md), "# Mine\n")
        self.assertEqual(self.read(os.path.join(self.tree["tracked_target"], "SKILL.md")), "# tracked\n")

    def test_public_skills_target_needs_confirm_and_checks(self):
        self.write(os.path.join(self.home, "config.json"), json.dumps({"public_skills": ["mirrored-skill"]}))
        target = os.path.join(self.tree["mirrored"], "SKILL.md")
        self.write_pack([{"id": A, "kind": "ask", "topic": "t", "evidence_ids": [], "repos": []}], {})
        refused = self.skill_apply(target, "--pack", self.pack)
        self.assertIn("result=skipped:git-tracked", self.out(refused))
        email = "bob" + "@" + "acme" + ".test"
        leaky = self.run_script("--id", A, "--text-file", self.text_file("Mail %s." % email), "--scope", "skill",
                                "--target", target, "--pack", self.pack, "--confirm-tracked")
        self.assertIn("result=skipped:sensitive-text:email", self.out(leaky))
        written = self.skill_apply(target, "--pack", self.pack, "--confirm-tracked")
        self.assertEqual(written.returncode, 0, written.stderr)
        row = ledger.read_applied(self.home)[-1]
        self.assertEqual((row["tracked"], row["remote_visibility"]), (True, "public"))

    def test_repo_claude_md_refused(self):
        repo = os.path.join(self.home, "workrepo")
        self.write(os.path.join(repo, ".git", "HEAD"), "ref: refs/heads/main\n")
        target = self.write(os.path.join(repo, "CLAUDE.md"), "# Repo\n")
        self.write_pack([{"id": A, "kind": "ask", "topic": "t", "evidence_ids": [], "repos": []}], {})
        result = self.skill_apply(target, "--pack", self.pack, "--confirm-tracked")
        self.assertEqual(result.returncode, 1)
        # a skill-scope target must be a SKILL.md, so a repo CLAUDE.md is refused before the repo-claude-md check
        self.assertIn("result=skipped:bad-target", self.out(result))
        self.assertEqual(self.read(target), "# Repo\n")


class UndoReplayTest(ApplyCliCase):
    def test_undo_removes_and_records(self):
        self.applied_global(A)
        result = self.run_script("--undo", A)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("result=removed", self.out(result))
        self.assertEqual(self.read(self.claude_md), "# Mine\n")
        self.assertEqual(ledger.read_applied(self.home)[-1]["action"], "undo")
        self.assertEqual(self.proposals(A)[-1]["status"], "undone")
        unknown = self.run_script("--undo", Z)
        self.assertEqual(unknown.returncode, 1)
        self.assertIn("result=skipped:not-applied", self.out(unknown))

    def test_replay_exact_line_and_unchanged_when_present(self):
        self.applied_global(A)
        line = ledger.read_applied(self.home)[0]["line"]
        with_rule = self.read(self.claude_md)
        replay = self.run_script("--replay")
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertIn("result=unchanged", self.out(replay))
        self.assertEqual(self.read(self.claude_md), with_rule)
        self.write(self.claude_md, "# Mine\n")
        replay = self.run_script("--replay")
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertIn("result=written", self.out(replay))
        self.assertEqual(self.read(self.claude_md), with_rule)
        self.assertIn(line, self.read(self.claude_md))
        self.assertEqual(ledger.read_applied(self.home)[-1]["action"], "replay")
        dry = self.run_script("--replay", "--dry-run")
        self.assertEqual(dry.returncode, 0)

    def test_replay_skips_undone_and_dry_run_writes_nothing(self):
        self.applied_global(A)
        self.run_script("--undo", A)
        replay = self.run_script("--replay")
        self.assertEqual(replay.returncode, 0)
        self.assertEqual(self.read(self.claude_md), "# Mine\n")
        self.applied_global(B)
        self.write(self.claude_md, "# Mine\n")
        dry = self.run_script("--replay", "--dry-run")
        self.assertIn("+- " + TEXT, self.out(dry))
        self.assertEqual(self.read(self.claude_md), "# Mine\n")

    def test_replay_tracked_needs_confirm(self):
        tree = skills.build(self.home, self.fake_home)
        target = os.path.join(tree["tracked"], "SKILL.md")
        real = os.path.join(tree["tracked_target"], "SKILL.md")
        self.write_pack([{"id": A, "kind": "ask", "topic": "t", "evidence_ids": [], "repos": []}], {})
        self.run_script("--id", A, "--text-file", self.text_file(), "--scope", "skill", "--target", target,
                        "--pack", self.pack, "--confirm-tracked")
        self.write(real, "# tracked\n")
        refused = self.run_script("--replay")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("result=skipped:git-tracked", self.out(refused))
        self.assertEqual(self.read(real), "# tracked\n")
        confirmed = self.run_script("--replay", "--confirm-tracked")
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        self.assertIn(TEXT, self.read(real))


class UnitTest(ApprenticeTestCase):
    def test_sensitive_check_unit(self):
        cfg = config.load_config(self.home)
        self.assertIsNone(apply.sensitive_check("Plain advice.", True, ["acme/widgets"], cfg))
        self.assertEqual(apply.sensitive_check("See XX-123.", False, [], cfg), None)
        self.assertEqual(apply.sensitive_check("See XX-123.", True, [], cfg), "sensitive-text:ticket")
        self.assertEqual(apply.sensitive_check("About Acme/Widgets here", True, ["acme/widgets"], cfg),
                         "sensitive-text:repo")
        self.assertIsNone(apply.sensitive_check("widgetsmith is fine", True, ["acme/widgets"], cfg))
        self.assertEqual(apply.sensitive_check("key ghp_" + "b" * 24, False, [], cfg),
                         "sensitive-text:github-token")

    def test_resolve_target_skill_without_target(self):
        cfg = config.load_config(self.home)
        self.assertEqual(apply.resolve_target("skill", None, None, cfg, self.fake_home),
                         (None, "skipped:no-target"))
        tree = skills.build(self.home, self.fake_home)
        found = apply.resolve_target("skill", None, "plain-skill", cfg, self.fake_home)
        self.assertEqual(found, (os.path.join(tree["plain"], "SKILL.md"), None))
        path, error = apply.resolve_target("global", None, None, cfg, self.fake_home)
        self.assertIsNone(error)
        self.assertEqual(path, os.path.realpath(os.path.join(self.fake_home, ".claude", "CLAUDE.md")))
