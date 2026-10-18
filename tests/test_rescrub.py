import json
import os
import stat
import subprocess

from apprentice import config, events, filters, rescrub
from tests.base import REPO_ROOT, ApprenticeTestCase, isolated_env

_AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
_CUSTOM = "Globex" + "Corp"
_TS = "2030-01-05T10:00:00.000Z"


def _prompt(text, length=None, tags=None, prev=None, flags=None):
    record = events.envelope("prompt", "hook", "s1", "default", "cli", None, None, None, None, None, ts=_TS,
                             flags=flags)
    record.update({"text": text, "sha1": "0" * 40, "len": len(text) if length is None else length,
                   "words": 1, "tags": tags or [], "register": "direction", "strong_marker": False,
                   "after_interrupt": False, "after_rejected_ask": False, "prev_assistant": prev,
                   "duplicate_of": None})
    return record


class RescrubTest(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        config.ensure_dirs(self.home)
        self.path = os.path.join(self.home, "events", "2030-01.jsonl")

    def seed(self, *records):
        for record in records:
            events.append(self.path, record)
        with open(self.path, "rb") as fh:
            return fh.read()

    def stored(self):
        return list(events.iter_events(self.home))

    def test_dry_run_counts_only(self):
        before = self.seed(_prompt("key " + _AWS + " here"))
        counts = rescrub.run(self.home, config.load_config(self.home), apply=False)
        self.assertEqual(counts["events_changed"], 1)
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_apply_rescrubs_free_text_and_adds_flags(self):
        self.seed(_prompt("key " + _AWS + " here", prev="earlier " + _AWS),
                  _prompt("nothing to see"))
        counts = rescrub.run(self.home, config.load_config(self.home), apply=True)
        self.assertEqual(counts["events_changed"], 1)
        first, second = self.stored()
        self.assertEqual(first["text"], "key <redacted:aws-key> here")
        self.assertNotIn(_AWS, first["prev_assistant"])
        self.assertIn("redacted:aws-key", first["flags"])
        self.assertEqual(second["text"], "nothing to see")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_cut_fragment_at_the_boundary_is_fully_redacted(self):
        pasted = "v " * 145 + _AWS[:10]
        typed_short = "see " + _AWS[:8]
        self.seed(_prompt(pasted, length=2000, tags=["pasted"]),
                  _prompt(typed_short),
                  _prompt("x " * 140 + "4111 1111 1111", length=1800, tags=["pasted"]),
                  _prompt("y " * 120 + "gh" + "p_A1b2", length=1700, tags=["pasted"]))
        counts = rescrub.run(self.home, config.load_config(self.home), apply=True)
        cut, short, digits, token = self.stored()
        self.assertEqual(counts["cut_redacted"], 3)
        for rec in (cut, digits, token):
            self.assertEqual(rec["text"], rescrub.CUT_PLACEHOLDER)
            self.assertIn("redacted:cut-fragment", rec["flags"])
        # not cut: the text ends where the prompt ended, so nothing was lost to a cut
        self.assertEqual(short["text"], typed_short)

    def test_custom_pattern_prefix_at_the_boundary(self):
        self.write(os.path.join(self.home, "config.json"), json.dumps({"extra_redact_patterns": [_CUSTOM]}))
        self.seed(_prompt("q " * 147 + "Globe", length=1900, tags=["pasted"]),
                  _prompt("q " * 147 + "plain", length=1900, tags=["pasted"]))
        rescrub.run(self.home, config.load_config(self.home), apply=True)
        hit, miss = self.stored()
        self.assertEqual(hit["text"], rescrub.CUT_PLACEHOLDER)
        self.assertTrue(miss["text"].endswith("plain"))

    def test_typed_prompt_over_text_cap_counts_as_cut(self):
        text = "w " * (filters.TEXT_CAP // 2 - 5) + _AWS[:9]
        self.seed(_prompt(text, length=filters.TEXT_CAP + 50))
        rescrub.run(self.home, config.load_config(self.home), apply=True)
        self.assertEqual(self.stored()[0]["text"], rescrub.CUT_PLACEHOLDER)

    def test_ask_fields_rescrubbed(self):
        record = events.envelope("ask", "hook", "s1", "default", "cli", None, None, None, None, None, ts=_TS)
        record.update({"tool_use_id": "tu1", "questions": [
            {"header": "h " + _AWS, "question": "Use " + _AWS + "?", "multi_select": False,
             "options": [{"label": _AWS, "description": None, "recommended": False}],
             "answer": {"labels": [_AWS], "free_text": None, "is_other": False, "followed_recommended": None}}],
            "raw_response": "answer " + _AWS})
        self.seed(record)
        rescrub.run(self.home, config.load_config(self.home), apply=True)
        self.assertNotIn(_AWS, json.dumps(self.stored()[0]))

    def test_unchanged_lines_keep_their_bytes(self):
        good = _prompt("nothing to see")
        self.seed(good, _prompt("key " + _AWS))
        rescrub.run(self.home, config.load_config(self.home), apply=True)
        with open(self.path, "rb") as fh:
            first_line = fh.read().split(b"\n")[0] + b"\n"
        self.assertEqual(first_line, events.serialize(good))

    def test_cli_dry_run_then_apply(self):
        self.seed(_prompt("key " + _AWS))
        env = isolated_env()
        env["APPRENTICE_HOME"] = self.home
        script = os.path.join(REPO_ROOT, "scripts", "rescrub.py")
        dry = subprocess.run(["/usr/bin/python3", "-I", "-S", script], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(b"dry run", dry.stdout)
        self.assertIn(_AWS, json.dumps(self.stored()))
        real = subprocess.run(["/usr/bin/python3", "-I", "-S", script, "--apply"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(real.returncode, 0, real.stderr)
        self.assertNotIn(_AWS, json.dumps(self.stored()))
        self.assertNotIn(_AWS.encode(), dry.stdout + real.stdout)
