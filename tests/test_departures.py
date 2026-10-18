from apprentice import departures
from tests.base import ApprenticeTestCase

_FABLE = "fable"
_NO_FABLE = "no fable (opus)"


def _is_dep(row):
    return row["answer"] != _FABLE


def _rows(count, answer, **features):
    return [dict(features, answer=answer) for _ in range(count)]


def _numeric_rows(values, answer, feature="used_pct"):
    return [{feature: float(value), "answer": answer} for value in values]


def _crow(ts, answer, visible=True, ticket=None, **extra):
    row = {
        "ts": ts, "visible": visible, "labels": [answer], "is_other": False, "followed_recommended": None,
        "repo": "acme/widgets", "ticket": ticket, "numbers": {}, "risk_words": [], "skill_hint": None,
        "weekday": None, "hour_local": None, "reset_at": None, "numbers_other": [],
        "risk_words_negated": [],
    }
    row.update(extra)
    return row


def _cluster(rows):
    return {"rows": rows, "n": len(rows)}


class HourBandTest(ApprenticeTestCase):
    def test_hour_band_edges(self):
        band = departures.hour_band
        self.assertEqual([band(0), band(6)], ["night", "night"])
        self.assertEqual([band(7), band(11)], ["morning", "morning"])
        self.assertEqual([band(12), band(17)], ["afternoon", "afternoon"])
        self.assertEqual([band(18), band(23)], ["evening", "evening"])


class FindSplitTest(ApprenticeTestCase):
    def test_numeric_threshold_used_pct_ge_80(self):
        rows = _numeric_rows([20.0 + index * 55.0 / 45 for index in range(46)], _FABLE)  # 20..75
        rows += _numeric_rows([80, 82, 85, 88, 90, 95], _NO_FABLE)
        split = departures.find_split(rows, _is_dep)
        self.assertEqual(split["feature"], "used_pct")
        self.assertEqual(split["op"], ">=")
        self.assertEqual(split["value"], 80)
        self.assertEqual(split["dep_side"], (6, 6))
        self.assertEqual(split["maj_side"], (46, 46))
        self.assertEqual(split["dep_answer"], _NO_FABLE)
        self.assertEqual(split["maj_answer"], _FABLE)
        self.assertEqual(departures.sentence(split),
                         'used_pct ≥ 80 → "no fable (opus)" 6 of 6; otherwise "fable" 46 of 46')

    def test_sentence_exact_form_with_impure_majority_side(self):
        split = {"feature": "used_pct", "op": ">=", "value": 80.0, "dep_side": (6, 6), "maj_side": (44, 46),
                 "purity": 1.95, "dep_answer": "No fable (opus)", "maj_answer": "Fable"}
        self.assertEqual(departures.sentence(split),
                         'used_pct ≥ 80 → "No fable (opus)" 6 of 6; otherwise "Fable" 44 of 46')
        split = dict(split, op="<", value=12.5)
        self.assertTrue(departures.sentence(split).startswith("used_pct < 12.5 → "))
        split = dict(split, op="==", feature="pace", value="slow")
        self.assertTrue(departures.sentence(split).startswith("pace = slow → "))
        self.assertEqual(departures.sentence(None), "no separating feature found")

    def test_categorical_pace_split(self):
        rows = _rows(10, _FABLE, pace="steady") + _rows(3, _NO_FABLE, pace="rushed")
        split = departures.find_split(rows, _is_dep)
        self.assertEqual((split["feature"], split["op"], split["value"]), ("pace", "==", "rushed"))
        self.assertEqual(split["dep_side"], (3, 3))
        self.assertEqual(split["maj_side"], (10, 10))

    def test_boolean_risk_word_split(self):
        rows = _rows(8, _FABLE, **{"risk:migration": False}) + _rows(3, _NO_FABLE, **{"risk:migration": True})
        split = departures.find_split(rows, _is_dep)
        self.assertEqual((split["feature"], split["op"], split["value"]), ("risk:migration", "==", True))

    def test_no_split_when_purity_below_80(self):
        rows = (_rows(5, _FABLE, pace="a") + _rows(2, _NO_FABLE, pace="a")
                + _rows(5, _FABLE, pace="b") + _rows(2, _NO_FABLE, pace="b"))
        self.assertIsNone(departures.find_split(rows, _is_dep))

    def test_minority_side_needs_two_rows(self):
        rows = _rows(10, _FABLE, pace="steady") + _rows(1, _NO_FABLE, pace="rushed")
        self.assertIsNone(departures.find_split(rows, _is_dep))

    def test_missing_numeric_never_satisfies(self):
        rows = _numeric_rows([90, 95], _NO_FABLE) + _rows(2, _NO_FABLE) + _numeric_rows([10, 20, 30], _FABLE)
        split = departures.find_split(rows, _is_dep)
        # the two rows without used_pct can never be on the satisfying side, so no split reaches 80% purity
        # on the other side for ">="; "<" fails because its satisfying side holds no departures
        self.assertIsNone(split)
        only_missing = _rows(3, _NO_FABLE) + _numeric_rows([10, 20, 30, 40], _FABLE)
        self.assertIsNone(departures.find_split(only_missing, _is_dep))

    def test_tie_break_numeric_before_categorical(self):
        rows = []
        for index in range(6):
            rows.append({"used_pct": 10.0 + index, "pace": "steady", "answer": _FABLE})
        for index in range(3):
            rows.append({"used_pct": 90.0 + index, "pace": "rushed", "answer": _NO_FABLE})
        split = departures.find_split(rows, _is_dep)
        self.assertEqual(split["feature"], "used_pct")

    def test_risk_word_only_in_recommended_description_no_split(self):
        rows = (_rows(6, "fast", **{"risk:schema": True}) + _rows(3, "safe", **{"risk:schema": True}))
        recommended = "fast"
        self.assertIsNone(departures.find_split(rows, lambda row: row["answer"] != recommended))
        self.assertIsNone(departures.find_split(rows, _is_dep))


