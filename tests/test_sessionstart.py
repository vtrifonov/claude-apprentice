import json
import os
import stat
import time
from unittest import mock

from apprentice import capture, config
from tests.base import ApprenticeTestCase
from tests.fixtures import worktrees as tree_fixture

_NAME = "CLAUDE.local.md"


class SessionStartTest(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.env = {"APPRENTICE_HOME": self.home}

    def p(self, *parts):
        return os.path.join(self.home, *parts)

    def start(self, cwd=None, cfg=None, env=None):
        payload = {"session_id": "sess-1", "cwd": cwd or self.fake_home}
        merged = dict(config.DEFAULTS)
        merged.update(cfg or {})
        return capture.handle_session_start(payload, self.env if env is None else env, self.home, merged)

    def month_name(self):
        return config.utc_now_iso()[:7] + ".jsonl"

    def test_creates_dirs_and_month_file_0600(self):
        self.assertIsNone(self.start())
        path = self.p("events", self.month_name())
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertTrue(os.path.isdir(self.p("state", "pending-ask")))

    def test_retention_deletes_old_months(self):
        config.ensure_dirs(self.home)
        old = self.write(self.p("events", "2020-01.jsonl"), "{}\n")
        self.start()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(self.p("events", self.month_name())))
        old = self.write(self.p("events", "2020-01.jsonl"), "{}\n")
        self.start(cfg={"retention_months": 0})
        self.assertTrue(os.path.exists(old))

    def test_error_log_truncated_to_last_128k(self):
        log = self.p("hook-errors.log")
        lines = ["%06d line\n" % i for i in range(40000)]
        data = "".join(lines)
        self.write(log, data)
        self.start()
        with open(log, "rb") as fh:
            kept = fh.read()
        self.assertLessEqual(len(kept), 131072)
        self.assertGreater(len(kept), 131072 - 64)
        self.assertTrue(kept.startswith(b"0"))
        self.assertTrue(kept.endswith(b"039999 line\n"))
        self.assertTrue(data.encode("utf-8").endswith(kept))

    def test_prunes_old_markers_keeps_fresh(self):
        config.ensure_dirs(self.home)
        stale = self.write(self.p("state", "pending-ask", "old.json"), "{}")
        fresh = self.write(self.p("state", "pending-ask", "new.json"), "{}")
        past = time.time() - 3700
        os.utime(stale, (past, past))
        self.start()
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh))

    def test_writes_plugin_root_and_last_sessionstart(self):
        self.start()
        with open(self.p("state", "plugin-root"), "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), os.path.dirname(os.path.dirname(os.path.realpath(capture.__file__))))
        with open(self.p("state", "last-sessionstart.json"), "r", encoding="utf-8") as fh:
            info = json.load(fh)
        self.assertEqual(sorted(info), ["cwd", "group", "is_worktree", "link", "tone_line", "ts"])
        self.assertEqual(info["cwd"], self.fake_home)
        self.assertEqual(info["link"], "not-a-repo")
        self.assertFalse(info["is_worktree"])
        self.assertFalse(info["tone_line"])

    def test_tone_line_only_when_group_file_exists(self):
        config.ensure_dirs(self.home)
        self.write(self.p("state", "group-labels.json"), json.dumps({"default": "group-1"}))
        tone = self.write(self.p("profile", "tone-group-1.md"), "tone\n")
        out = self.start()
        self.assertEqual(out, "apprentice tone file for this session (account group group-1): %s\n" % tone)
        os.remove(self.p("state", "group-labels.json"))
        self.assertIsNone(self.start())
        self.assertFalse(os.path.exists(self.p("state", "group-labels.json")))

    def test_link_output_header_once(self):
        tree = tree_fixture.build(self.fake_home)
        self.write(os.path.join(tree["main"], _NAME), "# alice rules\n")
        self.write(os.path.join(tree["main"], ".git", "info", "exclude"), _NAME + "\n")
        out = self.start(cwd=tree["wt1"])
        self.assertEqual(out, "Personal repo rules from CLAUDE.local.md (linked by apprentice):\n# alice rules\n\n")
        self.assertIsNone(self.start(cwd=tree["wt1"]))
        with open(self.p("state", "last-sessionstart.json"), "r", encoding="utf-8") as fh:
            info = json.load(fh)
        self.assertEqual(info["link"], "present")
        self.assertTrue(info["is_worktree"])

    def errors(self):
        try:
            with open(self.p("hook-errors.log"), "r", encoding="utf-8") as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def test_plugin_root_blocked_still_prints_tone_line(self):
        config.ensure_dirs(self.home)
        os.makedirs(self.p("state", "plugin-root"))
        self.write(self.p("state", "group-labels.json"), json.dumps({"default": "group-1"}))
        tone = self.write(self.p("profile", "tone-group-1.md"), "tone\n")
        out = self.start()
        self.assertEqual(out, "apprentice tone file for this session (account group group-1): %s\n" % tone)
        lines = self.errors()
        self.assertEqual(len(lines), 1)
        self.assertIn("session-start", lines[0])

    def test_every_housekeeping_step_failing_keeps_link_and_tone(self):
        tree = tree_fixture.build(self.fake_home)
        self.write(os.path.join(tree["main"], _NAME), "# alice rules\n")
        self.write(os.path.join(tree["main"], ".git", "info", "exclude"), _NAME + "\n")
        config.ensure_dirs(self.home)
        self.write(self.p("state", "group-labels.json"), json.dumps({"default": "group-1"}))
        tone = self.write(self.p("profile", "tone-group-1.md"), "tone\n")
        boom = OSError("disk full")
        with mock.patch.object(capture.config, "ensure_dirs", side_effect=boom), \
                mock.patch.object(capture, "_prune_events", side_effect=boom), \
                mock.patch.object(capture, "_truncate_error_log", side_effect=boom), \
                mock.patch.object(capture, "_prune_markers", side_effect=boom), \
                mock.patch.object(capture, "_write_atomic", side_effect=boom):
            out = self.start(cwd=tree["wt1"])
        self.assertEqual(out, "Personal repo rules from CLAUDE.local.md (linked by apprentice):\n# alice rules\n\n"
                         "apprentice tone file for this session (account group group-1): %s\n" % tone)
        lines = self.errors()
        self.assertEqual(len(lines), 6)
        self.assertTrue(all("session-start" in line and "disk full" in line for line in lines))

    def test_explicit_group_slug(self):
        config.ensure_dirs(self.home)
        tone = self.write(self.p("profile", "tone-work-eu.md"), "tone\n")
        out = self.start(cfg={"account_groups": {"Work EU": ["default"]}})
        self.assertEqual(out, "apprentice tone file for this session (account group Work EU): %s\n" % tone)
