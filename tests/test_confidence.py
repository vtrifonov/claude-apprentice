import os
import time
from collections import Counter

from apprentice import cluster, confidence
from tests.base import ApprenticeTestCase

DAY1 = "2026-03-02T10:00:00.000Z"
DAY1_LATER = "2026-03-02T10:30:00.000Z"
DAY4 = "2026-03-05T10:00:00.000Z"


def _prompt(event_id, text, ts=DAY1, session="s1", prev="assistant said something", **extra):
    event = {"id": event_id, "kind": "prompt", "ts": ts, "session_id": session, "text": text,
             "prev_assistant": prev, "strong_marker": False}
    event.update(extra)
    return event


def _pair(event):
    return {"event": event, "ts": event["ts"], "session_id": event["session_id"]}


def _ask_row(event_id, ts=DAY1, session="s1"):
    return {"event": {"id": event_id, "kind": "ask", "ts": ts, "session_id": session}, "ts": ts,
            "session_id": session}


class TwoSignalTest(ApprenticeTestCase):
    def test_single_pick_not_candidate(self):
        self.assertIsNone(confidence.two_signal("ask", [_ask_row("e1")], []))

    def test_single_prompt_not_candidate(self):
        pair = _pair(_prompt("e1", "undo that", strong_marker=False))
        self.assertIsNone(confidence.two_signal("correction", [pair], []))

    def test_run_the_build_again_not_candidate(self):
        first = _pair(_prompt("e1", "run the build again", ts=DAY1))
        second = _pair(_prompt("e2", "run the build again", ts=DAY1_LATER))
        self.assertIsNone(confidence.two_signal("correction", [first, second], []))

    def test_single_but_i_said_not_candidate(self):
        pair = _pair(_prompt("e1", "but i said use tabs", strong_marker=True))
        self.assertIsNone(confidence.two_signal("correction", [pair], []))

    def test_strong_marker_plus_independent_passes_path2(self):
        first = _pair(_prompt("e1", "remember that we never use tabs", strong_marker=True, ts=DAY1))
        second = _pair(_prompt("e2", "use spaces", ts=DAY1_LATER))
        self.assertEqual(confidence.two_signal("correction", [first, second], []), "strong-marker")

    def test_same_pair_without_assistant_turn_fails(self):
        first = _pair(_prompt("e1", "remember that we never use tabs", strong_marker=True, ts=DAY1))
        second = _pair(_prompt("e2", "use spaces", ts=DAY1_LATER, prev=None))
        self.assertIsNone(confidence.two_signal("correction", [first, second], []))

    def test_duplicate_of_not_second_signal(self):
        first = _pair(_prompt("e1", "remember that we never use tabs", strong_marker=True, ts=DAY1))
        second = _pair(_prompt("e2", "remember that we never use tabs", ts=DAY1_LATER, duplicate_of="e1"))
        self.assertIsNone(confidence.two_signal("correction", [first, second], []))

    def test_different_days_path(self):
        first = _pair(_prompt("e1", "run the build again", ts=DAY1, session="s1"))
        second = _pair(_prompt("e2", "run the build again", ts=DAY4, session="s2"))
        self.assertEqual(confidence.two_signal("correction", [first, second], []), "different-days")

    def test_different_days_same_session_fails(self):
        first = _pair(_prompt("e1", "run the build again", ts=DAY1, session="s1"))
        second = _pair(_prompt("e2", "run the build again", ts=DAY4, session="s1"))
        self.assertIsNone(confidence.two_signal("correction", [first, second], []))

    def test_same_event_twice_not_independent(self):
        event = _prompt("e1", "remember that we never use tabs", strong_marker=True)
        self.assertFalse(confidence.independent(event, dict(event)))

    def test_same_session_turn_not_independent(self):
        first = _prompt("e1", "use spaces")
        second = _prompt("e2", "use spaces")
        self.assertFalse(confidence.independent(first, second))

    def test_ask_plus_linked_correction_path(self):
        self.assertEqual(confidence.two_signal("ask", [_ask_row("e1")], ["r_00000001"]),
                         "ask-plus-correction")
        group = [_pair(_prompt("e1", "undo that"))]
        self.assertEqual(confidence.two_signal("correction", group, ["r_00000002"]),
                         "ask-plus-correction")

    def test_ask_rows_independent_without_prev_assistant(self):
        first = _ask_row("e1", ts=DAY1, session="s1")
        second = _ask_row("e2", ts=DAY4, session="s2")
        self.assertEqual(confidence.two_signal("ask", [first, second], []), "different-days")

    def test_marker_only_text_fails_tag_condition(self):
        first = _pair(_prompt("e1", "from now on", strong_marker=True, ts=DAY1))
        second = _pair(_prompt("e2", "ok then", ts=DAY1_LATER))
        self.assertIsNone(confidence.two_signal("correction", [first, second], []))

    def test_withheld_view_gives_same_path_as_visible_view(self):
        weak = _prompt("e1", "use spaces", ts=DAY1, tags=["correction"])
        strong = _prompt("e2", "remember that we never use tabs", ts=DAY1_LATER, strong_marker=True,
                         tags=["standing"], after_interrupt=False)
        seen = cluster.select_pairs([weak, strong])
        hidden = cluster.select_pairs([weak, strong], visible=lambda event: event["id"] == "e1")
        self.assertIn("text", seen[0]["event"])
        self.assertNotIn("text", hidden[0]["event"])
        self.assertEqual(confidence.two_signal("correction", seen, []), "strong-marker")
        self.assertEqual(confidence.two_signal("correction", hidden, []), "strong-marker")

    def test_withheld_marker_only_event_still_fails_tag(self):
        weak = _prompt("e1", "use spaces", ts=DAY1, tags=["correction"])
        marker = _prompt("e2", "from now on", ts=DAY1_LATER, strong_marker=True, tags=["standing"])
        hidden = cluster.select_pairs([weak, marker], visible=lambda event: event["id"] == "e1")
        self.assertIsNone(confidence.two_signal("correction", hidden, []))

    def test_withheld_events_carry_explicit_anchor_flag(self):
        event = _prompt("e1", "use spaces", tags=["correction"])
        hidden = cluster.select_pairs([event], visible=lambda _event: False)[0]["event"]
        self.assertIs(hidden["anchored"], True)
        self.assertNotIn("prev_assistant", hidden)

    def test_missing_anchor_keys_mean_not_anchored(self):
        first = _pair(_prompt("e1", "use spaces", ts=DAY1))
        keyless = {"id": "e2", "kind": "prompt", "ts": DAY4, "session_id": "s2", "text": "use spaces"}
        self.assertFalse(confidence.independent(first["event"], keyless))
        self.assertIsNone(confidence.two_signal("correction", [first, _pair(keyless)], []))
        self.assertTrue(confidence.independent(first["event"], dict(keyless, anchored=True)))
        self.assertFalse(confidence.independent(first["event"], dict(keyless, anchored=False, prev_assistant="x")))

    def test_strong_marker_partner_with_only_interrupt_flag_passes(self):
        # spec 10.5: two independent occurrences with at least one strong marker; the partner needs no correction text
        strong = _pair(_prompt("e1", "remember that we never use tabs", strong_marker=True, ts=DAY1))
        partner = _pair(_prompt("e2", "continue", ts=DAY1_LATER, after_interrupt=True))
        self.assertEqual(confidence.two_signal("correction", [strong, partner], []), "strong-marker")

    def test_three_thousand_same_session_asks_are_fast(self):
        rows = [_ask_row("e%d" % index, ts="2026-03-02T10:%02d:00.000Z" % (index % 60)) for index in range(3000)]
        started = time.time()
        self.assertIsNone(confidence.two_signal("ask", rows, []))
        self.assertLess(time.time() - started, 2.0)

    def test_many_sessions_without_a_qualifying_pair_are_fast(self):
        rows = [_ask_row("e%d" % index, session="s%d" % (index % 2)) for index in range(3000)]
        started = time.time()
        self.assertIsNone(confidence.two_signal("ask", rows, []))
        self.assertLess(time.time() - started, 2.0)


