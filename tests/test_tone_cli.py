import hashlib
import json
import os
import stat
import subprocess
from unittest import mock

from apprentice import apply, config, events, lexicon, tone
from tests.base import REPO_ROOT, ApprenticeTestCase, isolated_env
from tests.fixtures.events import tone_corpus
from tests.fixtures.events.tone_corpus import make_prompt

_SCRIPT = os.path.join(REPO_ROOT, "scripts", "tone.py")
_COMMAND = os.path.join(REPO_ROOT, "commands", "tone.md")
_FAKE_GH = os.path.join(REPO_ROOT, "tests", "fixtures", "fake-gh")
WORK_ENV = {"CLAUDE_CONFIG_DIR": "/nowhere/work"}
SECTIONS = ["## Stats", "## Registers", "## Vocabulary and phrases", "## Punctuation and casing",
            "## Spelling and patterns", "## Proficiency estimate", "## Exemplars",
            "## How to use: drafting as the user", "## How to use: talking to the user"]
POINTER_ALICE = ("**When drafting text as Alice or answering them, follow the apprentice tone file named in this "
                 "session's SessionStart line.** With no such line in context, use no tone file, and never open "
                 "another `tone-*.md` file.")
# words only W* texts (the other group) use: none may reach the default profile's files ("informations" is also
# a pattern name, so it is not listed)
HIDDEN_ONLY = ["zorbalice", "dashboard", "unicode", "parser"]


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class ToneCliTestCase(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = config.load_config(self.home)
        dict_path = self.write(os.path.join(self.fake_home, "words"), tone_corpus.corpus_dictionary_text())
        patcher = mock.patch.object(lexicon, "DICT_PATH", dict_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, records):
        for record in records:
            events.append_event(self.home, record)

    def build(self, env=None):
        profile = tone.build_profile(self.home, env or {}, self.cfg, self.fake_home)
        return profile, tone.write_profile(self.home, profile)

    def model_file(self, **fields):
        return self.write(os.path.join(self.home, "state", "tmp", "model.json"), json.dumps(fields))

    def run_script(self, *args):
        env = isolated_env()
        env.update({"APPRENTICE_HOME": self.home, "HOME": self.fake_home, "APPRENTICE_GH_BIN": _FAKE_GH,
                    "FAKE_GH_MODE": "private"})
        env.pop("CLAUDE_CONFIG_DIR", None)
        return subprocess.run(["/usr/bin/python3", "-I", "-S", _SCRIPT] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, universal_newlines=True)


class BuildTests(ToneCliTestCase):
    def test_build_writes_group_pair_and_leaves_other_group_byte_identical(self):
        self.add(tone_corpus.corpus())
        profile, (json_path, md_path) = self.build()
        self.assertEqual(profile["group"], "group-1")
        self.assertEqual(json_path, os.path.join(self.home, "profile", "tone-group-1.json"))
        self.assertEqual(md_path, os.path.join(self.home, "profile", "tone-group-1.md"))
        self.assertEqual(stat.S_IMODE(os.stat(json_path).st_mode), 0o600)
        self.assertEqual((profile["prompts_group"], profile["prompts_all"]), (12, 20))
        self.assertEqual(profile["exemplars_left_out"], 8)
        self.assertEqual(profile["model"], {})
        self.assertEqual(_read_json(json_path), profile)
        before = (_read_bytes(json_path), _read_bytes(md_path))
        own_text = before[0].decode("utf-8") + before[1].decode("utf-8")
        for word in HIDDEN_ONLY:
            self.assertNotIn(word, own_text)
        self.assertIn("recieve", own_text)

        other, (other_json, other_md) = self.build(WORK_ENV)
        self.assertEqual(other["group"], "group-2")
        self.assertEqual(other_json, os.path.join(self.home, "profile", "tone-group-2.json"))
        self.assertIn("zorbalice", _read_bytes(other_md).decode("utf-8"))
        self.assertEqual((_read_bytes(json_path), _read_bytes(md_path)), before)

    def test_repo_pieces_come_from_every_event(self):
        from tests.fixtures.events.prompts import prompt_event
        ask = prompt_event(kind="ask", profile=tone_corpus.HIDDEN_PROFILE, repo="acme/flimflam",
                           cwd="/work/zorptool")
        self.add(tone_corpus.corpus() + [ask])
        with mock.patch.object(tone, "compute_metrics", wraps=tone.compute_metrics) as spy:
            self.build()
        pieces = spy.call_args[1]["repo_pieces"]
        self.assertTrue({"acme", "flimflam", "work", "zorptool"} <= pieces)

    def test_low_sample_banner(self):
        self.add(tone_corpus.corpus())
        profile, _ = self.build()
        self.assertTrue(profile["low_sample"])
        self.assertIn("Low sample", tone.render_md(profile).split("## Stats")[0])
        profile["low_sample"] = False
        self.assertNotIn("Low sample", tone.render_md(profile))

    def test_no_visible_prompts_line(self):
        self.add([event for event in tone_corpus.corpus() if event["profile"] == tone_corpus.HIDDEN_PROFILE])
        profile, (_, md_path) = self.build()
        self.assertEqual((profile["prompts_group"], profile["prompts_all"]), (0, 8))
        self.assertEqual(profile["exemplars"], [])
        md = _read_bytes(md_path).decode("utf-8")
        line = next(line for line in md.splitlines() if "account_groups" in line)
        self.assertIn("unknown_group", line)
        own, _ = self.build(WORK_ENV)
        self.assertNotIn("account_groups", tone.render_md(own))

    def test_sections_order(self):
        self.add(tone_corpus.corpus())
        profile, _ = self.build()
        md = tone.render_md(profile)
        pos = -1
        for header in SECTIONS:
            found = md.find(header, pos + 1)
            self.assertNotEqual(found, -1, "missing or out of order: %s" % header)
            pos = found
        self.assertIn("observed patterns", md[md.find("## Spelling and patterns"):].splitlines()[0])
        self.assertIn("not yet assessed", md)
        profile["model"] = {"proficiency_band": "B1", "proficiency_justification": "short clauses",
                            "prose": "Write like this."}
        with_prose = tone.render_md(profile)
        self.assertIn("Write like this.", with_prose)
        self.assertNotIn("## How to use: drafting as the user", with_prose)
        self.assertNotIn("## How to use: talking to the user", with_prose)
        self.assertIn("B1", with_prose)

    def test_typos_note_replaces_typo_list_without_dictionary(self):
        self.add(tone_corpus.corpus())
        with mock.patch.object(lexicon, "DICT_PATH", os.path.join(self.fake_home, "missing-words")):
            profile, _ = self.build()
        md = tone.render_md(profile)
        self.assertIsNone(profile["metrics"]["spelling"]["typos"])
        self.assertIn(profile["metrics"]["spelling"]["typos_note"], md)


class SetModelTests(ToneCliTestCase):
    def test_set_model_only_model_key_changes(self):
        self.add(tone_corpus.corpus())
        _, (json_path, md_path) = self.build()
        before = _read_json(json_path)
        raw_metrics = json.dumps(before["metrics"], ensure_ascii=False, indent=2)
        path = self.model_file(proficiency_band="B2", proficiency_justification="sentence_len_mean 8.2 ...",
                               prose="Draft short lines.", metrics={"counts": {"all": 999}}, group="group-9")
        result = tone.set_model(self.home, {}, self.cfg, path)
        after = _read_json(json_path)
        self.assertEqual(after["model"], {"proficiency_band": "B2",
                                          "proficiency_justification": "sentence_len_mean 8.2 ...",
                                          "prose": "Draft short lines.",
                                          "assessed_inputs": before["metrics"]["proficiency_inputs"]})
        self.assertIs(after["reassess_band"], False)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(result, after)
        self.assertEqual(json.dumps(after["metrics"], ensure_ascii=False, indent=2), raw_metrics)
        self.assertEqual({k: v for k, v in after.items() if k not in ("model", "reassess_band")},
                         {k: v for k, v in before.items() if k not in ("model", "reassess_band")})
        md = _read_bytes(md_path).decode("utf-8")
        self.assertIn("B2", md)
        self.assertIn("Draft short lines.", md)

    def test_set_model_rejects_bad_band(self):
        self.add(tone_corpus.corpus())
        _, (json_path, _) = self.build()
        before = _read_bytes(json_path)
        bad = [{"proficiency_band": "D1", "proficiency_justification": "x"},
               {"proficiency_band": "b2", "proficiency_justification": "x"},
               {"proficiency_justification": "x"},
               {"proficiency_band": "B2", "proficiency_justification": "x" * 2001},
               {"proficiency_band": "B2"},
               {"proficiency_band": "B2", "proficiency_justification": "x", "prose": 3}]
        for fields in bad:
            path = self.model_file(**fields)
            with self.assertRaises(tone.ToneError) as caught:
                tone.set_model(self.home, {}, self.cfg, path)
            self.assertEqual(caught.exception.code, 2, fields)
            self.assertFalse(os.path.exists(path), fields)
        self.assertEqual(_read_bytes(json_path), before)

    def test_set_model_file_confined_to_state_tmp(self):
        self.add(tone_corpus.corpus())
        _, (json_path, _) = self.build()
        before = _read_bytes(json_path)
        outside = self.write(os.path.join(self.home, "model.json"),
                             json.dumps({"proficiency_band": "B2", "proficiency_justification": "x"}))
        with self.assertRaises(tone.ToneError) as caught:
            tone.set_model(self.home, {}, self.cfg, outside)
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("state/tmp", str(caught.exception))
        self.assertTrue(os.path.exists(outside))
        self.assertEqual(_read_bytes(json_path), before)

    def test_script_set_model_deletes_input_on_success_and_failure(self):
        self.add(tone_corpus.corpus())
        self.build()
        good = self.write(os.path.join(self.home, "state", "tmp", "group-1-tone-model.json"),
                          json.dumps({"proficiency_band": "B2", "proficiency_justification": "x"}))
        result = self.run_script("--set-model", good)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(good))
        bad = self.write(os.path.join(self.home, "state", "tmp", "group-1-tone-model.json"), "{not json")
        result = self.run_script("--set-model", bad)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(os.path.exists(bad))

    def test_set_model_writes_current_group_only(self):
        self.add(tone_corpus.corpus())
        _, (own_json, own_md) = self.build()
        path = self.model_file(proficiency_band="C1", proficiency_justification="dense clauses")
        with self.assertRaises(tone.ToneError) as caught:
            tone.set_model(self.home, WORK_ENV, self.cfg, path)
        self.assertEqual(caught.exception.code, 2)
        self.build(WORK_ENV)
        before = (_read_bytes(own_json), _read_bytes(own_md))
        path = self.model_file(proficiency_band="C1", proficiency_justification="dense clauses")
        tone.set_model(self.home, WORK_ENV, self.cfg, path)
        other = _read_json(os.path.join(self.home, "profile", "tone-group-2.json"))
        self.assertEqual(other["model"]["proficiency_band"], "C1")
        self.assertNotIn("prose", other["model"])
        self.assertEqual((_read_bytes(own_json), _read_bytes(own_md)), before)


