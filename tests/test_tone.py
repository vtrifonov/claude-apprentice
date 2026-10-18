import os
import re
from unittest import mock

from apprentice import config, lexicon, tone
from tests.base import ApprenticeTestCase
from tests.fixtures.events import tone_corpus
from tests.fixtures.events.tone_corpus import make_prompt, visible
from tests.fixtures.events.prompts import prompt_event

# Hand-computed from the texts in tests/fixtures/events/tone_corpus.py (17 non-ack prompts, 147 words split on
# whitespace, 145 letter-only words).
HIDDEN_ONLY_WORDS = ["layer", "cleanup", "report", "sure", "parser", "handles", "unicode", "explain", "plan",
                     "review", "informations", "dashboard", "ship", "zorbalice", "change", "today", "missing"]


class ToneTestCase(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = config.load_config()
        self.events = tone_corpus.corpus()
        self.prompts = tone.eligible_prompts(self.events)
        self.dict_path = self.write(os.path.join(self.home, "words"), tone_corpus.corpus_dictionary_text())
        patcher = mock.patch.object(lexicon, "DICT_PATH", self.dict_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def metrics(self, prompts=None, vis=visible):
        return tone.compute_metrics(self.prompts if prompts is None else prompts, vis, self.cfg)

    def texts(self, entries):
        return [entry["text"] for entry in entries]


class EligibleTests(ToneTestCase):
    def test_eligible_prompts_filters_kinds_flags_and_entrypoints(self):
        keep_cli = prompt_event(entrypoint="cli")
        keep_desktop = prompt_event(entrypoint="claude-desktop")
        keep_unknown = prompt_event(entrypoint=None)
        keep_ack = prompt_event(entrypoint="cli", register="ack", text="yep", tags=[])
        events = [
            keep_cli, keep_desktop, keep_unknown, keep_ack,
            prompt_event(kind="ask"),
            prompt_event(tags=["pasted"]),
            prompt_event(tags=["image_only"], text=None, register=None),
            prompt_event(duplicate_of="earlier-id"),
            prompt_event(entrypoint="sdk-cli"),
        ]
        got = tone.eligible_prompts(events)
        self.assertEqual([event["id"] for event in got],
                         [keep_cli["id"], keep_desktop["id"], keep_unknown["id"], keep_ack["id"]])


class MetricTests(ToneTestCase):
    def test_counts(self):
        counts = self.metrics()["counts"]
        self.assertEqual(counts["all"], 20)
        self.assertEqual(counts["visible"], 12)
        self.assertEqual(counts["by_register"], {"ack": 3, "direction": 11, "pushback": 2, "question": 2,
                                                 "status_ask": 1, "report": 1})

    def test_length_p50_p90_max(self):
        length = self.metrics()["length"]
        # non-ack words sorted: 5,5,5,7,7,7,8,9,9,9,10,10,10,11,11,12,12 (nearest rank)
        self.assertEqual(length["overall"], {"p50": 9, "p90": 12, "max": 12})
        by_register = length["by_register"]
        self.assertEqual(by_register["ack"], {"p50": 1, "p90": 1, "max": 1})
        self.assertEqual(by_register["direction"], {"p50": 9, "p90": 11, "max": 12})
        self.assertEqual(by_register["pushback"], {"p50": 11, "p90": 12, "max": 12})
        self.assertEqual(by_register["question"], {"p50": 5, "p90": 5, "max": 5})
        self.assertEqual(by_register["status_ask"], {"p50": 7, "p90": 7, "max": 7})
        self.assertEqual(by_register["report"], {"p50": 8, "p90": 8, "max": 8})

    def test_clauses_and_run_on(self):
        clauses = self.metrics()["clauses"]
        # joins total 16 over 17 prompts, clauses = joins + 1 -> 33 / 17; run-on: V1, V12, W1
        self.assertAlmostEqual(clauses["mean"], 33 / 17, places=4)
        self.assertAlmostEqual(clauses["run_on_share"], 3 / 17, places=4)

    def test_avg_word_len_and_ttr(self):
        result = self.metrics()
        self.assertAlmostEqual(result["avg_word_len"], 630 / 145, places=4)
        self.assertAlmostEqual(result["ttr_5000"], 87 / 145, places=4)

    def test_ttr(self):
        self.assertAlmostEqual(self.metrics()["ttr_5000"], 0.6, places=4)

    def test_registers_counts(self):
        self.assertEqual(self.metrics()["registers"],
                         {"yes": 3, "no": 0, "pushback": 2, "status_ask": 1, "directive": 3, "numbered": 1})

    def test_registers_lexicon_phrases(self):
        texts = ["no need of that", "let's skip it", "the fix is different", "good, approve it", "let's do that",
                 "post it", "but i said no", "i thought we've done it", "a, then b", "Q3: use b", "a",
                 "please do it", "a fix please"]
        prompts = [make_prompt("M%d" % i, "default", "direction", text, 1) for i, text in enumerate(texts)]
        self.assertEqual(self.metrics(prompts)["registers"],
                         {"yes": 3, "no": 3, "pushback": 2, "status_ask": 0, "directive": 3, "numbered": 3})

    def test_openers_ngrams_visible_only(self):
        result = self.metrics()
        self.assertEqual(result["openers"][:3], [{"text": "add", "count": 3}, {"text": "please", "count": 2},
                                                 {"text": "2", "count": 1}])
        self.assertEqual(self.texts(result["openers"]),
                         ["add", "please", "2", "any", "but", "hello", "i", "let's", "we", "where"])
        bigrams = result["bigrams"]
        self.assertEqual(bigrams[:3], [{"text": "add retry", "count": 2}, {"text": "retry helper", "count": 2},
                                       {"text": "acme widgets", "count": 1}])
        self.assertEqual(len(bigrams), 20)
        trigrams = result["trigrams"]
        self.assertEqual(trigrams[0], {"text": "add retry helper", "count": 2})
        self.assertEqual(len(trigrams), 20)
        for forbidden in ("build slow", "helper missing", "module helper"):
            self.assertNotIn(forbidden, self.texts(bigrams))
        for forbidden in ("retry helper module", "helper module helper"):
            self.assertNotIn(forbidden, self.texts(trigrams))
        top_words = result["top_words"]
        self.assertEqual(top_words[:13], [
            {"text": "add", "count": 3}, {"text": "build", "count": 3}, {"text": "helper", "count": 3},
            {"text": "job", "count": 3}, {"text": "retry", "count": 3}, {"text": "cache", "count": 2},
            {"text": "discuss", "count": 2}, {"text": "module", "count": 2}, {"text": "please", "count": 2},
            {"text": "recieve", "count": 2}, {"text": "slow", "count": 2}, {"text": "test", "count": 2},
            {"text": "widgets", "count": 2}])
        self.assertEqual(len(top_words), 43)
        for word in HIDDEN_ONLY_WORDS:
            self.assertNotIn(word, self.texts(top_words))
        for entry in result["openers"] + bigrams + trigrams + top_words:
            self.assertNotIn("zorbalice", entry["text"])

    def test_punctuation_rates(self):
        punct = self.metrics()["punctuation"]
        self.assertAlmostEqual(punct["lowercase_opener_rate"], 14 / 17, places=4)
        self.assertAlmostEqual(punct["final_period_rate"], 1 / 17, places=4)
        self.assertAlmostEqual(punct["question_mark_rate_on_questions"], 2 / 3, places=4)
        self.assertAlmostEqual(punct["exclamation_rate"], 1 / 17, places=4)
        self.assertEqual(punct["space_before_punct_rate"], 0.0)
        self.assertEqual((punct["curly_quotes"], punct["emoji_count"]), (0, 0))
        self.assertEqual((punct["greetings_count"], punct["signoffs_count"]), (1, 1))

    def test_punctuation_counts_on_extra_prompts(self):
        texts = ["hello there , this is fine .", "Hi! use “curly” quotes ‘x’ \U0001F600 please",
                 "ok, thanks", "Good morning team", "done. thank you"]
        prompts = [make_prompt("P%d" % i, "default", "direction", text, 1) for i, text in enumerate(texts)]
        punct = self.metrics(prompts)["punctuation"]
        self.assertAlmostEqual(punct["space_before_punct_rate"], 0.2, places=4)
        self.assertAlmostEqual(punct["exclamation_rate"], 0.2, places=4)
        self.assertEqual(punct["curly_quotes"], 4)
        self.assertEqual(punct["emoji_count"], 1)
        self.assertEqual(punct["greetings_count"], 3)
        self.assertEqual(punct["signoffs_count"], 2)

    def test_typos_exclude_code_tokens_and_repo_names(self):
        texts = ["please recieve the payload for alice and check retry_helper",
                 "recieve again and check retry_helper then teh widgets in src/widgets/panel.py",
                 "teh zorb and alice went to widgets, recieve."]
        prompts = [make_prompt("T%d" % i, "default", "direction", text, 1) for i, text in enumerate(texts)]
        path = self.write(os.path.join(self.home, "mini-words"), "please\npayload\ncheck\nwent\n")
        with mock.patch.object(lexicon, "DICT_PATH", path):
            spelling = self.metrics(prompts)["spelling"]
        # retry_helper (snake_case), src/widgets/panel.py (path), teh (3 letters), zorb (once),
        # widgets (piece of the repo name) and alice (cwd component) never qualify.
        self.assertEqual(spelling["typos"], [{"word": "recieve", "count": 3}])
        self.assertIsNone(spelling["typos_note"])

    def test_typos_none_with_note_when_dictionary_missing(self):
        missing = os.path.join(self.home, "no-such-words")
        with mock.patch.object(lexicon, "DICT_PATH", missing):
            spelling = self.metrics()["spelling"]
        self.assertIsNone(spelling["typos"])
        self.assertEqual(spelling["typos_note"], "typo detection skipped: no system dictionary at %s" % missing)
        names = [pattern["name"] for pattern in spelling["patterns"]]
        self.assertNotIn("missing_article", names)
        self.assertIn("informations", names)

    def test_patterns_examples_visible_only(self):
        spelling = self.metrics()["spelling"]
        self.assertIsNone(spelling["typos_note"])
        patterns = {pattern["name"]: pattern for pattern in spelling["patterns"]}
        self.assertEqual(list(patterns), ["missing_article", "informations", "prepositions"])
        self.assertEqual(patterns["missing_article"]["count"], 5)
        self.assertEqual(patterns["missing_article"]["examples"], ["create handler", "is silent"])
        self.assertEqual(patterns["informations"], {"name": "informations", "count": 1, "examples": []})
        self.assertEqual(patterns["prepositions"]["count"], 3)
        self.assertEqual(patterns["prepositions"]["examples"], ["discuss about"])
        # the visible corpus misspells "recieve" twice; "wigdet" once and the repo piece "widgets" never qualify
        self.assertEqual(spelling["typos"], [{"word": "recieve", "count": 2}])

    def test_proficiency_inputs(self):
        inputs = self.metrics()["proficiency_inputs"]
        # 18 sentences (V2 has two) over 147 whitespace-separated words (V8 counts "2" and "-") and 33 clauses
        self.assertAlmostEqual(inputs["sentence_len_mean"], 147 / 18, places=4)
        self.assertAlmostEqual(inputs["clause_density"], 33 / 18, places=4)
        # 79 content-token occurrences (once per prompt), 47 of them tokens found in exactly one prompt
        self.assertAlmostEqual(inputs["rarity_share"], 47 / 79, places=4)
        self.assertAlmostEqual(inputs["subordination_per_100"], 100 / 147, places=4)

    def test_rarity_share_zero_without_content_tokens(self):
        prompts = [make_prompt("R1", "default", "ack", "go", 1)]
        self.assertEqual(self.metrics(prompts)["proficiency_inputs"]["rarity_share"], 0.0)

    def test_numbers_equal_over_all_profiles_regardless_of_current_group(self):
        everyone = self.metrics(vis=lambda event: True)
        other_group = self.metrics(vis=lambda event: not visible(event))

        def numbers(result):
            kept = {key: result[key] for key in ("length", "clauses", "avg_word_len", "ttr_5000", "registers",
                                                 "proficiency_inputs")}
            kept["counts"] = {"all": result["counts"]["all"], "by_register": result["counts"]["by_register"]}
            kept["punctuation"] = result["punctuation"]
            kept["patterns"] = [(p["name"], p["count"]) for p in result["spelling"]["patterns"]]
            return kept

        self.assertEqual(numbers(everyone), numbers(other_group))
        self.assertEqual(everyone["counts"]["visible"], 20)
        self.assertEqual(other_group["counts"]["visible"], 8)


class ExemplarTests(ToneTestCase):
    def test_exemplars_per_register_masked_dedup_email_skipped(self):
        uuid = "-".join(["0a1b2c3d", "0a1b", "0a1b", "0a1b", "0a1b2c3d4e5f"])
        masked = make_prompt("X1", "default", "direction", "inspect the retry job %s again, queue drains, reruns, flaky suite" % uuid, 20)
        duplicate = make_prompt("X2", "default", "direction", self.events[0]["text"], 21)
        emailed = make_prompt("X3", "default", "direction",
                              "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima", 22,
                              flags=["email"])
        prompts = tone.eligible_prompts(self.events + [masked, duplicate, emailed])
        chosen, left_out = tone.select_exemplars(prompts, visible)
        texts = self.texts(chosen)
        self.assertEqual(left_out, 8)
        self.assertEqual(texts.count(self.events[0]["text"]), 1)
        self.assertNotIn(emailed["text"], texts)
        self.assertIn("inspect the retry job <uuid> again, queue drains, reruns, flaky suite", texts)
        self.assertFalse([text for text in texts if uuid in text])
        for hidden in ("zorbalice", "dashboard", "unicode", "layer"):
            self.assertFalse([text for text in texts if hidden in text])
        # direction: 5 chosen by content tokens (7, 7, 6, 5, 5...), then fill; every visible prompt fits under n=30
        first_direction = [entry for entry in chosen if entry["register"] == "direction"][:5]
        self.assertEqual(first_direction[0]["text"],
                         "inspect the retry job <uuid> again, queue drains, reruns, flaky suite")
        self.assertEqual(first_direction[1]["text"], self.events[0]["text"])
        self.assertEqual(set(chosen[0]), {"text", "date", "repo", "register"})
        self.assertEqual(chosen[0]["repo"], "acme/widgets")
        self.assertRegex(chosen[0]["date"], r"^2030-01-\d\d$")
        per_register = {}
        for entry in chosen[:11]:
            per_register[entry["register"]] = per_register.get(entry["register"], 0) + 1
        self.assertEqual(per_register, {"direction": 5, "pushback": 1, "question": 1, "ack": 2,
                                        "status_ask": 1, "report": 1})
        self.assertEqual(len(chosen), 13)

    def test_exemplars_cap_five_per_register_before_fill(self):
        prompts = tone.eligible_prompts(self.events)
        chosen, _ = tone.select_exemplars(prompts, visible, n=11)
        direction = [entry["text"] for entry in chosen if entry["register"] == "direction"]
        self.assertEqual(len(chosen), 11)
        self.assertEqual(len(direction), 5)
        self.assertEqual(direction[0], self.events[0]["text"])
        self.assertNotIn(self.events[7]["text"], direction)  # the 6th direction prompt (3 content tokens)

    def test_exemplars_count_left_out(self):
        prompts = tone.eligible_prompts(self.events)
        everyone, left_all = tone.select_exemplars(prompts, lambda event: True)
        self.assertEqual((len(everyone), left_all), (20, 0))
        none, left_none = tone.select_exemplars(prompts, lambda event: False)
        self.assertEqual((none, left_none), ([], 20))
        group, left_group = tone.select_exemplars(prompts, visible)
        self.assertEqual((len(group), left_group), (12, 8))
        self.assertFalse(re.search("zorbalice", " ".join(self.texts(group))))
