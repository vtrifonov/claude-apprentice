import json
import os
import subprocess
import sys

from apprentice import cluster, config, events, preclassify
from tests.base import REPO_ROOT, ApprenticeTestCase, isolated_env
from tests.fixtures.events.prompts import prompt_event

_FAKE = os.path.join(REPO_ROOT, "tests", "fixtures", "fake-claude")
_SCRIPT = os.path.join(REPO_ROOT, "scripts", "preclassify.py")


def _ts(index):
    return "2030-01-05T10:%02d:%02d.000Z" % (index // 60, index % 60)


def _prompt(index, **overrides):
    fields = {"id": "evt-%03d" % index, "ts": _ts(index), "text": "no, use helper number %d" % index}
    fields.update(overrides)
    return prompt_event(**fields)


def _pairs(items):
    return cluster.select_pairs(items)


class _Base(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.dump = os.path.join(self.fake_home, "dump.jsonl")
        self.logged = []
        self.env = {"APPRENTICE_CLAUDE_BIN": _FAKE, "FAKE_CLAUDE_MODE": "valid", "FAKE_CLAUDE_DUMP": self.dump,
                    "PATH": os.environ.get("PATH", "")}

    def log(self, line):
        self.logged.append(line)

    def run_batches(self, items, max_batches=None, env=None):
        return preclassify.run_batches(_pairs(items), self.home, env or self.env, max_batches, self.log)

    def calls(self):
        try:
            with open(self.dump, "r", encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except OSError:
            return []

    def sent_ids(self, call):
        return [pair["id"] for pair in json.loads(call["stdin"].strip().splitlines()[-1])]


class BatchTest(_Base):
    def test_batches_of_40(self):
        summary = self.run_batches([_prompt(i) for i in range(85)])
        calls = self.calls()
        self.assertEqual([len(self.sent_ids(call)) for call in calls], [40, 40, 5])
        self.assertEqual(summary, {"batches": 3, "cached": 85, "skipped": 0, "stopped_early": False})

    def test_max_batches_stops_early(self):
        summary = self.run_batches([_prompt(i) for i in range(85)], max_batches=1)
        self.assertEqual(summary["batches"], 1)
        self.assertEqual(summary["cached"], 40)
        self.assertTrue(summary["stopped_early"])

    def test_cache_hit_skips_pair(self):
        items = [_prompt(i) for i in range(3)]
        preclassify.write_cache(self.home, "evt-001", {"is_correction": True, "topic": "x", "rule_sketch": "y",
                                                       "confidence": 0.5}, "haiku")
        todo = preclassify.select_uncached(items, self.home, "default")
        self.assertEqual(sorted(p["event"]["id"] for p in todo), ["evt-000", "evt-002"])
        preclassify.run_batches(todo, self.home, self.env, None, self.log)
        self.assertEqual(sorted(self.sent_ids(self.calls()[0])), ["evt-000", "evt-002"])

    def test_valid_items_cached_with_model_and_ts(self):
        self.run_batches([_prompt(1)])
        cached = preclassify.read_cache(self.home)
        self.assertEqual(list(cached), ["evt-001"])
        item = cached["evt-001"]
        self.assertEqual(item["model"], "haiku")
        self.assertTrue(config.ts_epoch(item["ts"]) is not None)
        self.assertIs(item["is_correction"], True)
        self.assertEqual(item["topic"], "use-other-helper")
        self.assertEqual(preclassify.cache_path(self.home, "evt-001"),
                         os.path.join(self.home, "state", "preclass", "evt-001.json"))

    def test_fenced_result_is_accepted(self):
        env = dict(self.env, FAKE_CLAUDE_MODE="fence")
        self.run_batches([_prompt(1)], env=env)
        self.assertEqual(list(preclassify.read_cache(self.home)), ["evt-001"])

    def test_invalid_items_discarded(self):
        good = {"id": "evt-001", "is_correction": False, "topic": "Naming Style", "rule_sketch": "keep names short",
                "confidence": 0.7}
        items = [
            good,
            dict(good, id="unknown-id"),
            {"id": "evt-002", "is_correction": True, "topic": "t"},
            dict(good, id="evt-003", is_correction="yes"),
            dict(good, id="evt-004", confidence=1.5),
            dict(good, id="evt-005", confidence=-0.1),
            dict(good, id="evt-006", rule_sketch=" ".join(["w"] * 21)),
            dict(good, id="evt-007", confidence=True),
            "not an object",
        ]
        env = dict(self.env, FAKE_CLAUDE_MODE="raw", FAKE_CLAUDE_RESULT=json.dumps(items))
        summary = self.run_batches([_prompt(i) for i in range(1, 8)], env=env)
        cached = preclassify.read_cache(self.home)
        self.assertEqual(list(cached), ["evt-001"])
        self.assertEqual(cached["evt-001"]["topic"], "naming-style")
        self.assertEqual(summary["cached"], 1)

    def test_topic_and_sketch_length_caps(self):
        good = {"id": "evt-001", "is_correction": True, "topic": "One Two Three Four Five Six",
                "rule_sketch": "keep names short", "confidence": 0.7}
        items = [
            good,
            dict(good, id="evt-002", topic="x" * 5000),
            dict(good, id="evt-003", topic="!!! ???"),
            dict(good, id="evt-004", topic=""),
            dict(good, id="evt-005", rule_sketch="y" * 201),
            dict(good, id="evt-006", rule_sketch="z" * 200),
        ]
        env = dict(self.env, FAKE_CLAUDE_MODE="raw", FAKE_CLAUDE_RESULT=json.dumps(items))
        self.run_batches([_prompt(i) for i in range(1, 7)], env=env)
        cached = preclassify.read_cache(self.home)
        self.assertEqual(sorted(cached), ["evt-001", "evt-002", "evt-006"])
        self.assertEqual(cached["evt-001"]["topic"], "one-two-three-four")
        self.assertEqual(cached["evt-002"]["topic"], "x" * 60)
        self.assertEqual(cached["evt-006"]["rule_sketch"], "z" * 200)


class FailureTest(_Base):
    def test_each_failure_mode_skips_batch(self):
        preclassify.TIMEOUT_S = 0.5
        self.addCleanup(setattr, preclassify, "TIMEOUT_S", 120)
        for mode in ("malformed", "timeout", "exit1", "limit", "notarray", "iserror"):
            self.logged = []
            env = dict(self.env, FAKE_CLAUDE_MODE=mode)
            summary = self.run_batches([_prompt(1)], env=env)
            self.assertEqual(summary["cached"], 0, mode)
            self.assertEqual(summary["skipped"], 1, mode)
            self.assertEqual(len(self.logged), 1, mode)
        self.logged = []
        env = dict(self.env, APPRENTICE_CLAUDE_BIN=os.path.join(self.fake_home, "no-such-claude"))
        summary = self.run_batches([_prompt(1)], env=env)
        self.assertEqual((summary["cached"], summary["skipped"], len(self.logged)), (0, 1, 1))
        self.assertEqual(preclassify.read_cache(self.home), {})

    def test_empty_or_non_json_output_hints_at_safe_mode_auth(self):
        hint = ("child failed under --safe-mode; settings-based auth (apiKeyHelper/env) is not loaded in "
                "safe mode")
        for mode, hinted in (("exit1", True), ("malformed", True), ("limit", False), ("iserror", False),
                             ("notarray", False)):
            self.logged = []
            self.run_batches([_prompt(1)], env=dict(self.env, FAKE_CLAUDE_MODE=mode))
            self.assertEqual(len(self.logged), 1, mode)
            self.assertEqual(hint in self.logged[0], hinted, (mode, self.logged[0]))

    def test_lone_surrogate_in_text_does_not_abort(self):
        summary = self.run_batches([_prompt(1, text="no, use helper \ud800 one")])
        self.assertEqual(summary["cached"], 1)
        self.assertIn("helper ? one", self.calls()[0]["stdin"])

    def test_non_utf8_child_stdout_is_decoded_with_replacement(self):
        env = dict(self.env, FAKE_CLAUDE_MODE="badutf8")
        summary = self.run_batches([_prompt(1)], env=env)
        self.assertEqual(summary["cached"], 1)
        self.assertEqual(preclassify.read_cache(self.home)["evt-001"]["rule_sketch"], "prefer the � helper")

    def test_any_child_exception_counts_as_failed_batch(self):
        def boom(*_args, **_kwargs):
            raise RuntimeError("unexpected")
        original = preclassify.subprocess.run
        preclassify.subprocess.run = boom
        self.addCleanup(setattr, preclassify.subprocess, "run", original)
        summary = self.run_batches([_prompt(1)])
        self.assertEqual((summary["cached"], summary["skipped"], len(self.logged)), (0, 1, 1))
        self.assertIn("RuntimeError", self.logged[0])

    def test_limit_envelope_with_exit_zero_still_fails(self):
        env = dict(self.env, FAKE_CLAUDE_MODE="iserror")
        summary = self.run_batches([_prompt(1)], env=env)
        self.assertEqual(summary["cached"], 0)

    def test_three_consecutive_failures_stop(self):
        env = dict(self.env, FAKE_CLAUDE_MODE="exit1")
        summary = self.run_batches([_prompt(i) for i in range(200)], env=env)
        self.assertEqual(summary["batches"], 3)
        self.assertTrue(summary["stopped_early"])
        self.assertEqual(len(self.calls()), 3)


class PayloadTest(_Base):
    def test_payload_masks_email_and_uuid(self):
        email = "alice" + "@" + "mail" + ".test"
        uuid = "123e4567-e89b-42d3-a456-426614174000"
        item = _prompt(1, text="no, send to %s id %s" % (email, uuid),
                       prev_assistant="I mailed %s about %s" % (email, uuid))
        self.run_batches([item])
        stdin = self.calls()[0]["stdin"]
        self.assertNotIn(email, stdin)
        self.assertNotIn(uuid, stdin)
        self.assertIn("<email>", stdin)
        self.assertIn("<uuid>", stdin)

    def test_ask_events_never_sent(self):
        ask = {"id": "ask-001", "kind": "ask", "ts": _ts(1), "profile": "default", "prev_assistant": "x",
               "tags": ["correction"], "text": "ASK-TEXT-MARKER", "questions": []}
        todo = preclassify.select_uncached([ask, _prompt(2)], self.home, "default")
        self.assertEqual([p["event"]["id"] for p in todo], ["evt-002"])
        self.run_batches([ask, _prompt(2)])
        self.assertNotIn("ASK-TEXT-MARKER", self.calls()[0]["stdin"])

    def test_other_profile_and_unknown_never_in_stdin(self):
        self.write(os.path.join(self.home, "config.json"),
                   json.dumps({"account_groups": {"work": ["default", "other"]}, "unknown_group": "work"}))
        items = [_prompt(1, text="mine MINE-MARKER"),
                 _prompt(2, text="theirs OTHER-MARKER", profile="other"),
                 _prompt(3, text="backfilled UNKNOWN-MARKER", profile="unknown")]
        todo = preclassify.select_uncached(items, self.home, "default")
        self.assertEqual([p["event"]["id"] for p in todo], ["evt-001"])
        self.run_batches(items)
        stdin = self.calls()[0]["stdin"]
        self.assertIn("MINE-MARKER", stdin)
        self.assertNotIn("OTHER-MARKER", stdin)
        self.assertNotIn("UNKNOWN-MARKER", stdin)

    def test_filter_precedes_limit(self):
        items = [_prompt(i, profile="other") for i in range(10, 200)]
        items.append(_prompt(1))
        todo = preclassify.select_uncached(items, self.home, "default")
        self.assertEqual([p["event"]["id"] for p in todo], ["evt-001"])

    def test_profile_rechecked_before_batch(self):
        pairs = _pairs([_prompt(1)])
        env = dict(self.env, CLAUDE_CONFIG_DIR=os.path.join(self.fake_home, ".claude-other"))
        summary = preclassify.run_batches(pairs, self.home, env, None, self.log)
        self.assertEqual(self.calls(), [])
        self.assertEqual(summary["skipped"], 1)

    def test_child_env_internal_disable_no_entrypoint(self):
        env = dict(self.env, CLAUDE_CODE_ENTRYPOINT="cli")
        self.run_batches([_prompt(1)], env=env)
        call = self.calls()[0]
        self.assertEqual(call["env"]["APPRENTICE_INTERNAL"], "1")
        self.assertEqual(call["env"]["APPRENTICE_DISABLE"], "1")
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", call["env"])
        self.assertEqual(call["argv"][1:], ["-p", "--model", "haiku", "--output-format", "json",
                                            "--no-session-persistence", "--safe-mode", "--tools", ""])
        self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(os.path.join(self.home, "state")))

    def test_marker_first_line(self):
        self.run_batches([_prompt(1)])
        stdin = self.calls()[0]["stdin"]
        self.assertEqual(stdin.splitlines()[0], preclassify.PROMPT_MARKER)
        self.assertEqual(preclassify.PROMPT_MARKER, "[apprentice-preclassify v1]")


class ScriptTest(_Base):
    def run_script(self, *args, extra=None):
        env = isolated_env()
        env.update(self.env)
        env["APPRENTICE_HOME"] = self.home
        env["HOME"] = self.fake_home
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.update(extra or {})
        return subprocess.run([sys.executable, "-I", "-S", _SCRIPT] + list(args), capture_output=True, text=True,
                              env=env, timeout=60)

    def test_removed_flags_usage_error(self):
        for flag in ("--include-unknown", "--all-profiles"):
            proc = self.run_script(flag)
            self.assertEqual(proc.returncode, 2, flag)
            self.assertIn("removed flag", proc.stderr)
        self.assertEqual(self.calls(), [])

    def test_runs_and_prints_summary(self):
        for i in range(3):
            events.append_event(self.home, _prompt(i))
        proc = self.run_script("--max-batches", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "preclassify: 1 batches, 3 cached, 0 skipped")
        self.assertEqual(len(preclassify.read_cache(self.home)), 3)

    def test_since_filters_months(self):
        events.append_event(self.home, _prompt(1, ts="2029-12-05T10:00:00.000Z"))
        events.append_event(self.home, _prompt(2))
        proc = self.run_script("--since", "2030-01")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(list(preclassify.read_cache(self.home)), ["evt-002"])


class FakeClaudeTest(_Base):
    def test_limit_mode_envelope_exit_1(self):
        env = dict(os.environ, FAKE_CLAUDE_MODE="limit")
        proc = subprocess.run([_FAKE, "-p"], input="x", capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 1)
        envelope = json.loads(proc.stdout)
        self.assertIs(envelope["is_error"], True)
        self.assertEqual(envelope["api_error_status"], 429)
        self.assertEqual(envelope["subtype"], "success")
