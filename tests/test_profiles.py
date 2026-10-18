import json
import os
import subprocess
from unittest import mock

from apprentice import cluster, config, departures, distill, events
from tests.base import REPO_ROOT, ApprenticeTestCase, isolated_env
from tests.fixtures.events import profiles
from tests.fixtures.events.profiles import ALICE, BOB, DEFAULT, UNKNOWN

_SCRIPT = os.path.join(REPO_ROOT, "scripts", "distill.py")
WORK = {"account_groups": {"work": [ALICE, BOB]}}


def listed(pack):
    return pack["candidates"] + pack["weak"] + pack["covered"] + pack["missing"]


class ProfilesTest(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = profiles.build(self.home)

    def configure(self, data):
        self.write(os.path.join(self.home, "config.json"), json.dumps(data))

    def run_as(self, profile):
        env = dict(os.environ)
        env.pop("CLAUDE_CONFIG_DIR", None)
        if profile != DEFAULT:
            env["CLAUDE_CONFIG_DIR"] = os.path.join(self.fake_home, profile)
        with mock.patch.dict(os.environ, env, clear=True):
            pack, private = distill.build_pack(self.home, env, config.load_config(self.home), None, None, None,
                                               False, 10, self.fake_home)
            paths = distill.write_pack(self.home, pack, private)
        text = ""
        for path in paths:
            with open(path, encoding="utf-8") as fh:
                text += fh.read()
        return pack, text

    def evidence(self, pack):
        return {event_id for cand in listed(pack) for event_id in cand["evidence_ids"]}

    def by_stem(self, pack, stem):
        found = [cand for cand in listed(pack) if cand["stem"] == stem]
        self.assertEqual(len(found), 1, stem)
        return found[0]

    def test_default_profile_counts_equal_all_events_and_no_work_or_unknown_markers(self):
        self.configure(WORK)
        pack, text = self.run_as(DEFAULT)
        for marker in ("zorbalice", "zorbbob", "zorbunknown"):
            self.assertNotIn(marker, text)
        self.assertIn("zorbdefault", text)
        inputs = pack["inputs"]
        self.assertEqual(inputs["events_current_group"] + inputs["events_other_groups"], inputs["events"])
        self.assertEqual(inputs["events"], sum(1 for _ in events.iter_events(self.home)))
        self.assertEqual(inputs["profile"], DEFAULT)
        self.assertFalse(inputs["unknown_text_visible"])
        cfg = config.load_config(self.home)
        cleaned = distill.clean(list(events.iter_events(self.home)), cfg, None, {})
        model = [c for c in cluster.build_ask_clusters(cleaned, cfg, lambda event: True) if c["stem"] == "use fable?"]
        expected = departures.sentence(departures.analyze(model[0])["majority"])
        self.assertIn("used_pct ≥ 80", expected)
        cand = self.by_stem(pack, "use fable?")
        self.assertEqual(cand["departure"], expected)
        self.assertEqual(cand["band"], "high")
        self.assertEqual(cand["band_inputs"]["n"], 8)
        self.assertEqual(pack["remaining"]["hint"], distill.WITHHELD_HINT)

    def test_unknown_group_personal_shows_unknown_not_work(self):
        self.configure({"account_groups": {"work": [ALICE, BOB], "personal": [DEFAULT]}, "unknown_group": "personal"})
        pack, text = self.run_as(DEFAULT)
        self.assertIn("zorbunknown", text)
        self.assertIn("zorbdefault", text)
        self.assertNotIn("zorbalice", text)
        self.assertNotIn("zorbbob", text)
        self.assertEqual(pack["inputs"]["group"], "personal")
        self.assertTrue(pack["inputs"]["unknown_text_visible"])
        self.assertNotIn("hint", pack["remaining"])

    def test_work_profile_shows_both_work_markers_not_default(self):
        self.configure(WORK)
        pack, text = self.run_as(ALICE)
        self.assertIn("zorbalice", text)
        self.assertIn("zorbbob", text)
        self.assertNotIn("zorbdefault", text)
        self.assertNotIn("zorbunknown", text)
        self.assertEqual(pack["inputs"]["group"], "work")
        self.assertEqual(pack["inputs"]["group_profiles"], [ALICE, BOB])

    def test_work_only_correction_group_withheld(self):
        self.configure(WORK)
        pack, _ = self.run_as(DEFAULT)
        shown = self.evidence(pack)
        for profile in (ALICE, BOB, UNKNOWN):
            self.assertFalse(shown & set(self.fixture["ids"]["corrections"][profile]), profile)
        self.assertTrue(shown & set(self.fixture["ids"]["corrections"][DEFAULT]))
        self.assertGreaterEqual(pack["remaining"]["text_withheld"], 1)

    def test_stem_and_label_leak_withheld(self):
        self.configure(WORK)
        pack, text = self.run_as(DEFAULT)
        self.assertFalse(self.evidence(pack) & set(self.fixture["ids"]["adhoc_work"]))
        self.assertFalse(any("zorbalice" in (cand["stem"] or "") for cand in listed(pack)))
        self.assertGreaterEqual(pack["remaining"]["text_withheld"], 1)
        self.assertNotIn("zorbalice", text)
        self.assertNotIn("layout", text.lower())

    def test_work_only_skill_picker_listed_with_empty_quotes_and_withheld_count(self):
        self.configure(WORK)
        pack, text = self.run_as(DEFAULT)
        cand = self.by_stem(pack, "quick mode?")
        self.assertEqual(cand["skill"], "oneshot-pipeline")
        self.assertEqual(cand["quotes"], [])
        self.assertEqual(cand["free_texts"], [])
        self.assertEqual(cand["display_questions"], [])
        self.assertEqual(cand["text_withheld"], {"events": 3, "free_text": 1})
        self.assertNotIn("zorbbob", text)

    def test_adhoc_cluster_with_one_default_event_listed(self):
        self.configure(WORK)
        pack, text = self.run_as(DEFAULT)
        cand = self.by_stem(pack, "split the rollout into steps?")
        self.assertEqual(cand["free_texts"], ["zorbdefault wants a single rollout"])
        self.assertEqual(cand["text_withheld"], {"events": 2, "free_text": 1})
        self.assertNotIn("zorbalice", text)

    def test_opaque_labels_assigned_in_first_seen_order_and_stable(self):
        pack, text = self.run_as(DEFAULT)
        order = sorted((ALICE, BOB, DEFAULT), key=lambda profile: self.fixture["first_seen"][profile])
        expected = {profile: "group-%d" % (index + 1) for index, profile in enumerate(order)}
        self.assertEqual(config.read_group_labels(self.home), expected)
        self.assertEqual(pack["inputs"]["group"], expected[DEFAULT])
        self.run_as(DEFAULT)
        self.assertEqual(config.read_group_labels(self.home), expected)
        extra = ".claude-acme-extra"
        events.append_event(self.home, profiles.correction(DEFAULT, 40, "extra-0", text="no, extra words here"))
        record = profiles.correction(DEFAULT, 41, "extra-1")
        record["profile"] = extra
        events.append_event(self.home, record)
        pack, text = self.run_as(DEFAULT)
        labels = config.read_group_labels(self.home)
        self.assertEqual(labels[extra], "group-4")
        for profile in (ALICE, BOB, DEFAULT):
            self.assertEqual(labels[profile], expected[profile])
        for name in (ALICE, BOB, extra):
            self.assertNotIn(name, text)

    def test_contradictory_account_groups_falls_back(self):
        self.configure({"account_groups": {"work": [ALICE, BOB], "personal": [ALICE]}})
        pack, text = self.run_as(ALICE)
        self.assertIn("zorbalice", text)
        self.assertNotIn("zorbbob", text)
        self.assertNotIn("zorbdefault", text)
        self.assertTrue(pack["inputs"]["group"].startswith("group-"))
        self.assertEqual(pack["inputs"]["group_profiles"], [ALICE])

    def test_removed_flags_usage_error(self):
        env = isolated_env()
        env.update({"APPRENTICE_HOME": self.home, "HOME": self.fake_home})
        for flag in ("--include-unknown", "--all-profiles", "--all-profiles=1"):
            result = subprocess.run(["/usr/bin/python3", "-I", "-S", _SCRIPT, flag], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, universal_newlines=True)
            self.assertEqual(result.returncode, 2, flag)
            self.assertEqual(result.stdout, "")
        self.assertFalse(os.listdir(os.path.join(self.home, "review")) if os.path.isdir(
            os.path.join(self.home, "review")) else [])