class RefreshTests(ToneCliTestCase):
    def test_refresh_keeps_model_and_flags_reassess(self):
        self.add(tone_corpus.corpus())
        self.build()
        tone.set_model(self.home, {}, self.cfg,
                       self.model_file(proficiency_band="B1", proficiency_justification="short"))
        same = tone.refresh(self.home, {}, self.cfg, self.fake_home)
        self.assertEqual(same["model"]["proficiency_band"], "B1")
        self.assertIs(same["reassess_band"], False)
        long_text = ("we keep the old parser which handles the legacy rows, although the new one is faster "
                     "unless the batch is huge, and whether we switch depends on the load test results today")
        self.add([make_prompt("L%d" % i, tone_corpus.VISIBLE_PROFILE, "direction", long_text + " %d" % i, 21 + i)
                  for i in range(5)])
        moved = tone.refresh(self.home, {}, self.cfg, self.fake_home)
        self.assertEqual(moved["model"]["proficiency_band"], "B1")
        self.assertIs(moved["reassess_band"], True)
        self.assertEqual(moved["prompts_all"], 25)
        self.assertIn("reassess", tone.render_md(moved).lower())

    def test_set_model_clears_reassess_flag(self):
        self.add(tone_corpus.corpus())
        profile, (json_path, md_path) = self.build()
        profile["reassess_band"] = True
        tone.write_profile(self.home, profile)
        self.assertIn("reassess the band", _read_bytes(md_path).decode("utf-8"))
        stored = tone.set_model(self.home, {}, self.cfg,
                                self.model_file(proficiency_band="B2", proficiency_justification="x"))
        self.assertIs(stored["reassess_band"], False)
        self.assertIs(_read_json(json_path)["reassess_band"], False)
        self.assertNotIn("reassess the band", _read_bytes(md_path).decode("utf-8"))

    def _rewrite(self, json_path, change):
        data = _read_json(json_path)
        change(data)
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_refresh_compares_against_assessed_inputs(self):
        self.add(tone_corpus.corpus())
        _, (json_path, _) = self.build()
        tone.set_model(self.home, {}, self.cfg,
                       self.model_file(proficiency_band="B1", proficiency_justification="short"))
        current = dict(_read_json(json_path)["metrics"]["proficiency_inputs"])
        far = {name: (value * 2 if value else 1) for name, value in current.items()}

        # the last build matches, the assessed inputs are far: drift since the assessment counts
        self._rewrite(json_path, lambda data: data["model"].__setitem__("assessed_inputs", far))
        self.assertIs(tone.refresh(self.home, {}, self.cfg, self.fake_home)["reassess_band"], True)

        # the last build is far, the assessed inputs match: no reassessment
        def far_build(data):
            data["model"]["assessed_inputs"] = current
            data["metrics"]["proficiency_inputs"] = far
        self._rewrite(json_path, far_build)
        self.assertIs(tone.refresh(self.home, {}, self.cfg, self.fake_home)["reassess_band"], False)

        # no assessed inputs stored (an older file): the previous build is the baseline
        def legacy(data):
            data["model"].pop("assessed_inputs")
            data["metrics"]["proficiency_inputs"] = far
        self._rewrite(json_path, legacy)
        self.assertIs(tone.refresh(self.home, {}, self.cfg, self.fake_home)["reassess_band"], True)


