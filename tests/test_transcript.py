import builtins
import json
import os
import unittest
from unittest import mock

from apprentice import transcript
from tests.base import ApprenticeTestCase, REPO_ROOT
from tests.fixtures.gen import generate_transcript

_FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "transcripts")
_NBYTES = 65536


def _line(kind, content, **extra):
    obj = {
        "type": kind,
        "timestamp": "2030-01-05T10:00:00Z",
        "sessionId": "sess-1",
        "cwd": "/home/alice/code/acme/widgets",
        "message": {"role": kind, "content": content},
    }
    obj.update(extra)
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _assistant(text, **extra):
    return _line("assistant", [{"type": "text", "text": text}], **extra)


class _Counting(object):
    def __init__(self, fh, counts):
        self._fh = fh
        self._counts = counts

    def read(self, *args):
        data = self._fh.read(*args)
        self._counts.append(len(data))
        return data

    def readline(self, *args):
        data = self._fh.readline(*args)
        self._counts.append(len(data))
        return data

    def readlines(self, *args):
        data = self._fh.readlines(*args)
        self._counts.append(sum(len(d) for d in data))
        return data

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()
        return False


class TailContextTest(ApprenticeTestCase):
    def _path(self, name="t.jsonl"):
        return os.path.join(self.home, name)

    def test_tail_bounded_on_large_file(self):
        path = self._path()
        last = generate_transcript(path, lines=2000, seed=1, words_per_line=400)
        self.assertGreater(os.path.getsize(path), 5 * 1024 * 1024)
        real_open = builtins.open
        counts = []

        def opener(target, *args, **kwargs):
            fh = real_open(target, *args, **kwargs)
            return _Counting(fh, counts) if target == path else fh

        with mock.patch("builtins.open", side_effect=opener):
            ctx = transcript.tail_context(path)
        self.assertTrue(counts)
        self.assertLessEqual(sum(counts), _NBYTES)
        self.assertEqual(ctx["prev_assistant"], " ".join(last.split())[-400:])

    def test_tail_drops_partial_first_line(self):
        path = self._path()
        # the oldest line is junk followed by a parseable object; the window cuts exactly between them,
        # so the fragment is valid JSON and only dropping the first line keeps its entrypoint out
        fragment = json.dumps({"type": "user", "entrypoint": "cli", "message": {"role": "user", "content": "q"}})
        second = _assistant("kept answer")
        self.write(path, "x" * 50 + fragment + "\n" + second)
        window = len(fragment) + 1 + len(second.encode("utf-8"))
        ctx = transcript.tail_context(path, nbytes=window)
        self.assertEqual(ctx["prev_assistant"], "kept answer")
        self.assertIsNone(ctx["entrypoint"])

    def test_tail_utf8_boundary(self):
        path = self._path()
        last = _assistant("final reply")
        mid_char = False
        for pad in range(3):
            size_pad = _NBYTES + 100
            first = _line("user", "€" * (size_pad // 3) + "a" * pad)
            self.write(path, first + last)
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                fh.seek(size - _NBYTES)
                byte = fh.read(1)[0]
            if 0x80 <= byte <= 0xBF:
                mid_char = True
            ctx = transcript.tail_context(path)
            self.assertEqual(ctx["prev_assistant"], "final reply")
        self.assertTrue(mid_char, "no variant cut inside a multi-byte character")

    def test_prev_assistant_last_400(self):
        path = self._path()
        text = "".join("%03d " % i for i in range(300))
        self.write(path, _line("user", "go") + _assistant(text))
        ctx = transcript.tail_context(path)
        expected = " ".join(text.split())[-400:]
        self.assertEqual(len(ctx["prev_assistant"]), 400)
        self.assertEqual(ctx["prev_assistant"], expected)

    def test_prev_assistant_whitespace_collapsed_and_joined(self):
        path = self._path()
        content = [{"type": "text", "text": "one\n\n  two"}, {"type": "tool_use", "name": "X"}, {"type": "text", "text": "three"}]
        self.write(path, _line("assistant", content))
        ctx = transcript.tail_context(path)
        self.assertEqual(ctx["prev_assistant"], "one two three")

    def test_prev_assistant_scrubbed(self):
        path = self._path()
        secret = "".join(["AK", "IA", "ABCDEFGHIJKLMNOP"])
        self.write(path, _assistant("key is " + secret + " ok"))
        ctx = transcript.tail_context(path)
        self.assertNotIn(secret, ctx["prev_assistant"])

    def test_after_interrupt(self):
        path = self._path()
        self.write(path, _line("user", "first") + _assistant("a") + _line("user", "[Request interrupted by user]"))
        self.assertTrue(transcript.tail_context(path)["after_interrupt"])
        self.write(path, _line("user", "[Request interrupted by user for tool use]"))
        self.assertTrue(transcript.tail_context(path)["after_interrupt"])
        self.write(path, _line("user", "[Request interrupted by user]") + _line("user", "later prompt"))
        self.assertFalse(transcript.tail_context(path)["after_interrupt"])

    def test_skip_current_prompt_line(self):
        path = self._path()
        self.write(path, _line("user", "[Request interrupted by user]") + _assistant("a") + _line("user", "next prompt"))
        self.assertFalse(transcript.tail_context(path)["after_interrupt"])
        ctx = transcript.tail_context(path, current_prompt="next prompt")
        self.assertTrue(ctx["after_interrupt"])
        self.assertEqual(ctx["prev_assistant"], "a")

    def test_prev_assistant_walks_back_past_tool_use(self):
        path = self._path()
        tool_use = _line("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}])
        tool_result = _line("user", [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "x"}])
        self.write(path, _line("user", "go") + _assistant("I will run the build") + tool_use + tool_result
                   + _line("user", "[Request interrupted by user for tool use]") + _line("user", "use make instead"))
        ctx = transcript.tail_context(path, current_prompt="use make instead")
        self.assertEqual(ctx["prev_assistant"], "I will run the build")
        self.assertTrue(ctx["after_interrupt"])

    def test_entrypoint_from_newest_line(self):
        path = self._path()
        self.write(path, _line("user", "a", entrypoint="cli") + _assistant("b", entrypoint="sdk-ts") + _line("user", "c"))
        self.assertEqual(transcript.tail_context(path)["entrypoint"], "sdk-ts")

    def test_missing_file_defaults(self):
        ctx = transcript.tail_context(os.path.join(self.home, "absent.jsonl"))
        self.assertEqual(ctx, {"prev_assistant": None, "after_interrupt": False, "entrypoint": None})

    def test_truncated_last_line_ignored(self):
        path = self._path()
        self.write(path, _assistant("whole") + '{"type": "assistant", "message": {"role": "assi')
        self.assertEqual(transcript.tail_context(path)["prev_assistant"], "whole")


class LineStreamTest(ApprenticeTestCase):
    def test_linestream_partial_not_consumed(self):
        path = os.path.join(self.home, "s.jsonl")
        self.write(path, "one\ntwo\nthr")
        stream = transcript.LineStream(path)
        got = list(stream)
        self.assertEqual(got, [(4, b"one\n"), (8, b"two\n")])
        self.assertEqual(stream.offset, 8)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("ee\n")
        more = transcript.LineStream(path, offset=stream.offset)
        self.assertEqual(list(more), [(14, b"three\n")])
        self.assertEqual(more.offset, 14)

    def test_linestream_skips_long_line(self):
        path = os.path.join(self.home, "s.jsonl")
        self.write(path, "short\n" + "y" * 5000 + "\nafter\n")
        stream = transcript.LineStream(path, max_line=1000)
        got = list(stream)
        self.assertEqual([line for _, line in got], [b"short\n", b"after\n"])
        self.assertEqual(stream.skipped_long, 1)
        self.assertEqual(stream.offset, os.path.getsize(path))


class ParseAnswerTest(unittest.TestCase):
    def test_parse_answer_dict_mapping(self):
        self.assertEqual(transcript.parse_ask_answer({"answers": {"Q1": "A1", "Q2": "A2"}}), {"Q1": "A1", "Q2": "A2"})

    def test_parse_answer_dict_list(self):
        resp = {"answers": [{"question": "Q1", "answer": "A1"}, {"question": "Q2", "answer": "A2"}]}
        self.assertEqual(transcript.parse_ask_answer(resp), {"Q1": "A1", "Q2": "A2"})

    def test_parse_answer_blocks(self):
        resp = [{"type": "text", "text": 'The user answered: "Q1"="A1", '}, {"type": "text", "text": '"Q2"="A2".'}]
        self.assertEqual(transcript.parse_ask_answer(resp), {"Q1": "A1", "Q2": "A2"})

    def test_parse_answer_string_single(self):
        self.assertEqual(transcript.parse_ask_answer('The user answered: "Q"="A".'), {"Q": "A"})

    def test_parse_answer_string_multi(self):
        text = 'Your questions have been answered: "Q1"="A1", "Q2"="A2".'
        self.assertEqual(transcript.parse_ask_answer(text), {"Q1": "A1", "Q2": "A2"})

    def test_parse_answer_string_escaped_quote(self):
        text = 'answered: "Q"="no, \\"quick\\" but keep".'
        self.assertEqual(transcript.parse_ask_answer(text), {"Q": 'no, "quick" but keep'})

    def test_parse_answer_unknown_none(self):
        self.assertIsNone(transcript.parse_ask_answer(42))
        self.assertIsNone(transcript.parse_ask_answer(None))
        self.assertIsNone(transcript.parse_ask_answer({"other": 1}))


_QUESTION = {
    "question": "Mode?",
    "multiSelect": False,
    "options": [
        {"label": "Quick (Recommended)", "description": "fast"},
        {"label": "Full", "description": "slow"},
    ],
}


class MapAnswerTest(unittest.TestCase):
    def test_map_answer_label_case_ws_recommended_ignored_raw_label_kept(self):
        for answer in ("Quick (Recommended)", "quick", "  QUICK   ", "Quick"):
            res = transcript.map_answer(answer, _QUESTION)
            self.assertEqual(res["labels"], ["Quick (Recommended)"], answer)
            self.assertEqual(res["labels"][0], _QUESTION["options"][0]["label"], answer)
            self.assertIsNone(res["free_text"], answer)
            self.assertFalse(res["is_other"], answer)

    def test_map_multi_select_split_only_when_all_match(self):
        question = dict(_QUESTION, multiSelect=True)
        res = transcript.map_answer("Quick, Full", question)
        self.assertEqual(res["labels"], ["Quick (Recommended)", "Full"])
        self.assertFalse(res["is_other"])
        res = transcript.map_answer("Quick, something else", question)
        self.assertEqual(res["labels"], [])
        self.assertEqual(res["free_text"], "Quick, something else")
        self.assertTrue(res["is_other"])
        res = transcript.map_answer("Quick, Full", _QUESTION)
        self.assertEqual(res["labels"], [])
        self.assertTrue(res["is_other"])

    def test_map_free_text_is_other(self):
        res = transcript.map_answer('no, "quick" but keep the panel', _QUESTION)
        self.assertEqual(res["labels"], [])
        self.assertEqual(res["free_text"], 'no, "quick" but keep the panel')
        self.assertTrue(res["is_other"])
        self.assertIsNone(res["followed_recommended"])

    def test_map_answer_none(self):
        self.assertIsNone(transcript.map_answer(None, _QUESTION))

    def test_followed_recommended_truth_table(self):
        self.assertIs(transcript.map_answer("Quick (Recommended)", _QUESTION)["followed_recommended"], True)
        self.assertIs(transcript.map_answer("Full", _QUESTION)["followed_recommended"], False)
        self.assertIsNone(transcript.map_answer("whatever", _QUESTION)["followed_recommended"])
        plain = {"multiSelect": False, "options": [{"label": "Yes", "description": ""}, {"label": "No", "description": ""}]}
        self.assertIsNone(transcript.map_answer("Yes", plain)["followed_recommended"])


class MessageTextTest(unittest.TestCase):
    def test_message_text_string_and_blocks(self):
        self.assertEqual(transcript.message_text({"message": {"content": "hi"}}), "hi")
        blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        self.assertEqual(transcript.message_text({"message": {"content": blocks}}), "a\nb")

    def test_message_text_tool_result_only_none(self):
        blocks = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]
        self.assertIsNone(transcript.message_text({"message": {"content": blocks}}))


class FixtureShapeTest(unittest.TestCase):
    def test_transcript_fixtures_are_json_lines(self):
        seen = 0
        for dirpath, _dirs, names in os.walk(_FIXTURES):
            for name in names:
                if not name.endswith(".jsonl") or name == "truncated.jsonl":
                    continue
                seen += 1
                with open(os.path.join(dirpath, name), "r", encoding="utf-8") as fh:
                    for raw in fh:
                        obj = json.loads(raw)
                        for key in ("type", "timestamp", "sessionId", "cwd", "message"):
                            self.assertIn(key, obj, name)
                        self.assertTrue(obj["timestamp"].endswith("Z"), name)
        self.assertGreaterEqual(seen, 22)

    def test_truncated_fixture_last_line_cut(self):
        with open(os.path.join(_FIXTURES, "truncated.jsonl"), "rb") as fh:
            data = fh.read()
        self.assertFalse(data.endswith(b"\n"))
        with self.assertRaises(ValueError):
            json.loads(data.split(b"\n")[-1])

    def test_ask_fixture_round_trip(self):
        with open(os.path.join(_FIXTURES, "ask_plain.jsonl"), "r", encoding="utf-8") as fh:
            objs = [json.loads(raw) for raw in fh]
        asked = [o for o in objs if o["type"] == "assistant"][0]
        question = [b for b in asked["message"]["content"] if b["type"] == "tool_use"][0]["input"]["questions"][0]
        result = [o for o in objs if "toolUseResult" in o][0]
        answers = transcript.parse_ask_answer(result["toolUseResult"])
        res = transcript.map_answer(answers[question["question"]], question)
        self.assertEqual(res["labels"], ["Quick (Recommended)"])
        self.assertIs(res["followed_recommended"], True)
        block_text = transcript.message_text(result)
        self.assertIsNone(block_text)
        content = result["message"]["content"][0]["content"]
        self.assertEqual(transcript.parse_ask_answer(content), answers)


if __name__ == "__main__":
    unittest.main()
