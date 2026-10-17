import json
import os

from apprentice import backfill, config
from tests.base import REPO_ROOT, ApprenticeTestCase
from tests.fixtures import worktrees

_TRANSCRIPTS = os.path.join(REPO_ROOT, "tests", "fixtures", "transcripts")
_CWD = "/home/alice/code/acme/widgets"


def _fixture(name):
    return os.path.join(_TRANSCRIPTS, name)


def _line(role, content, ts, **extra):
    obj = {"type": role, "timestamp": ts, "sessionId": "sess-1", "cwd": _CWD,
           "message": {"role": role, "content": content}}
    obj.update(extra)
    return json.dumps(obj) + "\n"


class BackfillTest(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = config.load_config(self.home)

    def run_file(self, path, cfg=None, state=None, cutoff=None, since=None, entrypoint="cli", cache=None):
        state = state if state is not None else backfill.FileState()
        cache = cache if cache is not None else {}
        events = list(backfill.extract_file(path, state, cfg or self.cfg, cutoff, since, cache,
                                            entrypoint=entrypoint))
        return events, state

    def fixture_events(self, name, **kwargs):
        return self.run_file(_fixture(name), **kwargs)[0]

    def tmp_transcript(self, name, text):
        return self.write(os.path.join(self.fake_home, "t", name), text)

    def test_plain_two_prompts_with_prev_assistant(self):
        events = self.fixture_events("plain.jsonl")
        self.assertEqual([e["kind"] for e in events], ["prompt", "prompt"])
        self.assertEqual(events[0]["text"], "Please add a widget cache")
        self.assertIsNone(events[0]["prev_assistant"])
        self.assertEqual(events[1]["text"], "Now add tests for it")
        self.assertEqual(events[1]["prev_assistant"], "Added the cache.")
        first = events[0]
        self.assertEqual(first["source"], "backfill")
        self.assertEqual(first["fidelity"], "transcript")
        self.assertEqual(first["ts"], "2030-01-05T10:00:00Z")
        self.assertEqual(first["session_id"], "sess-1")
        self.assertEqual(first["entrypoint"], "cli")
        self.assertEqual(first["transcript_path"], _fixture("plain.jsonl"))
        self.assertIsNone(first["duplicate_of"])
        self.assertFalse(first["after_interrupt"])
        self.assertFalse(first["after_rejected_ask"])

    def test_missing_entrypoint_is_flagged(self):
        events = self.fixture_events("plain.jsonl", entrypoint=None)
        self.assertIsNone(events[0]["entrypoint"])
        self.assertIn("entrypoint_unknown", events[0]["flags"])

    def test_session_id_falls_back_to_file_stem(self):
        text = _line("user", "Hello there", "2030-01-05T10:00:00Z").replace('"sessionId": "sess-1", ', "")
        path = self.tmp_transcript("abc-stem.jsonl", text)
        events, _ = self.run_file(path)
        self.assertEqual(events[0]["session_id"], "abc-stem")

    def test_interrupt_flag(self):
        events = self.fixture_events("interrupt.jsonl")
        self.assertEqual([e["text"] for e in events], ["Refactor the router", "Continue with the tests instead"])
        self.assertFalse(events[0]["after_interrupt"])
        self.assertTrue(events[1]["after_interrupt"])

    def test_ask_plain_paired_from_structured_answers(self):
        events = self.fixture_events("ask_plain.jsonl")
        self.assertEqual([e["kind"] for e in events], ["prompt", "ask"])
        ask = events[1]
        self.assertEqual(ask["tool_use_id"], "tu1")
        answer = ask["questions"][0]["answer"]
        self.assertEqual(answer["labels"], ["Quick (Recommended)"])
        self.assertIs(answer["followed_recommended"], True)
        self.assertNotIn("partial", ask)
        self.assertEqual(ask["ts"], "2030-01-05T10:00:02Z")
        self.assertEqual(ask["ticket"], "XX-123")

    def test_ask_other_quote_intact(self):
        ask = self.fixture_events("ask_other_quote.jsonl")[1]
        answer = ask["questions"][0]["answer"]
        self.assertEqual(answer["free_text"], 'no, "quick" but keep the panel')
        self.assertTrue(answer["is_other"])

    def test_ask_multi_two_questions(self):
        ask = self.fixture_events("ask_multi.jsonl")[1]
        self.assertEqual([q["answer"]["labels"] for q in ask["questions"]], [["A1"], ["A2"]])

    def test_ask_answers_fall_back_to_result_text(self):
        lines = self.fixture_lines("ask_plain.jsonl")
        result = json.loads(lines[2])
        del result["toolUseResult"]
        path = self.tmp_transcript("text.jsonl", lines[0] + lines[1] + json.dumps(result) + "\n")
        ask = self.run_file(path)[0][1]
        self.assertEqual(ask["questions"][0]["answer"]["labels"], ["Quick (Recommended)"])

    def fixture_lines(self, name):
        with open(_fixture(name), "r", encoding="utf-8") as fh:
            return fh.readlines()

    def test_ask_rejected_marker_text(self):
        events = self.fixture_events("ask_rejected.jsonl")
        self.assertEqual([e["kind"] for e in events], ["prompt", "ask_rejected"])
        self.assertIsNone(events[1]["questions"][0]["answer"])
        self.assertNotIn("partial", events[1])

    def test_ask_rejected_by_is_error_alone(self):
        lines = self.fixture_lines("ask_rejected.jsonl")
        result = json.loads(lines[2])
        result["message"]["content"][0]["content"] = "Tool use was cancelled."
        path = self.tmp_transcript("err.jsonl", lines[0] + lines[1] + json.dumps(result) + "\n")
        self.assertEqual(self.run_file(path)[0][1]["kind"], "ask_rejected")

    def test_ask_clarify_marker_is_rejected(self):
        lines = self.fixture_lines("ask_rejected.jsonl")
        result = json.loads(lines[2])
        block = result["message"]["content"][0]
        block["content"] = "The user wants to clarify these questions first."
        del block["is_error"]
        path = self.tmp_transcript("clarify.jsonl", lines[0] + lines[1] + json.dumps(result) + "\n")
        self.assertEqual(self.run_file(path)[0][1]["kind"], "ask_rejected")

    def test_prompt_after_rejected_ask_is_flagged(self):
        lines = self.fixture_lines("ask_rejected.jsonl")
        follow = _line("user", "Do it the full way", "2030-01-05T10:00:03Z")
        path = self.tmp_transcript("after.jsonl", "".join(lines) + follow)
        events, _ = self.run_file(path)
        self.assertEqual(events[-1]["text"], "Do it the full way")
        self.assertTrue(events[-1]["after_rejected_ask"])
        self.assertFalse(events[0]["after_rejected_ask"])

    def test_relay_dropped(self):
        self.assertEqual(self.fixture_events("relay.jsonl"), [])

    def test_continued_dropped(self):
        self.assertEqual(self.fixture_events("continued.jsonl"), [])

    def test_compact_machine(self):
        events = self.fixture_events("compact.jsonl")
        self.assertEqual([e["kind"] for e in events], ["machine"])
        self.assertEqual(events[0]["command"], "/compact")

    def test_pasted_cut(self):
        events = self.fixture_events("pasted.jsonl")
        self.assertEqual(events[0]["kind"], "prompt")
        self.assertIn("pasted", events[0]["tags"])
        self.assertLessEqual(len(events[0]["text"]), 300)

    def test_image_only(self):
        path = self.tmp_transcript("img.jsonl", _line("user", "[Image #1]", "2030-01-05T10:00:00Z"))
        event = self.run_file(path)[0][0]
        self.assertEqual(event["kind"], "prompt")
        self.assertIsNone(event["text"])
        self.assertIsNone(event["register"])
        self.assertIn("image_only", event["tags"])

    def test_image_with_text_is_a_normal_prompt(self):
        event = self.fixture_events("image.jsonl")[0]
        self.assertEqual(event["text"], "[Image #1] why does this layout break")
        self.assertIsNotNone(event["register"])
        self.assertNotIn("image_only", event["tags"])

    def test_gitbranch_to_branch_and_ticket(self):
        event = self.fixture_events("gitbranch.jsonl")[0]
        self.assertEqual(event["branch"], "feat/XX-123-x")
        self.assertEqual(event["ticket"], "XX-123")

    def test_repo_from_deleted_worktree_cwd(self):
        paths = worktrees.build(os.path.join(self.fake_home, "tree"))
        text = "".join(self.fixture_lines("plain.jsonl")).replace(_CWD, paths["gone"])
        path = self.tmp_transcript("gone.jsonl", text)
        cache = {}
        events, _ = self.run_file(path, cache=cache)
        self.assertEqual({e["repo"] for e in events}, {"acme/widgets"})
        self.assertEqual(events[0]["cwd"], paths["gone"])
        self.assertIn(paths["gone"], cache)

    def test_repo_cache_is_used(self):
        cache = {_CWD: "cached/repo"}
        events, _ = self.run_file(_fixture("plain.jsonl"), cache=cache)
        self.assertEqual(events[0]["repo"], "cached/repo")

    def test_profile_rules_and_unknown(self):
        cfg = dict(self.cfg)
        cfg["profile_rules"] = [
            {"cwd_prefix": "/srv/other", "profile": "elsewhere"},
            {"cwd_prefix": "/home/alice/code", "profile": "work"},
            {"cwd_prefix": "/home/alice", "profile": "shadowed"},
        ]
        self.assertEqual(self.fixture_events("plain.jsonl", cfg=cfg)[0]["profile"], "work")
        cfg["profile_rules"] = [{"cwd_prefix": "/srv/other", "profile": "elsewhere"}]
        self.assertEqual(self.fixture_events("plain.jsonl", cfg=cfg)[0]["profile"], "unknown")
        self.assertEqual(self.fixture_events("plain.jsonl")[0]["profile"], "unknown")

    def test_hour_and_weekday_are_local_time(self):
        event = self.fixture_events("ask_plain.jsonl")[1]
        self.assertIn(event["hour_local"], range(24))
        self.assertIn(event["weekday"], range(7))

    def test_since_and_cutoff_skip_lines(self):
        texts = lambda events: [e["text"] for e in events]  # noqa: E731
        self.assertEqual(texts(self.fixture_events("plain.jsonl", since="2030-01-05T10:00:02Z")),
                         ["Now add tests for it"])
        self.assertEqual(texts(self.fixture_events("plain.jsonl", cutoff="2030-01-05T10:00:01Z")),
                         ["Now add tests for it"])
        self.assertEqual(texts(self.fixture_events("plain.jsonl", cutoff="2030-01-05T10:00:00Z",
                                                   since="2030-01-05T10:00:03Z")), [])
        self.assertEqual(len(self.fixture_events("plain.jsonl", since="2030-01-05T10:00:00Z")), 2)

    def test_skipped_lines_still_feed_context(self):
        events = self.fixture_events("plain.jsonl", since="2030-01-05T10:00:02Z")
        self.assertEqual(events[0]["prev_assistant"], "Added the cache.")

    def test_pending_state_survives_resume(self):
        lines = self.fixture_lines("ask_plain.jsonl")
        path = self.tmp_transcript("resume.jsonl", lines[0] + lines[1])
        events, state = self.run_file(path)
        self.assertEqual([e["kind"] for e in events], ["prompt"])
        self.assertEqual(len(state.pending_asks), 1)
        self.assertEqual(state.pending_asks[0]["tool_use_id"], "tu1")
        saved = json.loads(json.dumps(state.to_json()))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lines[2])
        resumed, state2 = self.run_file(path, state=backfill.FileState.from_json(saved))
        self.assertEqual([e["kind"] for e in resumed], ["ask"])
        self.assertEqual(resumed[0]["questions"][0]["answer"]["labels"], ["Quick (Recommended)"])
        self.assertEqual(state2.pending_asks, [])
        self.assertEqual(state2.lines_done, 3)
        self.assertEqual(state2.offset, os.path.getsize(path))

    def test_state_advances_per_line(self):
        state = backfill.FileState()
        gen = backfill.extract_file(_fixture("plain.jsonl"), state, self.cfg, None, None, {}, entrypoint="cli")
        next(gen)
        self.assertEqual(state.lines_done, 1)
        self.assertIs(state.last_user_interrupt, False)
        list(gen)
        self.assertEqual(state.lines_done, 3)
        self.assertEqual(state.last_assistant, "Added the cache.")

    def test_state_json_roundtrip(self):
        state = backfill.FileState()
        state.offset = 12
        state.lines_done = 3
        state.pending_asks = [{"tool_use_id": "tu9", "questions": [], "ts": "2030-01-05T10:00:00Z"}]
        state.last_assistant = "hi"
        state.last_user_interrupt = True
        again = backfill.FileState.from_json(json.loads(json.dumps(state.to_json())))
        self.assertEqual(again.to_json(), state.to_json())
        self.assertEqual(backfill.FileState.from_json({}).to_json(), backfill.FileState().to_json())

    def test_state_holds_no_user_text(self):
        events = self.fixture_events("interrupt.jsonl")
        self.assertTrue(events[1]["after_interrupt"])
        state = backfill.FileState.from_json({"last_user": "[Request interrupted by user] secret words"})
        self.assertIs(state.last_user_interrupt, False)
        self.assertNotIn("last_user", state.to_json())
        self.assertNotIn("secret words", json.dumps(state.to_json()))

    def test_assistant_anchor_scrubbed_before_state(self):
        secret = "AKIA" + "Q" * 16
        text = _line("assistant", [{"type": "text", "text": "use key %s here" % secret}], "2030-01-05T10:00:00Z")
        text += _line("user", "Next step please", "2030-01-05T10:00:01Z")
        events, state = self.run_file(self.tmp_transcript("anchor.jsonl", text))
        self.assertNotIn(secret, json.dumps(state.to_json()))
        self.assertNotIn(secret, events[0]["prev_assistant"])

    def test_assistant_anchor_uses_user_scrub_config(self):
        cfg = dict(self.cfg, extra_redact_patterns=[r"ZZ\d{4}"])
        text = _line("assistant", [{"type": "text", "text": "ticket ZZ1234 is done"}], "2030-01-05T10:00:00Z")
        text += _line("user", "Next step please", "2030-01-05T10:00:01Z")
        events, state = self.run_file(self.tmp_transcript("anchor_cfg.jsonl", text), cfg=cfg)
        self.assertIsNotNone(events[0]["prev_assistant"])
        self.assertNotIn("ZZ1234", events[0]["prev_assistant"])
        self.assertNotIn("ZZ1234", json.dumps(state.to_json()))

    def test_should_stop_checked_per_line(self):
        calls = []

        def should_stop():
            calls.append(1)
            return len(calls) > 2

        state = backfill.FileState()
        events = list(backfill.extract_file(_fixture("plain.jsonl"), state, self.cfg, None, None, {},
                                            entrypoint="cli", should_stop=should_stop))
        lines = self.fixture_lines("plain.jsonl")
        self.assertEqual([e["text"] for e in events], ["Please add a widget cache"])
        self.assertEqual(state.lines_done, 2)
        self.assertEqual(state.offset, len("".join(lines[:2]).encode("utf-8")))

    def test_filter_outcomes_counted(self):
        outcomes = {}
        for name in ("plain.jsonl", "relay.jsonl", "compact.jsonl", "interrupt.jsonl"):
            list(backfill.extract_file(_fixture(name), backfill.FileState(), self.cfg, None, None, {},
                                       entrypoint="cli", outcomes=outcomes))
        self.assertEqual(outcomes, {"prompt": 4, "drop": 1, "machine": 1, "interrupt": 1})

    def test_pending_asks_capped_at_twenty(self):
        uses = []
        for i in range(25):
            block = {"type": "tool_use", "id": "tu%d" % i, "name": "AskUserQuestion",
                     "input": {"questions": [{"question": "Q%d" % i, "options": []}]}}
            uses.append(_line("assistant", [block], "2030-01-05T10:00:00Z"))
        path = self.tmp_transcript("many.jsonl", "".join(uses))
        _, state = self.run_file(path)
        self.assertEqual(len(state.pending_asks), 20)
        self.assertEqual(state.pending_asks[0]["tool_use_id"], "tu5")

    def test_unmatched_tool_use_dropped(self):
        lines = self.fixture_lines("ask_plain.jsonl")
        path = self.tmp_transcript("unmatched.jsonl", lines[0] + lines[1])
        events, _ = self.run_file(path)
        self.assertEqual([e["kind"] for e in events], ["prompt"])

    def test_result_with_unknown_tool_use_id_is_ignored(self):
        lines = self.fixture_lines("ask_plain.jsonl")
        path = self.tmp_transcript("orphan.jsonl", lines[0] + lines[2])
        self.assertEqual([e["kind"] for e in self.run_file(path)[0]], ["prompt"])

    def test_truncated_last_line_is_left_for_next_run(self):
        full = "".join(self.fixture_lines("plain.jsonl"))
        path = self.tmp_transcript("cut.jsonl", full[:-20])
        events, state = self.run_file(path)
        self.assertEqual(len(events), 1)
        self.assertEqual(state.lines_done, 2)

    def test_garbage_lines_are_skipped(self):
        text = "not json\n[1, 2]\n" + _line("user", "Real prompt", "2030-01-05T10:00:00Z")
        path = self.tmp_transcript("junk.jsonl", text)
        events, state = self.run_file(path)
        self.assertEqual([e["text"] for e in events], ["Real prompt"])
        self.assertEqual(state.lines_done, 3)

    def test_prev_assistant_is_scrubbed_and_cut(self):
        long_text = "word " * 200
        text = _line("assistant", [{"type": "text", "text": long_text}], "2030-01-05T10:00:00Z")
        text += _line("user", "Next step please", "2030-01-05T10:00:01Z")
        event = self.run_file(self.tmp_transcript("long.jsonl", text))[0][0]
        self.assertLessEqual(len(event["prev_assistant"]), 400)

    def test_entrypoint_and_marker_detection(self):
        expected = {
            "entry_cli.jsonl": ("cli", False),
            "entry_desktop.jsonl": ("claude-desktop", False),
            "entry_sdk_cli.jsonl": ("sdk-cli", False),
            "entry_sdk_ts.jsonl": ("sdk-ts", False),
            "entry_none.jsonl": (None, False),
            "preclass_marker.jsonl": (None, True),
            "plain.jsonl": (None, False),
        }
        for name, want in expected.items():
            self.assertEqual(backfill.session_entrypoint_and_marker(_fixture(name)), want, name)

    def test_entrypoint_uses_first_line_that_has_one(self):
        text = _line("user", "one", "2030-01-05T10:00:00Z")
        text += _line("assistant", [{"type": "text", "text": "two"}], "2030-01-05T10:00:01Z", entrypoint="cli")
        text += _line("user", "three", "2030-01-05T10:00:02Z", entrypoint="sdk-ts")
        path = self.tmp_transcript("late.jsonl", text)
        self.assertEqual(backfill.session_entrypoint_and_marker(path), ("cli", False))

    def test_marker_only_on_first_user_line(self):
        text = _line("user", "plain first", "2030-01-05T10:00:00Z")
        text += _line("user", "[apprentice-preclassify v1] later", "2030-01-05T10:00:01Z")
        path = self.tmp_transcript("later.jsonl", text)
        self.assertEqual(backfill.session_entrypoint_and_marker(path), (None, False))

    def test_entrypoint_scan_reads_at_most_64k(self):
        filler = _line("user", "x" * 1000, "2030-01-05T10:00:00Z") * 70
        text = filler + _line("user", "far", "2030-01-05T10:00:01Z", entrypoint="cli")
        path = self.tmp_transcript("far.jsonl", text)
        self.assertEqual(backfill.session_entrypoint_and_marker(path), (None, False))

    def big_first_line(self, name, entrypoint_first=True):
        body = backfill.PRECLASSIFY_MARKER + "\n" + ("pair: Alice asked about widgets. " * 2500)
        obj = {"type": "user", "timestamp": "2030-01-05T10:00:00Z", "sessionId": "pc1", "cwd": _CWD}
        if entrypoint_first:
            obj["entrypoint"] = "sdk-cli"
        obj["message"] = {"role": "user", "content": body}
        if not entrypoint_first:
            obj["entrypoint"] = "sdk-cli"
        return self.tmp_transcript(name, json.dumps(obj) + "\n")

    def test_large_first_user_line_keeps_entrypoint_and_marker(self):
        path = self.big_first_line("big.jsonl")
        self.assertGreater(os.path.getsize(path), 80 * 1024)
        self.assertEqual(backfill.session_entrypoint_and_marker(path), ("sdk-cli", True))

    def test_first_line_cut_at_cap_falls_back_to_raw_bytes(self):
        saved = backfill._HEAD_CAP
        backfill._HEAD_CAP = 70 * 1024
        self.addCleanup(setattr, backfill, "_HEAD_CAP", saved)
        path = self.big_first_line("cap.jsonl")
        self.assertEqual(backfill.session_entrypoint_and_marker(path), ("sdk-cli", True))
        late = self.big_first_line("cap_late.jsonl", entrypoint_first=False)
        self.assertEqual(backfill.session_entrypoint_and_marker(late), (None, True))

    def test_entrypoint_missing_file(self):
        missing = os.path.join(self.fake_home, "nope.jsonl")
        self.assertEqual(backfill.session_entrypoint_and_marker(missing), (None, False))
