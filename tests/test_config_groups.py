import json
import os

from apprentice import config
from tests.base import ApprenticeTestCase


class ConfigGroupsTest(ApprenticeTestCase):
    def _labels_path(self):
        return os.path.join(self.home, "state", "group-labels.json")

    def test_current_profile_default_when_unset(self):
        self.assertEqual(config.current_profile({}), "default")
        self.assertEqual(config.current_profile(), "default")

    def test_current_profile_default_when_empty(self):
        self.assertEqual(config.current_profile({"CLAUDE_CONFIG_DIR": ""}), "default")

    def test_current_profile_default_when_dir_is_dot_claude(self):
        path = os.path.join(self.fake_home, ".claude")
        os.makedirs(path)
        self.assertEqual(config.current_profile({"CLAUDE_CONFIG_DIR": path}), "default")

    def test_current_profile_basename(self):
        env = {"CLAUDE_CONFIG_DIR": "/x/.claude-acme-alice/"}
        self.assertEqual(config.current_profile(env), ".claude-acme-alice")

    def test_current_profile_never_returns_unknown(self):
        self.assertEqual(config.current_profile({"CLAUDE_CONFIG_DIR": "/x/unknown"}), "unknown-dir")

    def test_assign_labels_name_tie_break(self):
        seen = {"p_b": "2026-01-01T00:00:00Z", "p_a": "2026-01-01T00:00:00Z"}
        self.assertEqual(config.assign_group_labels(self.home, seen), {"p_a": "group-1", "p_b": "group-2"})

    def test_assign_labels_corrupt_file_logs_and_does_not_renumber(self):
        self.write(self._labels_path(), "{not json")
        labels = config.assign_group_labels(self.home, {"p_a": "2026-01-01T00:00:00Z"})
        self.assertEqual(labels, {})
        with open(self._labels_path(), "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{not json")
        with open(os.path.join(self.home, "hook-errors.log"), "r", encoding="utf-8") as fh:
            self.assertIn("group-labels-corrupt", fh.read())

    def test_group_profiles_filters_explicit_profile_with_label(self):
        cfg = {"account_groups": {"team": ["p1"]}, "unknown_group": None}
        labels = {"p1": "group-1", "p3": "group-1"}
        self.assertEqual(config.group_profiles("group-1", cfg, labels), ["p3"])

    def test_assign_labels_first_seen_order(self):
        seen = {"p_b": "2026-02-01T00:00:00Z", "p_a": "2026-01-01T00:00:00Z"}
        labels = config.assign_group_labels(self.home, seen)
        self.assertEqual(labels, {"p_a": "group-1", "p_b": "group-2"})

    def test_assign_labels_stable_and_appends(self):
        config.assign_group_labels(self.home, {"p_a": "2026-01-01T00:00:00Z", "p_b": "2026-02-01T00:00:00Z"})
        labels = config.assign_group_labels(self.home, {
            "p_a": "2026-03-01T00:00:00Z", "p_b": "2026-03-01T00:00:00Z", "p_c": "2026-03-01T00:00:00Z"})
        self.assertEqual(labels, {"p_a": "group-1", "p_b": "group-2", "p_c": "group-3"})
        with open(self._labels_path(), "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), labels)
        self.assertEqual(config.read_group_labels(self.home), labels)

    def test_read_group_labels_tolerates_missing_and_malformed(self):
        self.assertEqual(config.read_group_labels(self.home), {})
        self.write(self._labels_path(), "{not json")
        self.assertEqual(config.read_group_labels(self.home), {})

    def test_group_of_profile_explicit_beats_label(self):
        cfg = {"account_groups": {"team": ["p1"]}, "unknown_group": None}
        self.assertEqual(config.group_of_profile("p1", cfg, {"p1": "group-1"}), "team")
        self.assertEqual(config.group_of_profile("p2", cfg, {"p2": "group-2"}), "group-2")
        self.assertIsNone(config.group_of_profile("p3", cfg, {}))

    def test_unknown_uses_unknown_group_or_none(self):
        cfg = {"account_groups": {"team": ["p1"]}, "unknown_group": "team"}
        self.assertEqual(config.group_of_profile("unknown", cfg, {}), "team")
        cfg["unknown_group"] = None
        self.assertIsNone(config.group_of_profile("unknown", cfg, {}))

    def test_group_profiles(self):
        cfg = {"account_groups": {"team": ["p1", "p2"]}, "unknown_group": None}
        labels = {"p3": "group-1", "p4": "group-2"}
        self.assertEqual(config.group_profiles("team", cfg, labels), ["p1", "p2"])
        self.assertEqual(config.group_profiles("group-1", cfg, labels), ["p3"])

    def test_text_visible_false_when_no_current_group(self):
        cfg = {"account_groups": {}, "unknown_group": None}
        self.assertFalse(config.text_visible("p1", None, cfg, {"p1": "group-1"}))
        self.assertFalse(config.text_visible("p1", None, cfg, {}))

    def test_text_visible_same_group(self):
        cfg = {"account_groups": {"team": ["p1", "p2"]}, "unknown_group": None}
        self.assertTrue(config.text_visible("p2", "team", cfg, {}))

    def test_text_visible_false_across_label_and_explicit_group(self):
        cfg = {"account_groups": {"team": ["p1"]}, "unknown_group": None}
        labels = {"p3": "group-1"}
        self.assertFalse(config.text_visible("p3", "team", cfg, labels))
        self.assertFalse(config.text_visible("p1", "group-1", cfg, labels))
