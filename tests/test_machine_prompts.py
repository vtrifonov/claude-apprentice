import json
import os

from apprentice import backfill, capture, config, distill, events, filters, preclassify, tone
from tests.base import REPO_ROOT, ApprenticeTestCase
from tests.fixtures.events.prompts import prompt_event
from tests.fixtures.machine_prompts import BUILT_IN, HEARTBEAT, QUOTING_HUMAN

_FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures")
_CWD = "/home/alice/code/acme/widgets"
_HUMAN = "no, keep the widget cache sorted by name"


def _line(text, ts, **extra):
    obj = {"type": "user", "timestamp": ts, "sessionId": "sess-1", "cwd": _CWD,
           "message": {"role": "user", "content": text}}
    obj.update(extra)
    return json.dumps(obj) + "\n"


class MachinePromptTestCase(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = config.load_config(self.home)

    def with_config(self, data):
        self.write(os.path.join(self.home, "config.json"), json.dumps(data))
        self.cfg = config.load_config(self.home)
        return self.cfg


class IsMachinePromptTest(MachinePromptTestCase):
    def test_built_ins_match(self):
        for name, text in sorted(BUILT_IN.items()):
            self.assertTrue(filters.is_machine_prompt(text, self.cfg), name)

    def test_human_prompts_kept(self):
        for text in (_HUMAN, QUOTING_HUMAN, HEARTBEAT, "Continue with the tests instead", "", None):
            self.assertFalse(filters.is_machine_prompt(text, self.cfg), text)

    def test_no_config_uses_built_ins_only(self):
        self.assertTrue(filters.is_machine_prompt(BUILT_IN["teammate"]))
        self.assertFalse(filters.is_machine_prompt(HEARTBEAT))

    def test_config_pattern(self):
        cfg = self.with_config({"machine_prompt_patterns": ["^Heartbeat for "]})
        self.assertEqual(cfg["machine_prompt_patterns"], ["^Heartbeat for "])
        self.assertTrue(filters.is_machine_prompt(HEARTBEAT, cfg))
        self.assertTrue(filters.is_machine_prompt("\n  " + HEARTBEAT, cfg))
        self.assertFalse(filters.is_machine_prompt("Why is the Heartbeat for acme/widgets late?", cfg))

    def test_bad_regex_ignored_with_warning(self):
        cfg = self.with_config({"machine_prompt_patterns": ["([unclosed", 7, "^Heartbeat for "]})
        self.assertEqual(cfg["machine_prompt_patterns"], ["^Heartbeat for "])
        self.assertTrue(filters.is_machine_prompt(HEARTBEAT, cfg))
        self.assertFalse(filters.is_machine_prompt(_HUMAN, cfg))
        with open(os.path.join(self.home, "hook-errors.log"), "r", encoding="utf-8") as fh:
            self.assertIn("rule: machine-prompt-patterns", fh.read())
        # an invalid pattern that slips past load_config never raises
        self.assertFalse(filters.is_machine_prompt(_HUMAN, {"machine_prompt_patterns": ["([unclosed"]}))

    def test_not_a_list_falls_back_to_default(self):
        cfg = self.with_config({"machine_prompt_patterns": "^Heartbeat for "})
        self.assertEqual(cfg["machine_prompt_patterns"], [])


class CaptureMachinePromptTest(MachinePromptTestCase):
    def payload(self, prompt):
        with open(os.path.join(_FIXTURES, "hook-payloads", "user_prompt.json"), "r", encoding="utf-8") as fh:
            text = fh.read()
        transcript = os.path.join(_FIXTURES, "transcripts", "plain.jsonl")
        data = json.loads(text.replace("__TRANSCRIPT__", transcript))
        data["prompt"] = prompt
        return data

    def run_prompt(self, prompt):
        capture.handle("user-prompt", self.payload(prompt), {"APPRENTICE_HOME": self.home,
                                                             "CLAUDE_CODE_ENTRYPOINT": "cli"})
        return list(events.iter_events(self.home))

    def test_each_built_in_dropped_at_capture(self):
        for name, text in sorted(BUILT_IN.items()):
            self.assertEqual(self.run_prompt(text), [], name)

    def test_quoting_human_kept_at_capture(self):
        (rec,) = self.run_prompt(QUOTING_HUMAN)
        self.assertEqual(rec["kind"], "prompt")
        self.assertEqual(rec["text"], QUOTING_HUMAN)

    def test_config_pattern_dropped_at_capture(self):
        self.with_config({"machine_prompt_patterns": ["^Heartbeat for ", "(bad"]})
        self.assertEqual(self.run_prompt(HEARTBEAT), [])
        self.assertEqual([rec["text"] for rec in self.run_prompt(_HUMAN)], [_HUMAN])


class BackfillMachinePromptTest(MachinePromptTestCase):
    def extract(self, text, cfg=None):
        path = self.write(os.path.join(self.fake_home, "t", "machine.jsonl"), text)
        outcomes = {}
        found = list(backfill.extract_file(path, backfill.FileState(), cfg or self.cfg, None, None, {},
                                           entrypoint="cli", outcomes=outcomes))
        return found, outcomes

    def test_each_built_in_dropped_at_backfill(self):
        lines = [_line(text, "2030-01-05T10:00:%02dZ" % index) for index, (_, text) in
                 enumerate(sorted(BUILT_IN.items()))]
        lines.append(_line(QUOTING_HUMAN, "2030-01-05T10:01:00Z"))
        found, outcomes = self.extract("".join(lines))
        self.assertEqual([rec["text"] for rec in found], [QUOTING_HUMAN])
        self.assertEqual(outcomes["machine_prompt"], len(BUILT_IN))

    def test_structured_markers_dropped_at_backfill(self):
        lines = [
            _line("Use the widget cache everywhere", "2030-01-05T10:00:00Z", isMeta=True),
            _line("Check the acme/widgets build", "2030-01-05T10:00:01Z", promptSource="system"),
            _line("Summarize the finished task", "2030-01-05T10:00:02Z", origin={"kind": "task-notification"}),
            _line(_HUMAN, "2030-01-05T10:00:03Z", promptSource="typed", origin={"kind": "human"}),
        ]
        found, outcomes = self.extract("".join(lines))
        self.assertEqual([rec["text"] for rec in found], [_HUMAN])
        self.assertEqual(outcomes["machine_prompt"], 3)

    def test_config_pattern_dropped_at_backfill(self):
        cfg = self.with_config({"machine_prompt_patterns": ["^Heartbeat for "]})
        found, _ = self.extract(_line(HEARTBEAT, "2030-01-05T10:00:00Z") + _line(_HUMAN, "2030-01-05T10:00:01Z"),
                                cfg)
        self.assertEqual([rec["text"] for rec in found], [_HUMAN])

    def test_ask_answer_on_meta_line_still_paired(self):
        ask_use = {"type": "assistant", "timestamp": "2030-01-05T10:00:00Z", "sessionId": "sess-1", "cwd": _CWD,
                   "message": {"role": "assistant", "content": [
                       {"type": "tool_use", "id": "tu1", "name": "AskUserQuestion",
                        "input": {"questions": [{"question": "Use the cache?", "header": "Cache",
                                                 "options": [{"label": "Yes"}, {"label": "No"}]}]}}]}}
        result = {"type": "user", "timestamp": "2030-01-05T10:00:05Z", "sessionId": "sess-1", "cwd": _CWD,
                  "isMeta": True, "toolUseResult": {"answers": {"Use the cache?": "Yes"}},
                  "message": {"role": "user", "content": [
                      {"type": "tool_result", "tool_use_id": "tu1", "content": "The user answered: x"}]}}
        found, _ = self.extract(json.dumps(ask_use) + "\n" + json.dumps(result) + "\n")
        self.assertEqual([rec["kind"] for rec in found], ["ask"])


class ReadTimeMachinePromptTest(MachinePromptTestCase):
    def stored(self, cfg=None):
        records = []
        for index, (name, text) in enumerate(sorted(BUILT_IN.items())):
            records.append(prompt_event(text=text, id="m-%s" % name, ts="2030-01-05T10:00:%02d.000Z" % index,
                                        session_id="s-%s" % name))
        records.append(prompt_event(text=QUOTING_HUMAN, id="human-quote", ts="2030-01-05T10:01:00.000Z",
                                    session_id="s-human"))
        records.append(prompt_event(text=HEARTBEAT, id="heartbeat", ts="2030-01-05T10:02:00.000Z",
                                    session_id="s-heartbeat"))
        return records

    def test_distill_clean_drops_machine_prompts(self):
        kept = distill.clean(self.stored(), self.cfg, "group-1", {"default": "group-1"})
        self.assertEqual(sorted(event["id"] for event in kept), ["heartbeat", "human-quote"])
        cfg = self.with_config({"machine_prompt_patterns": ["^Heartbeat for "]})
        kept = distill.clean(self.stored(), cfg, "group-1", {"default": "group-1"})
        self.assertEqual([event["id"] for event in kept], ["human-quote"])

    def test_tone_eligible_prompts_drops_machine_prompts(self):
        self.assertEqual(sorted(event["id"] for event in tone.eligible_prompts(self.stored(), self.cfg)),
                         ["heartbeat", "human-quote"])
        self.assertEqual(sorted(event["id"] for event in tone.eligible_prompts(self.stored())),
                         ["heartbeat", "human-quote"])
        cfg = self.with_config({"machine_prompt_patterns": ["^Heartbeat for "]})
        self.assertEqual([event["id"] for event in tone.eligible_prompts(self.stored(), cfg)], ["human-quote"])

    def test_preclassify_select_uncached_drops_machine_prompts(self):
        pairs = preclassify.select_uncached(self.stored(), self.home, "default", self.cfg)
        self.assertEqual(sorted(pair["event"]["id"] for pair in pairs), ["heartbeat", "human-quote"])
        cfg = self.with_config({"machine_prompt_patterns": ["^Heartbeat for "]})
        pairs = preclassify.select_uncached(self.stored(), self.home, "default", cfg)
        self.assertEqual([pair["event"]["id"] for pair in pairs], ["human-quote"])

    def test_stored_events_never_rewritten(self):
        for record in self.stored():
            events.append_event(self.home, record)
        before = [dict(record) for record in events.iter_events(self.home)]
        distill.clean(list(events.iter_events(self.home)), self.cfg, "group-1", {"default": "group-1"})
        self.assertEqual(list(events.iter_events(self.home)), before)