class AnalyzeTest(ApprenticeTestCase):
    def test_analyze_requires_n4_and_two_departures(self):
        small = _cluster([_crow("2030-01-07T04:00:00Z", "a"), _crow("2030-01-07T05:00:00Z", "a"),
                          _crow("2030-01-07T06:00:00Z", "b")])
        self.assertEqual(departures.analyze(small),
                         {"majority": None, "recommended": None, "departure_rows": []})
        one_departure = _cluster([_crow("2030-01-0%dT04:00:00Z" % day, "a") for day in range(1, 5)]
                                 + [_crow("2030-01-07T04:00:00Z", "b")])
        self.assertEqual(departures.analyze(one_departure)["departure_rows"], [])
        rows = [_crow("2030-01-0%dT04:00:00Z" % day, "a", numbers={"used_pct": 20.0 + day}) for day in range(1, 5)]
        rows += [_crow("2030-01-08T04:00:00Z", "b", numbers={"used_pct": 90.0}),
                 _crow("2030-01-09T04:00:00Z", "b", numbers={"used_pct": 95.0})]
        found = departures.analyze(_cluster(rows))
        self.assertEqual(len(found["departure_rows"]), 2)
        self.assertEqual(found["majority"]["feature"], "used_pct")
        self.assertIsNone(found["recommended"])

    def test_departure_rows_withhold_full_ticket_for_invisible(self):
        rows = [_crow("2030-01-0%dT04:00:00Z" % day, "a") for day in range(1, 5)]
        rows += [_crow("2030-01-08T04:00:00Z", "b", visible=True, ticket="XX-123"),
                 _crow("2030-01-09T04:00:00Z", "b", visible=False, ticket="XX-456")]
        found = departures.analyze(_cluster(rows))["departure_rows"]
        self.assertEqual(found[0]["ticket"], "XX-123")
        self.assertEqual(set(found[0]), {"ts", "repo", "ticket", "numbers", "answer", "reset_at",
                                         "risk_words_negated"})
        self.assertEqual(found[0]["answer"], "b")
        self.assertEqual(found[0]["repo"], "acme/widgets")
        # another group's repo and ticket prefix never reach a departure row
        self.assertEqual(set(found[1]), {"ts", "numbers", "answer", "reset_at", "risk_words_negated"})

    def test_invisible_rows_keep_only_named_numbers(self):
        rows = [_crow("2030-01-0%dT04:00:00Z" % day, "a", numbers={"used_pct": 10.0, "zorbnum": 1.0})
                for day in range(1, 5)]
        rows += [_crow("2030-01-0%dT04:00:00Z" % day, "b", visible=False, repo="zorbrepo/zorbname",
                       ticket="ZORBTK-1", numbers={"used_pct": 12.0, "zorbkvkey": "zorbkvval", "zorbnum": 9.0,
                                                   "pace": "zorbpace", "kb": 3.0}) for day in range(5, 8)]
        rows.append(_crow("2030-01-08T04:00:00Z", "b", visible=False, numbers={"pace": "tight"}))
        features = departures.feature_rows(_cluster(rows))
        hidden = features[4]
        self.assertEqual(hidden["numbers"], {"used_pct": 12.0, "kb": 3.0})
        for key in ("zorbkvkey", "zorbnum", "pace", "repo", "ticket_prefix"):
            self.assertNotIn(key, hidden)
        self.assertEqual(features[7]["pace"], "tight")
        found = departures.analyze(_cluster(rows))
        self.assertNotIn("zorb", repr(found).lower())
        self.assertNotIn("zorb", departures.sentence(found["majority"]).lower())


