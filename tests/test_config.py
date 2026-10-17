import json
import os
import stat
import unittest

from apprentice import config
from tests.base import ApprenticeTestCase


class ConfigTest(ApprenticeTestCase):
    def _write_config(self, obj):
        text = obj if isinstance(obj, str) else json.dumps(obj)
        self.write(os.path.join(self.home, "config.json"), text)

    def _log_lines(self):
        path = os.path.join(self.home, "hook-errors.log")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().splitlines()

    def test_home_env_override(self):
        self.assertEqual(config.home(), self.home)

    def test_home_default_expands_user(self):
        os.environ.pop("APPRENTICE_HOME", None)
        self.assertEqual(config.home(), os.path.join(self.fake_home, ".claude", "apprentice"))

    def test_ensure_dirs_mode_0700(self):
        config.ensure_dirs(self.home)
        names = ["events", "state", "state/pending-ask", "state/tmp", "state/preclass",
                 "ledger", "review", "profile", "backups"]
        for name in names:
            mode = stat.S_IMODE(os.stat(os.path.join(self.home, name)).st_mode)
            self.assertEqual(mode, 0o700, name)

    def test_defaults_when_missing(self):
        cfg = config.load_config()
        self.assertEqual(cfg["retention_months"], 24)
        self.assertEqual(cfg["header_aliases"], {"model set": "models", "quick mode": "mode"})

    def test_defaults_keys(self):
        self.assertEqual(sorted(config.DEFAULTS), sorted([
            "display_name", "own_email_domains", "skill_hints", "number_patterns",
            "header_aliases", "risk_words", "phase_words", "capture_entrypoints",
            "profile_rules", "account_groups", "unknown_group", "public_skills",
            "worktree_suffix", "extra_redact_patterns", "retention_months"]))
        self.assertEqual(config.DEFAULTS["display_name"], "the user")
        self.assertEqual(config.DEFAULTS["capture_entrypoints"], ["cli", "claude-desktop"])
        self.assertEqual(config.DEFAULTS["worktree_suffix"], "-worktrees")
        self.assertIsNone(config.DEFAULTS["unknown_group"])

    def test_load_config_returns_copy(self):
        cfg = config.load_config()
        cfg["header_aliases"]["x"] = "y"
        cfg["risk_words"].append("z")
        again = config.load_config()
        self.assertNotIn("x", again["header_aliases"])
        self.assertEqual(again["risk_words"], [])

    def test_malformed_logs_once_per_day(self):
        self._write_config("{")
        config.load_config()
        config.load_config()
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("config", lines[0])

    def test_file_keys_override_defaults(self):
        self._write_config({"retention_months": 6, "display_name": "Alice"})
        cfg = config.load_config()
        self.assertEqual(cfg["retention_months"], 6)
        self.assertEqual(cfg["display_name"], "Alice")

    def test_account_groups_valid_kept(self):
        self._write_config({"account_groups": {"work": ["p1"], "home": ["p2", "p3"]}, "unknown_group": "work"})
        cfg = config.load_config()
        self.assertEqual(cfg["account_groups"], {"work": ["p1"], "home": ["p2", "p3"]})
        self.assertEqual(cfg["unknown_group"], "work")
        self.assertEqual(self._log_lines(), [])

    def test_account_groups_not_dict_of_lists_falls_back(self):
        self._write_config({"account_groups": {"a": "p1"}})
        self.assertEqual(config.load_config()["account_groups"], {})
        self.assertEqual(len(self._log_lines()), 1)

    def test_account_groups_profile_in_two_groups_falls_back(self):
        self._write_config({"account_groups": {"a": ["p1"], "b": ["p1"]}})
        self.assertEqual(config.load_config()["account_groups"], {})

    def test_account_groups_listing_unknown_falls_back(self):
        self._write_config({"account_groups": {"a": ["p1", "unknown"]}})
        self.assertEqual(config.load_config()["account_groups"], {})

    def test_account_groups_slug_collision_falls_back(self):
        self._write_config({"account_groups": {"Work EU": ["p1"], "work-eu": ["p2"]}})
        self.assertEqual(config.load_config()["account_groups"], {})
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("slug-collision", lines[0])
        self.assertNotIn("Work EU", lines[0])
        self.assertNotIn("work-eu", lines[0])

    def test_account_groups_reserved_label_name_falls_back(self):
        self._write_config({"account_groups": {"group-1": ["p1"], "team": ["p2"]}})
        self.assertEqual(config.load_config()["account_groups"], {})
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("reserved-group-name", lines[0])
        self.assertNotIn("group-1", lines[0])

    def test_group_slug(self):
        self.assertEqual(config.group_slug("Work Team/EU"), "work-team-eu")
        self.assertEqual(config.group_slug("-x"), "x")

    def test_unknown_group_unknown_name_falls_back_to_none(self):
        self._write_config({"account_groups": {"a": ["p1"]}, "unknown_group": "nope"})
        self.assertIsNone(config.load_config()["unknown_group"])

    def test_unknown_group_implicit_label_allowed(self):
        self._write_config({"unknown_group": "group-3"})
        self.assertEqual(config.load_config()["unknown_group"], "group-3")

    def test_unknown_group_dropped_when_groups_invalid(self):
        self._write_config({"account_groups": {"a": ["p1"], "b": ["p1"]}, "unknown_group": "a"})
        cfg = config.load_config()
        self.assertEqual(cfg["account_groups"], {})
        self.assertIsNone(cfg["unknown_group"])

    def test_retention_months_invalid_falls_back(self):
        for bad in (-1, "12", 1.5, True):
            self._write_config({"retention_months": bad})
            self.assertEqual(config.load_config()["retention_months"], 24, repr(bad))
        self._write_config({"retention_months": 0})
        self.assertEqual(config.load_config()["retention_months"], 0)

    def test_capture_entrypoints_invalid_falls_back(self):
        self._write_config({"capture_entrypoints": ["cli", 3]})
        self.assertEqual(config.load_config()["capture_entrypoints"], ["cli", "claude-desktop"])
        self._write_config({"capture_entrypoints": ["sdk-cli"]})
        self.assertEqual(config.load_config()["capture_entrypoints"], ["sdk-cli"])

    def test_header_aliases_merge_over_defaults(self):
        self._write_config({"header_aliases": {"models?": "models"}})
        aliases = config.load_config()["header_aliases"]
        self.assertEqual(aliases["models?"], "models")
        self.assertEqual(aliases["model set"], "models")
        self.assertEqual(aliases["quick mode"], "mode")

    def test_log_error_truncates_and_writes_last_error(self):
        config.log_error("x", "a" * 400)
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)
        ts, event, message = lines[0].split(" ", 2)
        self.assertEqual(event, "x")
        self.assertEqual(len(message), 300)
        with open(os.path.join(self.home, "state", "last-error.json"), "r", encoding="utf-8") as fh:
            last = json.load(fh)
        self.assertEqual(last["event"], "x")
        self.assertEqual(last["ts"], ts)
        self.assertEqual(len(last["message"]), 300)
        mode = stat.S_IMODE(os.stat(os.path.join(self.home, "hook-errors.log")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_log_error_swallows_failures(self):
        blocker = self.write(os.path.join(self.fake_home, "file"), "x")
        config.log_error("x", "m", home_dir=os.path.join(blocker, "sub"))

    def test_utc_now_iso_shape(self):
        self.assertRegex(config.utc_now_iso(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_parse_iso_accepts_z(self):
        parsed = config.parse_iso("2026-10-17T14:03:11.204Z")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)
        self.assertEqual((parsed.year, parsed.hour, parsed.microsecond), (2026, 14, 204000))

    def test_parse_iso_bad_returns_none(self):
        self.assertIsNone(config.parse_iso("nope"))
        self.assertIsNone(config.parse_iso(None))

    def test_parse_iso_naive_becomes_utc(self):
        parsed = config.parse_iso("2026-10-17T14:03:11")
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)

    def test_number_extractor_table_order(self):
        names = [name for _regex, name, _type in config.NUMBER_EXTRACTORS]
        self.assertEqual(names, ["used_pct", "used_pct", "used_pct", "per_day_pct",
                                 "working_days_left", "lines", "files", "kb", "KEYVAL"])

    def test_tables(self):
        self.assertEqual(len(config.SKILL_HINTS), 7)
        self.assertEqual(config.SKILL_HINTS[0], ("quick mode?", "stem", "oneshot-pipeline"))
        self.assertEqual(config.SKILL_HINTS[6], ("approve|go autonomous", "both", "oneshot-pipeline"))
        self.assertEqual(config.NEGATIONS, ("no", "not", "without"))
        self.assertIn("public api", config.RISK_WORDS)
        self.assertEqual(config.PHASE_WORDS, ["shepherd", "checkpoint", "panel", "lane", "quick mode"])
        self.assertEqual(len(config.STRONG_MARKERS), 6)
        match = config.RESET_AT_RE.search("resets on 2026-10-20 09:00")
        self.assertEqual(match.group(1), "2026-10-20 09:00")
        match = config.NUMBER_EXTRACTORS[0][0].search("42% of the weekly limit used")
        self.assertEqual(match.group(1), "42")

    def test_effective_tables_merge(self):
        cfg = config.load_config()
        cfg["skill_hints"] = [{"phrase": "ship it", "fields": "stem", "skill": "s1"}]
        cfg["number_patterns"] = [{"regex": r"(\d+) widgets", "name": "widgets", "type": "number"}]
        cfg["risk_words"] = ["ledger"]
        tables = config.effective_tables(cfg)
        self.assertEqual(tables["skill_hints"][:7], config.SKILL_HINTS)
        self.assertEqual(tables["skill_hints"][7], ("ship it", "stem", "s1"))
        self.assertEqual(tables["number_extractors"][0][1], "widgets")
        self.assertEqual(tables["number_extractors"][0][2], "number")
        self.assertTrue(tables["number_extractors"][0][0].search("3 WIDGETS"))
        self.assertEqual(len(tables["number_extractors"]), 10)
        self.assertEqual(tables["risk_words"], config.RISK_WORDS + ["ledger"])
        self.assertEqual(tables["header_aliases"], cfg["header_aliases"])
        self.assertEqual(tables["phase_words"], config.PHASE_WORDS)

    def test_effective_tables_skips_bad_entries(self):
        cfg = config.load_config()
        cfg["skill_hints"] = [{"phrase": "x"}, "junk"]
        cfg["number_patterns"] = [{"regex": "(", "name": "n", "type": "number"}, 5]
        tables = config.effective_tables(cfg)
        self.assertEqual(len(tables["skill_hints"]), 7)
        self.assertEqual(len(tables["number_extractors"]), 9)


if __name__ == "__main__":
    unittest.main()
