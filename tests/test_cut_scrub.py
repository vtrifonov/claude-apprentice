"""Scrub first, cut second: a secret straddling a stored-text cap never survives as a fragment."""
import json
import os

from apprentice import asks, backfill, capture, config, events, filters, scrub, transcript
from tests.base import ApprenticeTestCase

_PAN = "4111" + "1" * 12  # Luhn-valid test card
_TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
_AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
_CUSTOM = "Globex" + "Corp"


def _cases():
    """Pasted prompts (over 1,500 characters, so cut at 300) with the secret across character 300."""
    return {
        "github": ("x " * 146 + _TOKEN + " " + "x " * 800, ["A1b2", "ghp_"]),
        "pan": ("y " * 144 + _PAN + " " + "y " * 800, ["41111111"]),
        "aws": ("v " * 145 + _AWS + " " + "v " * 800, ["ABCDEF", "AKIA"]),
        "custom": ("q " * 147 + _CUSTOM + " " + "q " * 800, ["Globe"]),
        "wrapped": ("see this <pasted_content id=1>" + "z" * 280 + " " + _PAN + "</pasted_content>", ["4111"]),
    }


class ScrubCutUnitTest(ApprenticeTestCase):
    def test_cut_safe_never_splits_a_placeholder(self):
        text = "a" * 10 + "<redacted:github-token>" + "b" * 10
        for cap in range(11, 33):
            self.assertEqual(scrub.cut_safe(text, cap), "a" * 10, cap)
        self.assertEqual(scrub.cut_safe(text, 33), text[:33])
        self.assertEqual(scrub.cut_safe("x <email> y", 5), "x ")
        self.assertEqual(scrub.cut_safe("short", 300), "short")

    def test_tail_safe_never_splits_a_placeholder(self):
        text = "a" * 10 + "<redacted:pan>" + "b" * 10
        for cap in range(11, 24):
            self.assertEqual(scrub.tail_safe(text, cap), "b" * 10, cap)
        self.assertEqual(scrub.tail_safe(text, 10), "b" * 10)
        self.assertEqual(scrub.tail_safe(text, 24), text[-24:])

    def test_scrub_cut_flags_describe_the_full_text(self):
        text = "x " * 146 + _TOKEN + " tail"
        out, flags = scrub.scrub_cut(text, 300, {})
        self.assertIn("redacted:github-token", flags)
        self.assertNotIn("A1b2", out)
        self.assertFalse(out.endswith("<redac"))


class CaptureCutTest(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.write(os.path.join(self.home, "config.json"), json.dumps({"extra_redact_patterns": [_CUSTOM]}))
        self.env = {"APPRENTICE_HOME": self.home, "CLAUDE_CODE_ENTRYPOINT": "cli"}

    def test_live_prompt_scrubbed_before_cut(self):
        cases = _cases()
        for i, (name, (prompt, _)) in enumerate(sorted(cases.items())):
            capture.handle("user-prompt", {"session_id": "s%d" % i, "transcript_path": "", "cwd": self.fake_home,
                                           "prompt": prompt}, self.env)
        records = [e for e in events.iter_events(self.home) if e["kind"] == "prompt"]
        self.assertEqual(len(records), len(cases))
        for rec, (name, (_prompt, fragments)) in zip(records, sorted(cases.items())):
            self.assertLessEqual(len(rec["text"]), filters.PASTED_CAP, name)
            for fragment in fragments:
                self.assertNotIn(fragment, rec["text"], name)
            self.assertTrue(any(f.startswith("redacted:") for f in rec.get("flags") or []), name)
            self.assertIsNone(_split_token(rec["text"]), name)


class BackfillCutTest(ApprenticeTestCase):
    def test_backfill_prompt_scrubbed_before_cut(self):
        cfg = dict(config.load_config(self.home), extra_redact_patterns=[_CUSTOM])
        cases = _cases()
        lines = []
        for i, (_name, (prompt, _)) in enumerate(sorted(cases.items())):
            lines.append(json.dumps({"type": "user", "timestamp": "2030-01-05T10:00:%02dZ" % i, "sessionId": "s1",
                                     "cwd": "/home/alice/code/acme/widgets",
                                     "message": {"role": "user", "content": prompt}}) + "\n")
        path = self.write(os.path.join(self.fake_home, "t", "cut.jsonl"), "".join(lines))
        found = list(backfill.extract_file(path, backfill.FileState(), cfg, None, None, {}, entrypoint="cli"))
        self.assertEqual(len(found), len(cases))
        for rec, (name, (_prompt, fragments)) in zip(found, sorted(cases.items())):
            for fragment in fragments:
                self.assertNotIn(fragment, rec["text"], name)
            self.assertTrue(any(f.startswith("redacted:") for f in rec.get("flags") or []), name)


class AnchorAndAskCutTest(ApprenticeTestCase):
    def test_anchor_tail_never_starts_inside_a_placeholder(self):
        text = "start " + _TOKEN + " " + "w" * 390
        anchor = transcript._anchor(text, {})
        self.assertNotIn("A1b2", anchor)
        self.assertIsNone(_split_token(anchor))
        self.assertEqual(anchor, " " + "w" * 390)

    def test_raw_response_cut_keeps_placeholders_whole(self):
        raw = "a" * 3990 + " " + _TOKEN
        payload, flags, _ = asks.ask_payload("tu1", [], None, raw, {})
        self.assertIn("redacted:github-token", flags)
        self.assertNotIn("<redac", payload["raw_response"][-25:])

    def test_serialized_question_cut_keeps_placeholders_whole(self):
        question = "q" * (events.QUESTION_CAP - 5) + "<redacted:github-token> " + "z" * 40000
        record = {"v": 1, "id": "e1", "ts": "2030-01-05T10:00:00.000Z", "kind": "ask",
                  "questions": [{"question": question}]}
        stored = json.loads(events.serialize(record).decode("utf-8"))
        self.assertTrue(stored["questions"][0]["question"].endswith("q"))


def _split_token(text):
    """The trailing or leading fragment of a placeholder, if a cut split one."""
    for start in range(1, 8):
        head = text[-start:]
        if head.startswith("<") and not head.endswith(">") and "<redacted".startswith(head[:9]):
            return head
    return None