class RecommendedPassTest(ApprenticeTestCase):
    def test_recommended_pass_runs_when_nobody_followed_it(self):
        rec = ["A (Recommended)"]
        rows = [_crow("2030-01-0%dT04:00:00Z" % day, "b", followed_recommended=False, recommended=rec,
                      numbers={"used_pct": 10.0 + day}) for day in range(1, 5)]
        rows += [_crow("2030-01-0%dT04:00:00Z" % day, "a", followed_recommended=True, recommended=rec,
                       numbers={"used_pct": 90.0 + day}) for day in range(5, 7)]
        cl = _cluster(rows)
        self.assertEqual(departures.recommended_answer(cl), "a")
        found = departures.analyze(cl)
        self.assertEqual(found["recommended"]["feature"], "used_pct")
        self.assertEqual(found["recommended"]["dep_answer"], "b")
        self.assertEqual(found["recommended"]["maj_answer"], "a")
        never = _cluster([_crow("2030-01-0%dT04:00:00Z" % day, "b", followed_recommended=False, recommended=rec)
                          for day in range(1, 6)])
        self.assertEqual(departures.recommended_answer(never), "a")
        self.assertEqual(len(departures.analyze(never)["departure_rows"]), 5)

    def test_rows_without_recommended_option_are_skipped(self):
        rec = ["A (Recommended)"]
        rows = [_crow("2030-01-0%dT04:00:00Z" % day, "a", recommended=rec) for day in range(1, 4)]
        rows += [_crow("2030-01-0%dT04:00:00Z" % day, "b", recommended=None) for day in range(4, 9)]
        found = departures.analyze(_cluster(rows))
        self.assertIsNone(found["recommended"])

    def test_recommended_mark_moving_uses_row_specific_set(self):
        rows = [_crow("2030-01-0%dT04:00:00Z" % day, "a", recommended=["A (Recommended)"],
                      numbers={"used_pct": 10.0 + day}) for day in range(1, 5)]
        rows += [_crow("2030-01-0%dT04:00:00Z" % day, "b", recommended=["B (Recommended)"],
                       numbers={"used_pct": 90.0 + day}) for day in range(5, 9)]
        found = departures.analyze(_cluster(rows))
        self.assertIsNone(found["recommended"])  # every row followed its own recommendation


