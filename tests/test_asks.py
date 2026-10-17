import datetime
import json

from apprentice import asks, config
from tests.base import ApprenticeTestCase
from tests.fixtures import worktrees

_PHRASINGS = [
    ("74% of the weekly limit used, 2 working days left, 13%/day available (pace=tight)",
     74.0, 13.0, 2.0, "tight"),
    ("used 4% of weekly limit, 5 working days until reset, 19% per day (pace=ample)",
     4.0, 19.0, 5.0, "ample"),
    ("weekly usage 65%, 1 working day left, 35%/day (pace=low)", 65.0, 35.0, 1.0, "low"),
    ("weekly usage is 87% used, 3 working days left, 4% per day, pace=tight", 87.0, 4.0, 3.0, "tight"),
    ("61% used; 2 working days left; 20%/day; pace=tight", 61.0, 20.0, 2.0, "tight"),
    ("Budget: 50% of the weekly limit used, 4 working days left, 12%/day available (pace=ample), "
     "resets 2030-01-07 04:00 +02:00", 50.0, 12.0, 4.0, "ample"),
]


def _tables():
    return config.effective_tables(dict(config.DEFAULTS))


def _cfg():
    return dict(config.DEFAULTS)


def _q(question, options=None, header="H", multi=False):
    out = {"question": question, "header": header, "multiSelect": multi}
    if options is not None:
        out["options"] = options
    return out


