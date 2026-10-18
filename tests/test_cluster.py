import copy
import json

from apprentice import cluster, config
from tests.base import ApprenticeTestCase
from tests.fixtures.events import asks as ask_fixtures

_ALIASES = {"model set": "models", "quick mode": "mode"}
_MODEL_SET_ID = "r_a098793e"
_DAY = 86400.0


def _cfg():
    return dict(config.DEFAULTS)


def _all_visible(event):
    return True


def _epoch(ts):
    return config.ts_epoch(ts)


class NormalizeTest(ApprenticeTestCase):
    def test_normalize_strips_budget_dates_quotes_tickets_paths_numbers(self):
        norm = cluster.normalize_stem
        self.assertEqual(norm("Budget: 50% used, 2 days left. Use fable?"), "use fable?")
        self.assertEqual(norm("Weekly budget is tight, 3 days. Go on?"), "go on?")
        self.assertEqual(norm("Weekly usage 70%! Keep going?"), "keep going?")
        self.assertEqual(norm("Usage: 70% of the limit and no end"), "")
        self.assertEqual(norm("Ship on 2030-01-07 04:00 +02:00 now?"), "ship on now?")
        self.assertEqual(norm("Ship on 2030-01-07T04:00:05Z now?"), "ship on now?")
        self.assertEqual(norm('Use "fable" or \'opus\' or `haiku`?'), "use <q> or <q> or <q>?")
        self.assertEqual(norm("Is it don't or isn't ok?"), "is it dont or isnt ok?")
        self.assertEqual(norm("Does XX-123 block this?"), "does <ticket> block this?")
        self.assertEqual(norm("Edit src/app/main.py or notes.md now?"), "edit <path> or <path> now?")
        self.assertEqual(norm("Touch src/app?"), "touch <path>?")
        self.assertEqual(norm("Done at 80% yet?"), "done at yet?")
        self.assertEqual(norm("Use 2 files, ok?"), "use files ok?")

    def test_normalize_drops_every_digit_run(self):
        norm = cluster.normalize_stem
        self.assertEqual(norm("Resume 3 files?"), norm("Resume 5 files?"))
        self.assertEqual(norm("Resume 3 files?"), "resume files?")
        self.assertEqual(norm("Resume 12.5% of 40 files?"), "resume of files?")
        self.assertEqual(cluster.ask_rule_id(("", "h", norm("Resume 3 files?"), "a")),
                         cluster.ask_rule_id(("", "h", norm("Resume 5 files?"), "a")))

    def test_versions_and_abbreviations_are_not_paths(self):
        norm = cluster.normalize_stem
        self.assertEqual(norm("Use v1.2 or 3.5 now?"), "use v or now?")
        self.assertEqual(norm("Pick one, e.g. opus, now?"), "pick one eg opus now?")
        self.assertEqual(norm("Is that i.e? ok"), "is that ie?")
        self.assertEqual(norm("Edit notes.md or /tmp/x now?"), "edit <path> or <path> now?")

    def test_normalize_stops_at_first_question_mark_else_120_chars(self):
        self.assertEqual(cluster.normalize_stem("Quick mode? (test-only change: one spec file)"), "quick mode?")
        long_text = "word " * 60
        self.assertEqual(cluster.normalize_stem(long_text), ("word " * 24).strip())

    def test_fold_header(self):
        self.assertEqual(cluster.fold_header("  Model   SET ", _ALIASES), "models")
        self.assertEqual(cluster.fold_header("Quick mode", _ALIASES), "mode")
        self.assertEqual(cluster.fold_header("Approve", _ALIASES), "approve")
        self.assertEqual(cluster.fold_header(None, _ALIASES), "")


class KeyTest(ApprenticeTestCase):
    def test_label_set_strips_recommended_and_sorts(self):
        key = cluster.cluster_key("s", "Models", "Use fable?", ["No fable (opus)", "Fable (Recommended)"],
                                  _ALIASES)
        self.assertEqual(key, ("s", "models", "use fable?", "fable|no fable (opus)"))
        same = cluster.cluster_key("s", "models", "Use fable?", ["Fable", "No fable (opus)"], _ALIASES)
        self.assertEqual(key, same)
        self.assertEqual(cluster.key_string(key), "s\x1fmodels\x1fuse fable?\x1ffable|no fable (opus)")
        self.assertEqual(cluster.cluster_key(None, "H", "Q?", [], _ALIASES), ("", "h", "q?", ""))

    def test_ask_rule_id_shape(self):
        key = ("", "h", "q?", "a|b")
        rule_id = cluster.ask_rule_id(key)
        self.assertRegex(rule_id, r"^r_[0-9a-f]{8}$")
        self.assertEqual(rule_id, cluster.ask_rule_id(key))