class LocalDayTest(ApprenticeTestCase):
    LATE = "2026-03-02T23:30:00.000Z"
    EARLY = "2026-03-03T00:30:00.000Z"

    def path(self, tz):
        old = os.environ.get("TZ")

        def restore():
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

        self.addCleanup(restore)
        os.environ["TZ"] = tz
        time.tzset()
        first = _pair(_prompt("e1", "run the build again", ts=self.LATE, session="s1"))
        second = _pair(_prompt("e2", "run the build again", ts=self.EARLY, session="s2"))
        return confidence.two_signal("correction", [first, second], [])

    def test_utc_midnight_crossing_is_different_days(self):
        self.assertEqual(self.path("UTC"), "different-days")

    def test_local_time_decides_the_day(self):
        self.assertIsNone(self.path("Pacific/Kiritimati"))


def _band(n=5, sessions=3, repos=1, consistency=1.0, reversal_30d=False, has_reversal=False, path=None,
          split_purity=None, majority=None):
    return confidence.band(n, sessions, repos, consistency, reversal_30d, has_reversal, path,
                           split_purity, majority)[0]


class BandTest(ApprenticeTestCase):
    def test_band_high_boundaries(self):
        self.assertEqual(_band(n=5, sessions=3, repos=1), "high")
        self.assertEqual(_band(n=4, sessions=3, repos=2), "medium")
        self.assertEqual(_band(n=5, sessions=2, repos=2), "high")
        self.assertEqual(_band(n=5, sessions=2, repos=1), "medium")
        self.assertEqual(_band(n=5, consistency=0.9), "high")
        self.assertEqual(_band(n=5, consistency=0.89), "medium")
        self.assertEqual(_band(n=5, reversal_30d=True, has_reversal=True), "low")
        self.assertEqual(_band(n=5, reversal_30d=True), "medium")

    def test_band_medium_n3_or_n2_strong(self):
        self.assertEqual(_band(n=3, sessions=1), "medium")
        self.assertEqual(_band(n=2, sessions=1), "low")
        self.assertEqual(_band(n=2, sessions=1, path="strong-marker"), "medium")
        self.assertEqual(_band(n=1, sessions=1, path="strong-marker"), "low")

    def test_reversal_drops_one_band(self):
        self.assertEqual(_band(n=5, has_reversal=True, reversal_30d=False), "medium")
        self.assertEqual(_band(n=3, sessions=1, has_reversal=True), "low")
        self.assertEqual(_band(n=1, sessions=1, has_reversal=True), "low")

    def test_split_raise_to_majority_band(self):
        self.assertEqual(_band(n=3, sessions=1, split_purity=0.9, majority="high"), "high")
        self.assertEqual(_band(n=3, sessions=1, split_purity=0.89, majority="high"), "medium")
        self.assertEqual(_band(n=3, sessions=1, split_purity=1.0, majority="low"), "medium")

    def test_split_raise_applies_after_reversal_drop(self):
        self.assertEqual(_band(n=5, has_reversal=True, split_purity=0.95, majority="high"), "high")

    def test_inputs_dict(self):
        value, inputs = confidence.band(5, 3, 1, 1.0, False, False, "x", None, None)
        self.assertEqual(value, "high")
        self.assertEqual(inputs, {"n": 5, "sessions": 3, "repos": 1, "consistency": 1.0,
                                  "reversal_30d": False, "has_reversal": False, "path": "x",
                                  "split_purity": None, "majority_band": None})
        self.assertEqual(confidence.BANDS, ("low", "medium", "high"))

    def test_majority_band_recipe(self):
        rows = [{"session_id": "s%d" % (i % 3), "repo": "acme/widgets"} for i in range(6)]
        self.assertEqual(confidence.majority_band(rows, None), "high")
        self.assertEqual(confidence.majority_band(rows[:2], None), "low")
        self.assertEqual(confidence.majority_band(rows[:2], "strong-marker"), "medium")


class ConsistencyTest(ApprenticeTestCase):
    def test_majority_share(self):
        cluster = {"answers": Counter({"A": 9, "B": 1})}
        self.assertEqual(confidence.consistency(cluster, None), 0.9)

    def test_split_agreement_share(self):
        cluster = {"answers": Counter({"A": 6, "B": 4})}
        split = {"dep_side": (3, 4), "maj_side": (6, 6)}
        self.assertEqual(confidence.consistency(cluster, split), 0.9)

    def test_correction_group_is_unassessed(self):
        group = {"pairs": []}
        self.assertEqual(confidence.consistency(group, None), 1.0)
        self.assertFalse(confidence.assessed(group))
        self.assertTrue(confidence.assessed({"answers": Counter({"A": 1})}))

    def test_empty_answers_is_zero(self):
        self.assertEqual(confidence.consistency({"answers": Counter()}, None), 0.0)