class WeekdayTest(ApprenticeTestCase):
    def test_weekday_is_categorical(self):
        rows = [_crow("2030-01-0%dT04:00:00Z" % day, "a", weekday=day % 5) for day in range(1, 7)]
        rows += [_crow("2030-01-1%dT04:00:00Z" % day, "b", weekday=5 + day % 2) for day in range(3)]
        found = departures.analyze(_cluster(rows))
        self.assertEqual(found["majority"]["op"], "==")
        self.assertEqual(found["majority"]["feature"], "weekday")
        self.assertIn(found["majority"]["value"], ("5", "6"))
        self.assertTrue(departures.sentence(found["majority"]).startswith("weekday = "))
        pinned = [_crow("2030-01-0%dT04:00:00Z" % day, "a", weekday=2) for day in range(1, 7)]
        pinned += [_crow("2030-01-1%dT04:00:00Z" % day, "b", weekday=5) for day in range(3)]
        self.assertTrue(departures.sentence(departures.analyze(_cluster(pinned))["majority"])
                        .startswith('weekday = 5 → "b" 3 of 3'))


class FeatureRowsTest(ApprenticeTestCase):
    def test_feature_rows_shape(self):
        rows = [
            _crow("2030-01-07T04:00:00Z", "a", ticket="XX-123", numbers={"used_pct": 80, "pace": "slow"},
                  risk_words=["migration"], skill_hint="s", weekday="Mon", hour_local=9, numbers_other=[1],
                  reset_at="2030-01-08T00:00:00Z", risk_words_negated=["delete"]),
            _crow("2030-01-07T05:00:00Z", "b", is_other=True, labels=[], repo=None),
            _crow("2030-01-07T06:00:00Z", "x", labels=[]),
        ]
        out = departures.feature_rows(_cluster(rows))
        self.assertEqual(len(out), 2)  # the unanswered row is skipped
        first, second = out
        self.assertEqual(first["used_pct"], 80.0)
        self.assertIsInstance(first["used_pct"], float)
        self.assertEqual(first["pace"], "slow")
        self.assertEqual(first["repo"], "acme/widgets")
        self.assertEqual(first["ticket_prefix"], "XX")
        self.assertIs(first["risk:migration"], True)
        self.assertIs(second["risk:migration"], False)
        self.assertEqual(first["skill_hint"], "s")
        self.assertEqual(first["weekday"], "Mon")
        self.assertEqual(first["hour_band"], "morning")
        self.assertEqual(first["answer"], "a")
        self.assertEqual(second["answer"], "other")
        self.assertEqual(first["reset_at"], "2030-01-08T00:00:00Z")
        self.assertEqual(first["risk_words_negated"], ["delete"])
        self.assertEqual(first["numbers_other"], [1])
        self.assertEqual(first["ticket"], "XX-123")
        self.assertNotIn("repo", second)
        self.assertNotIn("hour_band", second)

    def test_majority_and_recommended_answer(self):
        rows = [dict(answer="b"), dict(answer="a"), dict(answer="a"), dict(answer="b")]
        self.assertEqual(departures.majority_answer(rows), "a")  # ties sort alphabetically
        crows = [_crow("2030-01-0%dT04:00:00Z" % day, "a", recommended=["A (Recommended)"]) for day in range(1, 3)]
        crows.append(_crow("2030-01-05T04:00:00Z", "b", recommended=["A (Recommended)"]))
        self.assertEqual(departures.recommended_answer(_cluster(crows)), "a")
        self.assertIsNone(departures.recommended_answer(_cluster([_crow("2030-01-01T04:00:00Z", "a")])))