class AsksTest(ApprenticeTestCase):
    def test_six_budget_phrasings_same_keys(self):
        for text, used, per_day, days, pace in _PHRASINGS:
            numbers, other, _ = asks.extract_numbers(text, _tables())
            self.assertTrue({"used_pct", "per_day_pct", "working_days_left", "pace"} <= set(numbers), text)
            self.assertEqual(numbers["used_pct"], used, text)
            self.assertEqual(numbers["per_day_pct"], per_day, text)
            self.assertEqual(numbers["working_days_left"], days, text)
            self.assertEqual(numbers["pace"], pace, text)
            self.assertEqual(other, [], text)

    def test_reset_at_kept_out_of_numbers_other(self):
        text = "50% used, resets 2030-01-07 04:00 +02:00 and 7 things"
        numbers, other, reset_at = asks.extract_numbers(text, _tables())
        self.assertEqual(reset_at, "2030-01-07 04:00 +02:00")
        self.assertEqual(other, [7.0])
        self.assertEqual(numbers["used_pct"], 50.0)

    def test_keyval_not_glued(self):
        numbers, _, _ = asks.extract_numbers("available (pace=tight)", _tables())
        self.assertEqual(numbers, {"pace": "tight"})

    def test_lines_files_kb(self):
        numbers, other, _ = asks.extract_numbers("120 changed lines in 7 files, 3.5 KB", _tables())
        self.assertEqual(numbers, {"lines": 120.0, "files": 7.0, "kb": 3.5})
        self.assertEqual(other, [])

    def test_risk_negated(self):
        self.assertEqual(asks.risk_words("no trust/persistence trigger", [], _tables()),
                         ([], ["persistence", "trust"]))

    def test_risk_present(self):
        self.assertEqual(asks.risk_words("trips the deploy trigger", [], _tables()), (["deploy"], []))

    def test_risk_union_over_descriptions_independent_of_pick(self):
        questions = [_q("Mode?", [{"label": "Standard", "description": "touches the deploy path"},
                                  {"label": "Quick", "description": "no deploy trigger"}])]
        when = datetime.datetime(2030, 1, 7, 9, 0)
        first, _, _ = asks.ask_payload("t1", questions, {"Mode?": "Standard"}, None, _cfg(), when)
        second, _, _ = asks.ask_payload("t1", questions, {"Mode?": "Quick"}, None, _cfg(), when)
        self.assertEqual(first["risk_words"], second["risk_words"])
        self.assertEqual(first["risk_words"], ["deploy"])
        self.assertEqual(first["risk_words_negated"], [])
        self.assertEqual(asks.risk_words("Mode?", ["no deploy trigger", "ships to deploy"], _tables()),
                         (["deploy"], []))

    def test_skill_hint_models_budget(self):
        self.assertEqual(asks.skill_hint("Models", "Use fable? Budget: 50% used", _tables()),
                         "oneshot-pipeline")

    def test_skill_hint_approve_go_autonomous(self):
        self.assertEqual(asks.skill_hint("Approve", "Go autonomous on this plan?", _tables()),
                         "oneshot-pipeline")

    def test_skill_hint_pr_size_header(self):
        self.assertEqual(asks.skill_hint("PR size budget", "Something unrelated entirely", _tables()),
                         "oneshot-pipeline")

    def test_skill_hint_config_extension(self):
        cfg = dict(config.DEFAULTS)
        cfg["skill_hints"] = [{"phrase": "ship the thing", "fields": "stem", "skill": "ship-skill"}]
        tables = config.effective_tables(cfg)
        self.assertEqual(asks.skill_hint("X", "Ship  the\nthing now?", tables), "ship-skill")

    def test_skill_hint_none(self):
        self.assertIsNone(asks.skill_hint("Colour", "Which colour do you prefer?", _tables()))

    def test_ticket_precedence(self):
        self.assertEqual(asks.find_ticket(None, "see XX-123 and YY-9", "ZZ-5"), "XX-123")
        self.assertIsNone(asks.find_ticket("nothing", None))
        tree = worktrees.build(self.home)
        ctx = asks.departure_context(tree["wt1"], "AB-77")
        self.assertEqual(ctx["ticket"], "XX-123")
        ctx = asks.departure_context(tree["main"] + "/CD-5", "AB-77")
        self.assertEqual(ctx["ticket"], "CD-5")
        self.assertEqual(asks.departure_context(None, "AB-77")["ticket"], "AB-77")

    def test_build_questions_partial_and_no_options(self):
        questions = [
            _q("Pick one?", [{"label": "A (Recommended)", "description": "d"}, {"label": "B"}]),
            _q("No options here?"),
        ]
        built, partial = asks.build_questions(questions, {"Pick one?": "A"}, _cfg())
        self.assertTrue(partial)
        self.assertIs(built[0]["options"][0]["recommended"], True)
        self.assertIs(built[0]["options"][1]["recommended"], False)
        self.assertEqual(built[0]["answer"]["labels"], ["A"])
        self.assertEqual(built[1]["options"], [])
        self.assertIsNone(built[1]["answer"])
        done, partial2 = asks.build_questions(questions[:1], {"Pick   one?": "B"}, _cfg())
        self.assertFalse(partial2)
        self.assertEqual(done[0]["answer"]["labels"], ["B"])
        _, flags, partial3 = asks.ask_payload("t", questions, None, None, _cfg(), datetime.datetime(2030, 1, 7, 9))
        self.assertTrue(partial3)
        self.assertNotIn("partial", flags)
        self.assertFalse(any("partial" in f for f in flags))

    def test_ask_payload_fields_and_raw_cap(self):
        questions = [_q("Use fable? 50% used, 2 working days left", [{"label": "Yes"}], header="Models")]
        when = datetime.datetime(2030, 1, 7, 14, 30)
        payload, flags, partial = asks.ask_payload("tu1", questions, {"Use fable? 50% used, 2 working days left": "Yes"},
                                                   "x" * 5000, _cfg(), when)
        self.assertFalse(partial)
        self.assertEqual(flags, [])
        self.assertEqual(payload["tool_use_id"], "tu1")
        self.assertEqual(payload["skill_hint"], "oneshot-pipeline")
        self.assertEqual(payload["numbers"], {"used_pct": 50.0, "working_days_left": 2.0})
        self.assertEqual(payload["numbers_other"], [])
        self.assertIsNone(payload["reset_at"])
        self.assertEqual(payload["hour_local"], 14)
        self.assertEqual(payload["weekday"], 0)
        self.assertEqual(len(payload["raw_response"]), 4000)
        as_dict, _, _ = asks.ask_payload("tu2", questions, None, {"a": 1}, _cfg(), when)
        self.assertEqual(as_dict["raw_response"], json.dumps({"a": 1}))
        _, flags2, _ = asks.ask_payload("tu3", [_q("token ghp_" + "a" * 36 + "?")], None, None, _cfg(), when)
        self.assertTrue(flags2)
        self.assertNotIn("raw_response", asks.ask_payload("t4", [], None, None, _cfg(), when)[0])

    def test_departure_context_from_worktree_fixture(self):
        tree = worktrees.build(self.home)
        ctx = asks.departure_context(tree["wt1"])
        self.assertEqual(ctx, {"repo": "acme/widgets", "branch": "feat/XX-123-x", "ticket": "XX-123"})
        none_ctx = asks.departure_context(None)
        self.assertEqual(none_ctx, {"repo": None, "branch": None, "ticket": None})