class ClusterTest(ApprenticeTestCase):
    def _clusters(self, events, visible=_all_visible):
        return cluster.build_ask_clusters(events, _cfg(), visible)

    def test_five_model_set_variants_one_cluster_one_id(self):
        variants = ask_fixtures.model_set_variants()
        self.assertEqual(len(variants), 5)
        clusters = self._clusters(variants)
        self.assertEqual(len(clusters), 1)
        found = clusters[0]
        self.assertEqual(found["n"], 5)
        self.assertEqual(found["header"], "models")
        self.assertEqual(found["stem"], "use fable?")
        self.assertEqual(found["raw_headers"], ["Model set", "Models"])
        again = self._clusters(ask_fixtures.model_set_variants())
        self.assertEqual(again[0]["id"], found["id"])
        self.assertEqual(found["id"], _MODEL_SET_ID)

    def test_three_mode_variants_one_cluster_via_default_aliases(self):
        clusters = self._clusters(ask_fixtures.mode_variants())
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["n"], 3)
        self.assertEqual(clusters[0]["header"], "mode")
        self.assertEqual(clusters[0]["stem"], "quick mode?")
        self.assertEqual(clusters[0]["raw_headers"], ["Mode", "Quick mode"])

    def test_rejected_asks_are_not_clustered(self):
        rejected = ask_fixtures.ask_event(kind="ask_rejected")
        self.assertEqual(self._clusters([rejected]), [])

    def test_answers_distribution_other_for_free_text(self):
        events = [
            ask_fixtures.ask_event(ts="2030-01-05T10:00:01.000Z"),
            ask_fixtures.ask_event(ts="2030-01-05T10:00:02.000Z"),
            ask_fixtures.ask_event(ts="2030-01-05T10:00:03.000Z", answer_labels=["No fable (opus)"]),
            ask_fixtures.ask_event(ts="2030-01-05T10:00:04.000Z", free_text="neither"),
        ]
        found = self._clusters(events)[0]
        self.assertEqual(dict(found["answers"]), {"fable": 2, "no fable (opus)": 1, "other": 1})
        self.assertEqual(found["n"], 4)

    def test_rec_follow_rate(self):
        events = [
            ask_fixtures.ask_event(ts="2030-01-05T10:00:01.000Z"),
            ask_fixtures.ask_event(ts="2030-01-05T10:00:02.000Z"),
            ask_fixtures.ask_event(ts="2030-01-05T10:00:03.000Z"),
            ask_fixtures.ask_event(ts="2030-01-05T10:00:04.000Z", answer_labels=["No fable (opus)"]),
            ask_fixtures.ask_event(ts="2030-01-05T10:00:05.000Z", free_text="neither"),
        ]
        found = self._clusters(events)[0]
        self.assertEqual(found["rec_follow_rate"], 0.75)
        no_rec = ask_fixtures.ask_event(option_labels=["Yes", "No"], answer_labels=["Yes"])
        self.assertIsNone(self._clusters([no_rec])[0]["rec_follow_rate"])

    def test_free_texts_visible_only_and_withheld_count(self):
        address = "bob" + "@" + "example" + ".test"
        mine = ask_fixtures.ask_event(profile="mine", free_text="use bob at %s please" % address,
                                      ts="2030-01-05T10:00:01.000Z")
        theirs = ask_fixtures.ask_event(profile="theirs", free_text="secret plan",
                                        ts="2030-01-05T10:00:02.000Z")
        found = self._clusters([mine, theirs], visible=lambda e: e["profile"] == "mine")[0]
        self.assertEqual(found["free_texts"], ["use bob at <email> please"])
        self.assertEqual(found["free_text_withheld"], 1)
        self.assertEqual(found["visible_rows"], 1)
        self.assertEqual(found["withheld_rows"], 1)
        self.assertTrue(found["text_visible"])
        withheld_row = [row for row in found["rows"] if not row["visible"]][0]
        self.assertIsNone(withheld_row["free_text"])
        self.assertTrue(withheld_row["is_other"])
        none_visible = self._clusters([theirs], visible=lambda e: False)[0]
        self.assertFalse(none_visible["text_visible"])
        self.assertEqual(none_visible["display_questions"], [])
        self.assertEqual(none_visible["free_texts"], [])

    def test_reversal_same_session_within_7_days_and_not_after(self):
        first = ask_fixtures.ask_event(ts="2030-01-05T10:00:00.000Z", repo=None)
        flipped = ask_fixtures.ask_event(ts="2030-01-11T10:00:00.000Z", repo=None,
                                         answer_labels=["No fable (opus)"])
        found = self._clusters([first, flipped])[0]
        self.assertEqual(found["reversals"], [{
            "ts_a": "2030-01-05T10:00:00.000Z", "answer_a": "fable",
            "ts_b": "2030-01-11T10:00:00.000Z", "answer_b": "no fable (opus)"}])
        late = ask_fixtures.ask_event(ts="2030-01-13T10:00:01.000Z", repo=None,
                                      answer_labels=["No fable (opus)"])
        self.assertEqual(self._clusters([first, late])[0]["reversals"], [])
        other_session = ask_fixtures.ask_event(ts="2030-01-06T10:00:00.000Z", repo=None,
                                               session_id="sess-2", answer_labels=["No fable (opus)"])
        self.assertEqual(self._clusters([first, other_session])[0]["reversals"], [])
        same_answer = ask_fixtures.ask_event(ts="2030-01-06T10:00:00.000Z", repo=None)
        self.assertEqual(self._clusters([first, same_answer])[0]["reversals"], [])

    def test_reversals_list_only_adjacent_flips(self):
        answers = [["Fable (Recommended)"], ["No fable (opus)"], ["Fable (Recommended)"], ["No fable (opus)"]]
        events = [ask_fixtures.ask_event(ts="2030-01-05T10:00:0%d.000Z" % index, answer_labels=labels)
                  for index, labels in enumerate(answers)]
        found = self._clusters(events)[0]
        self.assertEqual([(r["answer_a"], r["answer_b"]) for r in found["reversals"]],
                         [("fable", "no fable (opus)"), ("no fable (opus)", "fable"),
                          ("fable", "no fable (opus)")])
        steady = [ask_fixtures.ask_event(ts="2030-01-05T10:00:0%d.000Z" % index) for index in range(3)]
        steady.append(ask_fixtures.ask_event(ts="2030-01-05T10:00:09.000Z", answer_labels=["No fable (opus)"]))
        self.assertEqual(len(self._clusters(steady)[0]["reversals"]), 1)

    def test_rows_carry_recommended_labels_raw(self):
        found = self._clusters([ask_fixtures.ask_event(),
                                ask_fixtures.ask_event(option_labels=["Yes", "No"], answer_labels=["Yes"],
                                                       ts="2030-01-05T10:00:02.000Z")])
        by_labels = {tuple(row["labels"]): row for found_one in found for row in found_one["rows"]}
        self.assertEqual(by_labels[("fable",)]["recommended"], ["Fable (Recommended)"])
        self.assertIsNone(by_labels[("yes",)]["recommended"])

    def test_non_visible_rows_and_headers_leak_nothing(self):
        marker = "zq" + "leak" + "9"
        mine = ask_fixtures.ask_event(profile="mine", ts="2030-01-05T10:00:01.000Z", header="Models",
                                      ticket="XX-123")
        theirs = ask_fixtures.ask_event(profile="theirs", ts="2030-01-05T10:00:02.000Z",
                                        header="Model set", free_text="plan " + marker,
                                        ticket="LK-123", cwd="/home/" + marker + "/code")
        other = ask_fixtures.ask_event(profile="theirs", ts="2030-01-05T10:00:03.000Z", question="Other " + marker + "?",
                                       header="Hdr " + marker, free_text="more " + marker, ticket="LK-9",
                                       cwd="/home/" + marker)
        found = self._clusters([mine, theirs, other], visible=lambda e: e["profile"] == "mine")
        by_stem = {item["stem"]: item for item in found}
        group = by_stem["use fable?"]
        self.assertEqual(group["raw_headers"], ["Models"])  # the hidden "Model set" spelling is not listed
        hidden = [row for row in group["rows"] if not row["visible"]][0]
        self.assertEqual(hidden["ticket"], "LK")
        self.assertIsNone(hidden["cwd"])
        self.assertEqual(hidden["repo"], "acme/widgets")
        visible = [row for row in group["rows"] if row["visible"]][0]
        self.assertEqual(visible["ticket"], "XX-123")
        # a cluster that is entirely non-visible: only identity fields may carry the marker
        other_cluster = by_stem[cluster.normalize_stem("Other " + marker + "?")]
        self.assertEqual(other_cluster["raw_headers"], [])
        stripped = {key: value for key, value in other_cluster.items() if key not in ("key", "stem", "header", "id")}
        self.assertNotIn(marker, json.dumps(stripped, default=sorted))
        self.assertNotIn(marker, json.dumps({k: v for k, v in group.items() if k not in ("key",)}, default=sorted))

    def test_visible_row_free_text_is_outbound_masked(self):
        address = "bob" + "@" + "example" + ".test"
        event = ask_fixtures.ask_event(free_text="mail %s now" % address)
        row = self._clusters([event])[0]["rows"][0]
        self.assertEqual(row["free_text"], "mail <email> now")

    def test_reversal_same_repo(self):
        first = ask_fixtures.ask_event(ts="2030-01-05T10:00:00.000Z", session_id="sess-1")
        flipped = ask_fixtures.ask_event(ts="2030-01-07T10:00:00.000Z", session_id="sess-2",
                                         answer_labels=["No fable (opus)"])
        found = self._clusters([flipped, first])[0]
        self.assertEqual(len(found["reversals"]), 1)
        self.assertEqual(found["reversals"][0]["ts_a"], "2030-01-05T10:00:00.000Z")
        other_repo = copy.deepcopy(flipped)
        other_repo["repo"] = "acme/gadgets"
        self.assertEqual(self._clusters([first, other_repo])[0]["reversals"], [])

    def test_reversal_30d(self):
        first = ask_fixtures.ask_event(ts="2030-01-05T10:00:00.000Z")
        flipped = ask_fixtures.ask_event(ts="2030-01-06T10:00:00.000Z", answer_labels=["No fable (opus)"])
        found = self._clusters([first, flipped])[0]
        base = _epoch("2030-01-06T10:00:00.000Z")
        self.assertTrue(cluster.reversal_30d(found, base + 29 * _DAY))
        self.assertFalse(cluster.reversal_30d(found, base + 31 * _DAY))
        self.assertFalse(cluster.reversal_30d(self._clusters([first])[0], base))

    def test_display_questions_three_most_recent_visible(self):
        events = []
        for index in range(5):
            events.append(ask_fixtures.ask_event(question="Use fable? run %d" % index,
                                                 ts="2030-01-05T10:00:0%d.000Z" % index,
                                                 profile="mine" if index != 4 else "theirs"))
        found = self._clusters(events, visible=lambda e: e["profile"] == "mine")[0]
        self.assertEqual(found["display_questions"],
                         ["Use fable? run 3", "Use fable? run 2", "Use fable? run 1"])

    def test_rows_carry_departure_fields(self):
        event = ask_fixtures.ask_event(ticket="XX-123", branch="feat/x")
        row = self._clusters([event])[0]["rows"][0]
        self.assertEqual(row["event_id"], event["id"])
        self.assertEqual(row["session_id"], "sess-1")
        self.assertEqual(row["ts"], event["ts"])
        self.assertEqual(row["profile"], "default")
        self.assertTrue(row["visible"])
        self.assertEqual(row["labels"], ["fable"])
        self.assertFalse(row["is_other"])
        self.assertIs(row["followed_recommended"], True)
        self.assertEqual(row["repo"], "acme/widgets")
        self.assertEqual(row["ticket"], "XX-123")
        self.assertEqual(row["numbers"]["used_pct"], 74.0)
        self.assertEqual(row["risk_words"], [])
        self.assertEqual(row["skill_hint"], "oneshot-pipeline")
        self.assertEqual(row["weekday"], 5)
        self.assertEqual(row["hour_local"], 10)
        self.assertEqual(row["cwd"], "/home/alice/code/acme/widgets")
        self.assertEqual(row["reset_at"], "2030-01-07 04:00 +02:00")
        self.assertEqual(row["numbers_other"], [])
        self.assertEqual(row["risk_words_negated"], [])

    def test_cluster_summary_fields(self):
        events = ask_fixtures.model_set_variants()
        events[2]["session_id"] = "sess-2"
        events[2]["repo"] = "acme/gadgets"
        found = self._clusters(events)[0]
        self.assertEqual(found["skill"], "oneshot-pipeline")
        self.assertEqual(found["labels"], ["fable", "no fable (opus)"])
        self.assertEqual(found["sessions"], ["sess-1", "sess-2"])
        self.assertEqual(found["repos"], ["acme/gadgets", "acme/widgets"])
        self.assertEqual(found["first_seen"], "2030-01-05T10:00:01.000Z")
        self.assertEqual(found["last_seen"], "2030-01-05T10:00:05.000Z")
