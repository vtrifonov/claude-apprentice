import json
import os
import shutil

from apprentice import config, events
from tests.base import REPO_ROOT, ApprenticeTestCase
from tests.hookrun import payload_bytes, run_hook

_TRANSCRIPTS = os.path.join(REPO_ROOT, "tests", "fixtures", "transcripts")
_PLAIN = os.path.join(_TRANSCRIPTS, "plain.jsonl")
_CLI = {"CLAUDE_CODE_ENTRYPOINT": "cli"}


class CaptureEndToEndTest(ApprenticeTestCase):
    def p(self, *parts):
        return os.path.join(self.home, *parts)

    def records(self):
        return list(events.iter_events(self.home))

    def log_lines(self):
        try:
            with open(self.p("hook-errors.log"), "r", encoding="utf-8") as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def run_silent(self, event, stdin, env_extra=None, **kwargs):
        code, out, err = run_hook(event, stdin, _CLI if env_extra is None else env_extra, **kwargs)
        self.assertEqual(code, 0)
        self.assertEqual(out, b"")
        self.assertEqual(err, b"")

    def test_pre_tool_writes_marker(self):
        self.run_silent("pre-tool", payload_bytes("pre_tool", _PLAIN, tool_use_id="tu1"))
        with open(os.path.join(self.p("state", "pending-ask"), "sess-1.json"), "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["tool_use_id"], "tu1")
        self.assertEqual(self.records(), [])

    def test_post_tool_writes_ask(self):
        self.run_silent("post-tool", payload_bytes("post_tool_dict", _PLAIN, tool_use_id="tu1"))
        (rec,) = self.records()
        self.assertEqual(rec["kind"], "ask")
        self.assertEqual(rec["questions"][0]["answer"]["labels"], ["Quick (Recommended)"])

    def test_user_prompt_writes_prompt(self):
        self.run_silent("user-prompt", payload_bytes("user_prompt", _PLAIN))
        (rec,) = self.records()
        self.assertEqual(rec["kind"], "prompt")
        self.assertEqual(rec["text"], "Please add a widget cache")

    def test_session_start_creates_month_file_and_state(self):
        self.run_silent("session-start", payload_bytes("session_start", _PLAIN))
        self.assertTrue(os.path.isfile(self.p("events", config.utc_now_iso()[:7] + ".jsonl")))
        self.assertTrue(os.path.isfile(self.p("state", "last-sessionstart.json")))

    def test_garbage_stdin_silent(self):
        self.run_silent("user-prompt", b"\x00{")
        self.assertEqual(self.records(), [])
        (line,) = self.log_lines()
        self.assertIn(" user-prompt ", line)

    def test_empty_stdin_silent(self):
        for event in ("pre-tool", "post-tool", "user-prompt", "session-start"):
            self.run_silent(event, b"")
        self.assertEqual(self.records(), [])
        self.assertEqual(len(self.log_lines()), 4)

    def test_unwritable_home_silent(self):
        blocker = self.write(os.path.join(self.fake_home, "blocker"), "x")
        for event, name in (("pre-tool", "pre_tool"), ("post-tool", "post_tool_dict"),
                            ("user-prompt", "user_prompt"), ("session-start", "session_start")):
            self.run_silent(event, payload_bytes(name, _PLAIN), home=blocker)
        self.assertTrue(os.path.isfile(blocker))

    def test_disable_env_and_file(self):
        stdin = payload_bytes("user_prompt", _PLAIN)
        self.run_silent("user-prompt", stdin, dict(_CLI, APPRENTICE_DISABLE="1"))
        self.assertEqual(self.records(), [])
        self.assertFalse(os.path.exists(self.p("events")))
        self.write(self.p("disabled"), "")
        self.run_silent("user-prompt", stdin)
        self.assertEqual(self.records(), [])
        os.remove(self.p("disabled"))
        self.run_silent("user-prompt", stdin)
        self.assertEqual(len(self.records()), 1)

    def test_internal_one_mb_stdin(self):
        big = b"x" * 1048576
        for event in ("pre-tool", "post-tool", "user-prompt", "session-start"):
            self.run_silent(event, big, dict(_CLI, APPRENTICE_INTERNAL="1"))
        self.assertFalse(os.path.exists(self.p("events")))
        self.assertEqual(os.listdir(self.home), [])

    def test_missing_interpreter_sessionstart_logs_once(self):
        env = dict(_CLI, APPRENTICE_PYTHON="/nonexistent/python3")
        code, out, _ = run_hook("session-start", payload_bytes("session_start", _PLAIN), env)
        self.assertEqual((code, out), (0, b""))
        (line,) = self.log_lines()
        self.assertRegex(line, r"^\S+ session-start interpreter-missing: exit \d+$")
        before = self.log_lines()
        for event, name in (("pre-tool", "pre_tool"), ("post-tool", "post_tool_dict"),
                            ("user-prompt", "user_prompt")):
            code, out, _ = run_hook(event, payload_bytes(name, _PLAIN), env)
            self.assertEqual((code, out), (0, b""), event)
        self.assertEqual(self.log_lines(), before)
        self.assertEqual(sorted(os.listdir(self.home)), ["hook-errors.log"])

    def test_broken_package_module_silent(self):
        root = os.path.join(self.fake_home, "broken_root")
        ignore = shutil.ignore_patterns("__pycache__")
        shutil.copytree(os.path.join(REPO_ROOT, "apprentice"), os.path.join(root, "apprentice"), ignore=ignore)
        shutil.copytree(os.path.join(REPO_ROOT, "hooks"), os.path.join(root, "hooks"), ignore=ignore)
        with open(os.path.join(root, "apprentice", "filters.py"), "a", encoding="utf-8") as fh:
            fh.write("\ndef broken(:\n")
        self.run_silent("user-prompt", payload_bytes("user_prompt", _PLAIN), root=root)
        self.assertEqual(self.records(), [])
        (line,) = self.log_lines()
        self.assertIn("SyntaxError", line)

    def test_sessionstart_tone_line_for_clear_and_compact(self):
        config.ensure_dirs(self.home)
        self.write(self.p("state", "group-labels.json"), json.dumps({"default": "group-1"}))
        tone = self.write(self.p("profile", "tone-group-1.md"), "tone\n")
        expected = ("apprentice tone file for this session (account group group-1): %s\n" % tone).encode("utf-8")
        for source in ("clear", "compact"):
            code, out, err = run_hook("session-start", payload_bytes("session_start", _PLAIN, source=source), _CLI)
            self.assertEqual((code, out, err), (0, expected, b""), source)

    def test_sdk_cli_payload_writes_nothing(self):
        sdk = os.path.join(_TRANSCRIPTS, "entry_sdk_cli.jsonl")
        self.run_silent("user-prompt", payload_bytes("user_prompt", sdk), {})
        self.run_silent("post-tool", payload_bytes("post_tool_dict", sdk, tool_use_id="tu1"), {})
        self.run_silent("pre-tool", payload_bytes("pre_tool", sdk, tool_use_id="tu1"),
                        {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})
        self.assertEqual(self.records(), [])
        self.assertFalse(os.path.exists(self.p("state", "pending-ask", "sess-1.json")))

    def test_profile_env_recorded(self):
        env = dict(_CLI, CLAUDE_CONFIG_DIR=os.path.join(self.fake_home, ".claude-work"))
        self.run_silent("user-prompt", payload_bytes("user_prompt", _PLAIN), env)
        (rec,) = self.records()
        self.assertEqual(rec["profile"], ".claude-work")
