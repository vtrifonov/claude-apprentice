import io
import json
import os
import contextlib
import warnings

from apprentice import config, scrub, status
from tests.base import ApprenticeTestCase


class ExtraRedactPatternsTest(ApprenticeTestCase):
    def with_config(self, data):
        self.write(os.path.join(self.home, "config.json"), json.dumps(data))
        return config.load_config(self.home)

    def log(self):
        try:
            with open(os.path.join(self.home, "hook-errors.log"), "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    def test_invalid_entries_dropped_with_warning(self):
        cfg = self.with_config({"extra_redact_patterns": ["(Globex", 7, "Globex[", "Initech"]})
        self.assertEqual(cfg["extra_redact_patterns"], ["Initech"])
        self.assertIn("rule: extra-redact-patterns", self.log())

    def test_valid_list_logs_nothing(self):
        cfg = self.with_config({"extra_redact_patterns": ["Initech"]})
        self.assertEqual(cfg["extra_redact_patterns"], ["Initech"])
        self.assertEqual(self.log(), "")

    def test_not_a_list_falls_back_to_default(self):
        cfg = self.with_config({"extra_redact_patterns": "Initech"})
        self.assertEqual(cfg["extra_redact_patterns"], [])
        self.assertIn("rule: extra-redact-patterns", self.log())

    def test_patterns_ignore_case(self):
        out, flags = scrub.scrub("ask initech and INITECH", {"extra_redact_patterns": ["Initech"]})
        self.assertEqual(out, "ask <redacted:custom> and <redacted:custom>")
        self.assertIn("redacted:custom", flags)

    def test_status_counts_ignored_patterns(self):
        self.with_config({"extra_redact_patterns": ["(Globex", "Initech[", "Initech"]})
        text = status.report(self.home, {}, self.fake_home)
        self.assertIn("⚠ extra_redact_patterns: 2 invalid patterns ignored", text)
        self.with_config({"extra_redact_patterns": ["Initech"]})
        self.assertNotIn("⚠ extra_redact_patterns", status.report(self.home, {}, self.fake_home))

    def test_future_warning_pattern_prints_nothing(self):
        err = io.StringIO()
        with warnings.catch_warnings():
            warnings.simplefilter("default")
            with contextlib.redirect_stderr(err):
                cfg = self.with_config({"extra_redact_patterns": ["[[zq]"], "machine_prompt_patterns": ["[[zr]"]})
                scrub.scrub("text", cfg)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(cfg["extra_redact_patterns"], ["[[zq]"])
