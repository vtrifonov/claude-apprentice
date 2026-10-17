import datetime
import json
import os
import stat
import subprocess

from apprentice import capture, config, events, filters
from tests.base import REPO_ROOT, ApprenticeTestCase

_FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")
_RELAY = '<teammate-message teammate_id="bob" summary="status">Build is green</teammate-message>'


def _transcript(name):
    return os.path.join(_FIXTURES, "transcripts", name)


def _iso(delta_seconds=0.0):
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delta_seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (when.microsecond // 1000)


class CaptureTest(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.env = {"APPRENTICE_HOME": self.home, "CLAUDE_CODE_ENTRYPOINT": "cli"}

    def payload(self, name, transcript="plain.jsonl", **overrides):
        with open(os.path.join(_FIXTURES, "hook-payloads", name + ".json"), "r", encoding="utf-8") as fh:
            text = fh.read()
        data = json.loads(text.replace("__TRANSCRIPT__", _transcript(transcript)))
        data.update(overrides)
        return data

    def run_hook(self, event, payload, env=None):
        return capture.handle(event, payload, self.env if env is None else env)

    def records(self):
        return list(events.iter_events(self.home))

    def marker_path(self, session_id="sess-1"):
        return capture.pending_path(self.home, session_id)

    def read_marker(self, session_id="sess-1"):
        with open(self.marker_path(session_id), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def write_marker(self, tool_use_id, age_seconds, session_id="sess-1"):
        config.ensure_dirs(self.home)
        questions = self.payload("pre_tool")["tool_input"]["questions"]
        marker = {"tool_use_id": tool_use_id, "ts": _iso(-age_seconds), "questions": questions}
        with open(self.marker_path(session_id), "w", encoding="utf-8") as fh:
            json.dump(marker, fh)

    def seed_event(self, obj):
        config.ensure_dirs(self.home)
        events.append(events.events_path(self.home, config.utc_now_iso()), obj)

    def seed_prompt(self, text, session_id, age_seconds):
        env = events.envelope("prompt", "hook", session_id, "default", "cli", None, None, None, None, None,
                              ts=_iso(-age_seconds))
        env.update({"text": text, "sha1": filters.normalized_sha1(text)})
        self.seed_event(env)
        return env["id"]

    def assert_nothing_written(self):
        self.assertEqual(self.records(), [])
        self.assertFalse(os.path.exists(self.marker_path()))

    # pre-tool

    def test_pre_tool_writes_marker_atomically_and_overwrites(self):
        self.assertIsNone(self.run_hook("pre-tool", self.payload("pre_tool", tool_use_id="tu1")))
        first = self.read_marker()
        self.assertEqual(first["tool_use_id"], "tu1")
        self.assertIsNotNone(config.parse_iso(first["ts"]))
        self.assertEqual(first["questions"][0]["header"], "Mode")
        self.run_hook("pre-tool", self.payload("pre_tool", tool_use_id="tu2"))
        self.assertEqual(self.read_marker()["tool_use_id"], "tu2")
        self.assertEqual(stat.S_IMODE(os.stat(self.marker_path()).st_mode), 0o600)
        self.assertEqual(os.listdir(os.path.dirname(self.marker_path())), ["sess-1.json"])
        self.assertEqual(self.records(), [])

    def test_marker_cap_cuts_labels_and_headers(self):
        questions = [{"question": "Q%d?" % i, "header": "H" * 3000, "multiSelect": False,
                      "options": [{"label": "L" * 3000, "description": "d"} for _ in range(4)]} for i in range(4)]
        self.run_hook("pre-tool", self.payload("pre_tool", tool_use_id="tu1", tool_input={"questions": questions}))
        self.assertLessEqual(os.path.getsize(self.marker_path()), capture.MARKER_CAP)
        marker = self.read_marker()
        self.assertEqual(marker["tool_use_id"], "tu1")
        self.assertNotIn("truncated", marker)
        self.assertEqual(len(marker["questions"]), 4)
        self.assertEqual(len(marker["questions"][0]["header"]), 200)
        self.assertEqual(len(marker["questions"][0]["options"][0]["label"]), 200)

    def test_marker_cap_minimal_fallback(self):
        for count in (60, 2000):
            questions = [{"question": "Q" * 3000, "header": "H" * 3000, "multiSelect": True,
                          "options": [{"label": "L" * 3000} for _ in range(10)]} for _ in range(count)]
            marker = {"tool_use_id": "tu1", "ts": _iso(), "questions": questions}
            data = capture._capped_marker(marker)
            self.assertLessEqual(len(data), capture.MARKER_CAP, count)
            small = json.loads(data.decode("utf-8"))
            self.assertIs(small["truncated"], True)
            self.assertEqual(small["tool_use_id"], "tu1")
            self.assertEqual(small["ts"], marker["ts"])
            for q in small["questions"]:
                self.assertLessEqual(len(q["header"]), 100)
                self.assertTrue(all(len(o["label"]) <= 100 for o in q["options"]))

    # post-tool

    def test_post_tool_ask_event_and_marker_deleted_on_match(self):
        for name in ("post_tool_blocks", "post_tool_dict", "post_tool_string"):
            self.run_hook("pre-tool", self.payload("pre_tool", tool_use_id="tu-" + name))
            self.assertIsNone(self.run_hook("post-tool", self.payload(name, "entry_cli.jsonl",
                                                                      tool_use_id="tu-" + name)))
            self.assertFalse(os.path.exists(self.marker_path()), name)
        recs = self.records()
        self.assertEqual([r["kind"] for r in recs], ["ask"] * 3)
        for rec in recs:
            self.assertEqual(rec["session_id"], "sess-1")
            self.assertEqual(rec["cwd"], "/home/alice/code/acme/widgets")
            self.assertEqual(rec["ticket"], "XX-123")
            self.assertEqual(rec["entrypoint"], "cli")
            self.assertEqual(rec["questions"][0]["answer"]["labels"], ["Quick (Recommended)"])
            self.assertEqual(rec["questions"][0]["answer"]["labels"][0], rec["questions"][0]["options"][0]["label"])
            self.assertNotIn("partial", rec)

    def test_post_tool_leaves_marker_of_later_picker(self):
        self.run_hook("pre-tool", self.payload("pre_tool", tool_use_id="tu2"))
        self.run_hook("post-tool", self.payload("post_tool_dict", tool_use_id="tu1"))
        self.assertEqual(self.read_marker()["tool_use_id"], "tu2")
        self.assertEqual([r["tool_use_id"] for r in self.records()], ["tu1"])

    def test_post_tool_unknown_shape_partial_raw_kept(self):
        self.run_hook("post-tool", self.payload("post_tool_unknown", tool_use_id="tu1"))
        (rec,) = self.records()
        self.assertIs(rec["partial"], True)
        self.assertEqual(rec["raw_response"], str(42))
        self.assertNotIn("partial", rec.get("flags", []))
        self.assertIsNone(rec["questions"][0]["answer"])

    # user-prompt

    def test_user_prompt_plain_event_fields(self):
        self.assertIsNone(self.run_hook("user-prompt", self.payload("user_prompt")))
        (rec,) = self.records()
        self.assertEqual(rec["kind"], "prompt")
        self.assertEqual(rec["source"], "hook")
        self.assertEqual(rec["text"], "Please add a widget cache")
        self.assertEqual(rec["sha1"], filters.classify("Please add a widget cache")["sha1"])
        self.assertEqual(rec["len"], len("Please add a widget cache"))
        self.assertEqual(rec["words"], 5)
        self.assertEqual(rec["register"], "direction")
        self.assertIs(rec["strong_marker"], False)
        self.assertIs(rec["after_interrupt"], False)
        self.assertIs(rec["after_rejected_ask"], False)
        self.assertEqual(rec["prev_assistant"], "Added the cache.")
        self.assertIsNone(rec["duplicate_of"])
        self.assertEqual(rec["tags"], [])
        self.assertEqual(rec["profile"], "default")
        self.assertEqual(rec["entrypoint"], "cli")
        self.assertNotIn("flags", rec)

    def test_user_prompt_machine(self):
        self.run_hook("user-prompt", self.payload("user_prompt", prompt="/apprentice:status now"))
        (rec,) = self.records()
        self.assertEqual(rec["kind"], "machine")
        self.assertEqual(rec["command"], "/apprentice:status")
        self.assertNotIn("text", rec)

    def test_user_prompt_dropped_writes_nothing(self):
        self.run_hook("user-prompt", self.payload("user_prompt", "relay.jsonl", prompt=_RELAY))
        self.assert_nothing_written()

    def test_user_prompt_rejected_ask_after_1s(self):
        self.write_marker("tu1", 2)
        self.run_hook("user-prompt", self.payload("user_prompt", "ask_rejected.jsonl", prompt="No, do it by hand"))
        rejected, prompt = self.records()
        self.assertEqual(rejected["kind"], "ask_rejected")
        self.assertEqual(rejected["tool_use_id"], "tu1")
        self.assertEqual(rejected["ticket"], "XX-123")
        self.assertIsNone(rejected["questions"][0]["answer"])
        self.assertNotIn("partial", rejected)
        self.assertEqual(prompt["kind"], "prompt")
        self.assertIs(prompt["after_rejected_ask"], True)
        self.assertFalse(os.path.exists(self.marker_path()))

    def test_user_prompt_marker_too_young_no_rejection(self):
        self.write_marker("tu1", 0)
        self.run_hook("user-prompt", self.payload("user_prompt"))
        (rec,) = self.records()
        self.assertIs(rec["after_rejected_ask"], False)
        self.assertTrue(os.path.exists(self.marker_path()))

    def test_user_prompt_late_posttool_race(self):
        self.run_hook("post-tool", self.payload("post_tool_dict", tool_use_id="tu1"))
        self.write_marker("tu1", 5)
        self.run_hook("user-prompt", self.payload("user_prompt"))
        kinds = [r["kind"] for r in self.records()]
        self.assertEqual(kinds, ["ask", "prompt"])
        self.assertIs(self.records()[1]["after_rejected_ask"], False)
        self.assertFalse(os.path.exists(self.marker_path()))

    def test_duplicate_of_cross_session_120s(self):
        text = "Please add a widget cache"
        near = self.seed_prompt("please  add a WIDGET cache", "sess-2", 30)
        self.run_hook("user-prompt", self.payload("user_prompt", prompt=text))
        self.assertEqual(self.records()[-1]["duplicate_of"], near)

    def test_duplicate_of_cut_text_requires_raw_sha1(self):
        head = "Traceback (most recent call last):\n" + "".join(
            '  File "/srv/acme/widgets/mod%02d.py", line %d, in run\n' % (i, i) for i in range(12))
        self.assertGreater(len(head), 300)
        first = head + "ValueError: bad widget size\n"
        second = head + "KeyError: missing cache entry\n"
        self.run_hook("user-prompt", self.payload("user_prompt", prompt=first, session_id="sess-2"))
        self.run_hook("user-prompt", self.payload("user_prompt", prompt=second))
        earlier, later = self.records()
        self.assertIn("pasted", later["tags"])
        self.assertEqual(earlier["text"], later["text"])
        self.assertIsNone(later["duplicate_of"])
        self.run_hook("user-prompt", self.payload("user_prompt", prompt=first, session_id="sess-3"))
        self.assertEqual(self.records()[-1]["duplicate_of"], earlier["id"])

    def test_duplicate_of_too_old_is_none(self):
        self.seed_prompt("Please add a widget cache", "sess-2", 200)
        self.run_hook("user-prompt", self.payload("user_prompt"))
        self.assertIsNone(self.records()[-1]["duplicate_of"])

    def test_duplicate_of_same_session_is_none(self):
        self.seed_prompt("Please add a widget cache", "sess-1", 30)
        self.run_hook("user-prompt", self.payload("user_prompt"))
        self.assertIsNone(self.records()[-1]["duplicate_of"])

    def test_prev_assistant_and_after_interrupt_from_fixture(self):
        self.run_hook("user-prompt", self.payload("user_prompt", "interrupt.jsonl",
                                                  prompt="Continue with the tests instead"))
        (rec,) = self.records()
        self.assertIs(rec["after_interrupt"], True)
        self.assertEqual(rec["prev_assistant"], "Starting the refactor.")

    def test_prev_assistant_uses_user_scrub_config(self):
        self.write(os.path.join(self.home, "config.json"), json.dumps({"extra_redact_patterns": [r"ZZ\d{4}"]}))
        lines = [{"type": "assistant", "message": {"role": "assistant",
                                                   "content": [{"type": "text", "text": "ticket ZZ1234 is done"}]}},
                 {"type": "user", "message": {"role": "user", "content": "Please add a widget cache"}}]
        path = self.write(os.path.join(self.fake_home, "t", "cfg.jsonl"),
                          "".join(json.dumps(o) + "\n" for o in lines))
        self.run_hook("user-prompt", self.payload("user_prompt", transcript_path=path))
        (rec,) = self.records()
        self.assertIsNotNone(rec["prev_assistant"])
        self.assertNotIn("ZZ1234", rec["prev_assistant"])

    # entrypoints and capture gating

    def test_entrypoint_order_payload_env_tail(self):
        env = {"APPRENTICE_HOME": self.home, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}
        self.run_hook("user-prompt", self.payload("user_prompt", "entry_sdk_cli.jsonl", entrypoint="cli"), env)
        self.assertEqual(self.records()[-1]["entrypoint"], "cli")
        self.run_hook("user-prompt", self.payload("user_prompt", "entry_sdk_cli.jsonl"),
                      {"APPRENTICE_HOME": self.home, "CLAUDE_CODE_ENTRYPOINT": "claude-desktop"})
        self.assertEqual(self.records()[-1]["entrypoint"], "claude-desktop")
        self.run_hook("user-prompt", self.payload("user_prompt", "entry_desktop.jsonl"),
                      {"APPRENTICE_HOME": self.home})
        self.assertEqual(self.records()[-1]["entrypoint"], "claude-desktop")
        self.assertEqual(len(self.records()), 3)

    def test_resolve_entrypoint_pre_tool_never_reads_tail(self):
        payload = self.payload("pre_tool", "entry_cli.jsonl")
        self.assertIsNone(capture.resolve_entrypoint(payload, {}, payload["transcript_path"], False))
        self.assertEqual(capture.resolve_entrypoint(payload, {}, payload["transcript_path"], True), "cli")

    def test_sdk_cli_writes_nothing(self):
        env = {"APPRENTICE_HOME": self.home}
        self.run_hook("user-prompt", self.payload("user_prompt", "entry_sdk_cli.jsonl"), env)
        self.run_hook("pre-tool", self.payload("pre_tool", tool_use_id="tu1", entrypoint="sdk-ts"), env)
        self.run_hook("post-tool", self.payload("post_tool_dict", "entry_sdk_ts.jsonl", tool_use_id="tu1"), env)
        self.assert_nothing_written()

    def test_observer_path_writes_nothing(self):
        observer = os.path.join("observer", "-home-alice-code-acme-widgets-claude-mem-observer", "s.jsonl")
        self.run_hook("user-prompt", self.payload("user_prompt", observer))
        self.run_hook("pre-tool", self.payload("pre_tool", observer, tool_use_id="tu1"))
        self.run_hook("user-prompt", self.payload("user_prompt", cwd="/home/alice/claude-mem-observer"))
        self.assert_nothing_written()

    def test_unknown_entrypoint_flagged(self):
        self.run_hook("user-prompt", self.payload("user_prompt", "entry_none.jsonl"), {"APPRENTICE_HOME": self.home})
        (rec,) = self.records()
        self.assertIsNone(rec["entrypoint"])
        self.assertEqual(rec["flags"], ["entrypoint_unknown"])

    def test_should_capture_table(self):
        cfg = dict(config.DEFAULTS)
        payload = {"cwd": "/home/alice/code/acme/widgets", "transcript_path": "/t/s.jsonl"}
        self.assertEqual(capture.should_capture("cli", payload, cfg), (True, []))
        self.assertEqual(capture.should_capture("claude-desktop", payload, cfg), (True, []))
        self.assertEqual(capture.should_capture("sdk-ts", payload, cfg), (False, []))
        self.assertEqual(capture.should_capture("other", payload, cfg), (False, []))
        self.assertEqual(capture.should_capture(None, payload, cfg), (True, ["entrypoint_unknown"]))

    def test_profile_from_config_dir(self):
        env = dict(self.env, CLAUDE_CONFIG_DIR=os.path.join(self.fake_home, ".claude-work"))
        self.run_hook("user-prompt", self.payload("user_prompt"), env)
        (rec,) = self.records()
        self.assertEqual(rec["profile"], ".claude-work")

    def test_email_flag_in_envelope(self):
        prompt = "mail " + "alice" + "@" + "example.test" + " the report"
        self.run_hook("user-prompt", self.payload("user_prompt", prompt=prompt))
        (rec,) = self.records()
        self.assertEqual(rec["flags"], ["email", "foreign_email"])

    def test_session_start_returns_none(self):
        self.assertIsNone(self.run_hook("session-start", self.payload("session_start")))
        self.assertTrue(os.path.isdir(os.path.join(self.home, "events")))

    def test_wrongly_typed_payload_fields_treated_as_missing(self):
        payload = {"session_id": 5, "cwd": 7, "transcript_path": ["a"], "prompt": "Please add a widget cache"}
        self.assertIsNone(self.run_hook("user-prompt", payload))
        (rec,) = self.records()
        self.assertEqual(rec["kind"], "prompt")
        self.assertEqual(rec["session_id"], "")
        self.assertIsNone(rec["cwd"])
        self.assertIsNone(rec["transcript_path"])
        self.assertIsNone(rec["prev_assistant"])
        post = self.payload("post_tool_dict", tool_use_id="tu1", session_id=5, cwd=7, transcript_path=["a"])
        self.assertIsNone(self.run_hook("post-tool", post, {"APPRENTICE_HOME": self.home}))
        self.assertEqual([r["kind"] for r in self.records()], ["prompt", "ask"])
        self.assertIsNone(self.records()[-1]["transcript_path"])
        self.assertFalse(os.path.exists(os.path.join(self.home, "hook-errors.log")))

    def test_unknown_event_returns_none(self):
        self.assertIsNone(self.run_hook("bogus", {}))


class HookFrameTest(ApprenticeTestCase):
    def run_frame(self, event, stdin, **extra_env):
        env = dict(os.environ, CLAUDE_CODE_ENTRYPOINT="cli", **extra_env)
        cmd = ["/usr/bin/python3", "-I", "-S", os.path.join(REPO_ROOT, "hooks", "capture.py"), event]
        return subprocess.run(cmd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    def prompt_bytes(self):
        payload = {"session_id": "sess-1", "transcript_path": _transcript("plain.jsonl"),
                   "cwd": "/home/alice/code/acme/widgets", "prompt": "Please add a widget cache"}
        return json.dumps(payload).encode("utf-8")

    def error_lines(self):
        try:
            with open(os.path.join(self.home, "hook-errors.log"), "r", encoding="utf-8") as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def assert_silent(self, result):
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_frame_captures_prompt_silently(self):
        result = self.run_frame("user-prompt", self.prompt_bytes())
        self.assert_silent(result)
        self.assertEqual([r["kind"] for r in events.iter_events(self.home)], ["prompt"])
        self.assertEqual(self.error_lines(), [])

    def test_frame_bad_stdin_logs_one_line(self):
        for stdin, reason in ((b"", "empty"), (b"[1]", "not a JSON object"), (b"{nope", "not a JSON object"),
                              (b" " * (1048576 + 10), "over 1 MB")):
            before = len(self.error_lines())
            self.assert_silent(self.run_frame("user-prompt", stdin))
            lines = self.error_lines()
            self.assertEqual(len(lines), before + 1, reason)
            self.assertIn("bad-stdin: " + reason, lines[-1])
        self.assertEqual(list(events.iter_events(self.home)), [])

    def test_frame_disabled_writes_nothing(self):
        self.assert_silent(self.run_frame("user-prompt", self.prompt_bytes(), APPRENTICE_DISABLE="1"))
        self.write(os.path.join(self.home, "disabled"), "")
        self.assert_silent(self.run_frame("user-prompt", self.prompt_bytes()))
        self.assertEqual(list(events.iter_events(self.home)), [])
        self.assertEqual(self.error_lines(), [])

    def test_frame_internal_exits_without_writing(self):
        self.assert_silent(self.run_frame("user-prompt", self.prompt_bytes(), APPRENTICE_INTERNAL="1"))
        self.assertEqual(os.listdir(self.home), [])