class PointerTests(ToneCliTestCase):
    def test_pointer_id_stable_and_text(self):
        self.assertEqual(tone.TONE_POINTER_ID, "r_" + hashlib.sha1(b"tone-pointer").hexdigest()[:8])
        self.assertEqual(tone.TONE_POINTER_ID, "r_ec4ee9bc")
        self.assertEqual(tone.pointer_rule_text("Alice"), POINTER_ALICE)

    def test_pointer_apply_twice_unchanged(self):
        target = self.write(os.path.join(self.fake_home, ".claude", "CLAUDE.md"), "# Global\n\nBe brief.\n")
        text = tone.pointer_rule_text("Alice")
        first = apply.apply_rule_to_file(target, tone.TONE_POINTER_ID, text, 0, "manual", "2030-01-01", self.home,
                                         cfg=self.cfg)
        self.assertEqual(first["result"], "written")
        content = _read_bytes(target)
        second = apply.apply_rule_to_file(target, tone.TONE_POINTER_ID, text, 0, "manual", "2030-02-01", self.home,
                                          cfg=self.cfg)
        self.assertEqual(second["result"], "unchanged")
        self.assertEqual(_read_bytes(target), content)
        lines = [line for line in content.decode("utf-8").splitlines() if tone.TONE_POINTER_ID in line]
        self.assertEqual(lines, [apply.render_line(tone.TONE_POINTER_ID, text, 0, "manual", "2030-01-01")])


class InstallPointerTests(ToneCliTestCase):
    def global_md(self):
        return os.path.join(self.fake_home, ".claude", "CLAUDE.md")

    def tracked_global(self):
        self.write(os.path.join(self.fake_home, ".claude", ".git", "HEAD"), "ref: refs/heads/main\n")
        self.write(os.path.join(self.fake_home, ".claude", ".git", "config"),
                   '[remote "origin"]\n\turl = https://github.com/acme/widgets.git\n')
        return self.write(self.global_md(), "# Global\n")

    def test_tracked_global_needs_confirm_tracked(self):
        target = self.tracked_global()
        refused = self.run_script("--install-pointer")
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn("result=skipped:git-tracked", refused.stdout)
        self.assertEqual(_read_bytes(target), b"# Global\n")
        written = self.run_script("--install-pointer", "--confirm-tracked")
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(written.stdout.splitlines()[0], "result=written file=%s id=%s"
                         % (os.path.realpath(target), tone.TONE_POINTER_ID))
        self.assertIn(tone.pointer_rule_text("the user"), _read_bytes(target).decode("utf-8"))
        self.assertIs(apply.ledger.read_applied(self.home)[-1]["tracked"], True)

    def test_tracked_dry_run_shows_tracking_and_diff_and_writes_nothing(self):
        target = self.tracked_global()
        result = self.run_script("--install-pointer", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(lines[0].startswith("result=written file="), lines)
        self.assertIn("tracked=%s" % os.path.join(self.fake_home, ".claude"), lines)
        self.assertIn("remote=acme/widgets", lines)
        self.assertIn("remote_visibility=private", lines)
        self.assertIn("+- " + tone.pointer_rule_text("the user"), result.stdout)
        self.assertEqual(_read_bytes(target), b"# Global\n")
        self.assertEqual(apply.ledger.read_applied(self.home), [])

    def test_untracked_output_has_no_tracking_lines(self):
        self.write(self.global_md(), "# Global\n")
        result = self.run_script("--install-pointer", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse([line for line in result.stdout.splitlines()
                          if line.startswith(("tracked=", "remote=", "remote_visibility="))])

    def test_missing_global_is_created_private_and_second_run_unchanged(self):
        target = self.global_md()
        preview = self.run_script("--install-pointer", "--dry-run")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn("result=written", preview.stdout)
        self.assertIn("+- " + tone.pointer_rule_text("the user"), preview.stdout)
        self.assertFalse(os.path.lexists(target))
        first = self.run_script("--install-pointer")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("result=written", first.stdout)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        content = _read_bytes(target)
        self.assertIn(tone.pointer_rule_text("the user"), content.decode("utf-8"))
        second = self.run_script("--install-pointer")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("result=unchanged", second.stdout)
        self.assertEqual(_read_bytes(target), content)
        accepted = [e for e in apply.ledger.read_proposals(self.home) if e["id"] == tone.TONE_POINTER_ID]
        self.assertEqual(accepted[0]["status"], "accepted")

    def test_no_pack_gate_still_holds_for_other_ids(self):
        self.tracked_global()
        text = self.write(os.path.join(self.home, "state", "tmp", "r.txt"), "Run the linter.")
        env = isolated_env()
        env.update({"APPRENTICE_HOME": self.home, "HOME": self.fake_home, "APPRENTICE_GH_BIN": _FAKE_GH})
        env.pop("CLAUDE_CONFIG_DIR", None)
        result = subprocess.run(["/usr/bin/python3", "-I", "-S", os.path.join(REPO_ROOT, "scripts", "apply.py"),
                                 "--id", tone.TONE_POINTER_ID, "--text-file", text, "--scope", "global",
                                 "--confirm-tracked"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                universal_newlines=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("result=skipped:no-pack", result.stdout)


class ScriptTests(ToneCliTestCase):
    def test_set_display_name_merges_config(self):
        self.write(os.path.join(self.home, "config.json"),
                   json.dumps({"retention_months": 12, "account_groups": {"home": ["default"]}}))
        tone.set_display_name(self.home, "Alice")
        path = os.path.join(self.home, "config.json")
        self.assertEqual(_read_json(path), {"retention_months": 12, "account_groups": {"home": ["default"]},
                                            "display_name": "Alice"})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        result = self.run_script("--set-display-name", "Bob")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(_read_json(path)["display_name"], "Bob")
        self.assertEqual(_read_json(path)["retention_months"], 12)
        for bad in ("", "  ", "Al\nice"):
            with self.assertRaises(tone.ToneError):
                tone.set_display_name(self.home, bad)
        self.write(path, "{not json")
        with self.assertRaises(tone.ToneError):
            tone.set_display_name(self.home, "Alice")
        self.assertEqual(_read_bytes(path), b"{not json")

    def test_display_name_file_confined_and_deleted(self):
        name_file = self.write(os.path.join(self.home, "state", "tmp", "group-1-display-name.txt"),
                               'Al "$(x)" `y`\n')
        result = self.run_script("--display-name-file", name_file)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(_read_json(os.path.join(self.home, "config.json"))["display_name"], 'Al "$(x)" `y`')
        self.assertFalse(os.path.exists(name_file))
        bad = self.write(os.path.join(self.home, "state", "tmp", "group-1-display-name.txt"), "")
        result = self.run_script("--display-name-file", bad)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(os.path.exists(bad))
        outside = self.write(os.path.join(self.home, "name.txt"), "Bob\n")
        result = self.run_script("--display-name-file", outside)
        self.assertEqual(result.returncode, 2)
        self.assertIn("state/tmp", result.stderr)
        self.assertTrue(os.path.exists(outside))

    def test_set_display_name_keeps_symlinked_config(self):
        real = self.write(os.path.join(self.fake_home, "dotfiles", "config.json"), json.dumps({"retention_months": 6}))
        link = os.path.join(self.home, "config.json")
        os.symlink(real, link)
        tone.set_display_name(self.home, "Alice")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(_read_json(real), {"retention_months": 6, "display_name": "Alice"})

    def test_print_pointer_treats_non_string_display_name_as_unset(self):
        for value in (["Alice"], 42, None, {"n": 1}):
            self.write(os.path.join(self.home, "config.json"), json.dumps({"display_name": value}))
            result = self.run_script("--print-pointer")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, tone.pointer_rule_text("the user") + "\n", value)

    def test_scripts_sweep_stale_tmp_files_at_start(self):
        import time
        for script in ("tone", "distill"):
            stale = self.write(os.path.join(self.home, "state", "tmp", "group-1-r_0000000a.txt"), "x")
            fresh = self.write(os.path.join(self.home, "state", "tmp", "group-1-r_0000000b.txt"), "x")
            old = time.time() - 7200
            os.utime(stale, (old, old))
            env = isolated_env()
            env.update({"APPRENTICE_HOME": self.home, "HOME": self.fake_home})
            env.pop("CLAUDE_CONFIG_DIR", None)
            args = ["--print-pointer"] if script == "tone" else []
            result = subprocess.run(["/usr/bin/python3", "-I", "-S", os.path.join(REPO_ROOT, "scripts",
                                                                                 script + ".py")] + args,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                    universal_newlines=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(os.path.exists(stale), script)
            self.assertTrue(os.path.exists(fresh), script)

    def test_scripts_print_group_line(self):
        self.add(tone_corpus.corpus())
        built = self.run_script()
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertIn("group=group-1", built.stdout.splitlines())
        env = isolated_env()
        env.update({"APPRENTICE_HOME": self.home, "HOME": self.fake_home})
        env.pop("CLAUDE_CONFIG_DIR", None)
        result = subprocess.run(["/usr/bin/python3", "-I", "-S", os.path.join(REPO_ROOT, "scripts", "distill.py")],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[2], "group=group-1")
        self.assertEqual(_read_json(lines[0])["inputs"]["group"], "group-1")

    def test_script_help_and_removed_flags(self):
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("--refresh", "--set-model", "--print-pointer", "--set-display-name", "--display-name-file",
                     "--install-pointer", "--confirm-tracked", "--dry-run"):
            self.assertIn(flag, result.stdout)
        for flag in ("--include-unknown", "--all-profiles"):
            removed = self.run_script(flag)
            self.assertEqual(removed.returncode, 2, flag)
            self.assertIn("removed flag", removed.stderr)
        printed = self.run_script("--print-pointer")
        self.assertEqual(printed.returncode, 0, printed.stderr)
        self.assertEqual(printed.stdout, tone.pointer_rule_text("the user") + "\n")
        missing = self.run_script("--set-model", self.model_file(proficiency_band="B2",
                                                                 proficiency_justification="x"))
        self.assertEqual(missing.returncode, 2)
        self.assertIn("tone.py:", missing.stderr)

        self.add(tone_corpus.corpus())
        built = self.run_script()
        self.assertEqual(built.returncode, 0, built.stderr)
        lines = built.stdout.splitlines()
        self.assertEqual(lines[:2], [os.path.join(self.home, "profile", "tone-group-1.json"),
                                     os.path.join(self.home, "profile", "tone-group-1.md")])
        self.assertEqual(lines[2:], ["group=group-1", "display_name unset"])
        tone.set_display_name(self.home, "Alice")
        self.assertEqual(self.run_script("--refresh").stdout.splitlines()[2:], ["group=group-1"])
        self.assertEqual(self.run_script("--print-pointer").stdout, POINTER_ALICE + "\n")

    def test_tone_command(self):
        with open(_COMMAND, "r", encoding="utf-8") as fh:
            text = fh.read()
        head = text.split("---\n")[1]
        fields = dict(line.split(": ", 1) for line in head.splitlines())
        self.assertEqual(fields["description"],
                         "Build the writing profile for this account group and install the tone pointer")
        self.assertEqual(fields["argument-hint"], "[--refresh]")
        self.assertEqual(fields["allowed-tools"],
                         "Bash(${APPRENTICE_PYTHON:-/usr/bin/python3} *), AskUserQuestion, Read, Write")
        for needle in ('"<root>/scripts/tone.py" $ARGUMENTS', "--set-model <home>/state/tmp/<group>-tone-model.json",
                       "group=<group>", "Install tone pointer in global CLAUDE.md (Recommended)", "Not now",
                       tone.TONE_POINTER_ID, '"<root>/scripts/tone.py" --install-pointer --dry-run',
                       '"<root>/scripts/tone.py" --install-pointer --confirm-tracked',
                       '"<root>/scripts/tone.py" --install-pointer`', "`tracked=`", "`remote_visibility=`",
                       "`Install into the tracked file`", "On exit 1, print the result line and stop",
                       "--display-name-file <home>/state/tmp/<group>-display-name.txt",
                       "Rewrite", "proficiency_justification"):
            self.assertIn(needle, text)
        for absent in ("--text-file", "apply.py", "--print-pointer", "--set-display-name", "<text>",
                       "tone-pointer.txt"):
            self.assertNotIn(absent, text)
        # Other text sets the name only when step 2 printed `display_name unset`; otherwise it is a reply
        self.assertIn("Otherwise treat the Other text as a reply: answer it, install nothing, and stop", text)
