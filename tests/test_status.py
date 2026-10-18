import fcntl
import json
import os
import subprocess
import time

from apprentice import config, events, status
from tests.base import REPO_ROOT, ApprenticeTestCase
from tests.fixtures import worktrees as tree_fixture

_SCRIPT = os.path.join(REPO_ROOT, "scripts", "status.py")
_COMMAND_DOC = os.path.join(REPO_ROOT, "commands", "status.md")
_PLUGIN_KEY = "apprentice@vtrifonov"
_HOUR = 3600.0
_DAY = 86400.0
_DEFAULT_CMD = "env -u CLAUDE_CONFIG_DIR claude plugin %s %s" % (status.ENABLE_VERB, _PLUGIN_KEY)


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def _local(year, month, day, hour, minute):
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


class StatusTestBase(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()

    def event(self, kind="prompt", source="hook", profile="default", entrypoint="cli", ts=None):
        obj = events.envelope(kind, source, "s1", profile, entrypoint, None, None, None, None, None,
                              ts=ts or _iso(self.now))
        events.append_event(self.home, obj)
        return obj

    def month_file(self, month):
        return os.path.join(self.home, "events", month + ".jsonl")

    def profile_dir(self, name, enabled=None, malformed=False, transcript_age=None):
        path = os.path.join(self.fake_home, name)
        if malformed:
            self.write(os.path.join(path, "settings.json"), "{not json")
        else:
            data = {} if enabled is None else {"enabledPlugins": {_PLUGIN_KEY: enabled}}
            self.write(os.path.join(path, "settings.json"), json.dumps(data))
        os.makedirs(os.path.join(path, "projects"), exist_ok=True)
        if transcript_age is not None:
            transcript = self.write(os.path.join(path, "projects", "slug", "x.jsonl"), "{}\n")
            stamp = self.now - transcript_age
            os.utime(transcript, (stamp, stamp))
        return path

    def report(self, env=None, full=False, now=None):
        return status.report(self.home, env or {}, self.fake_home, full=full, now=now)

    def lines(self, text):
        return [line.rstrip() for line in text.splitlines()]

    def row(self, text, first):
        """Whitespace-split plain line starting with `first` (the details block)."""
        found = None
        for line in self.lines(text):
            parts = line.split()
            if parts and parts[0] == first:
                found = parts
        if found is None:
            self.fail("no row starting with %r in:\n%s" % (first, text))
        return found

    def cells(self, text, first):
        """Cells of the Markdown table row whose first cell is `first`."""
        found = None
        for line in self.lines(text):
            if line.startswith("|"):
                parts = [c.strip() for c in line.strip("|").split("|")]
                if parts[0] == first:
                    found = parts
        if found is None:
            self.fail("no table row starting with %r in:\n%s" % (first, text))
        return found

    def head(self, text):
        return text.splitlines()[0]

    def line_with(self, text, needle):
        return [line for line in self.lines(text) if needle in line]

    def hook_error_line(self, age):
        with open(os.path.join(self.home, "hook-errors.log"), "a", encoding="utf-8") as fh:
            fh.write("%sZ capture boom\n" % _iso(self.now - age)[:19])


class CountsTest(StatusTestBase):
    def seed(self):
        self.event("prompt", "hook", "alice", ts="2026-09-10T10:00:00.000Z")
        self.event("prompt", "backfill", "bob", ts="2026-10-10T10:00:00.000Z")
        self.event("ask", "hook", "alice", ts="2026-10-11T10:00:00.000Z")
        self.event("prompt", "hook", "alice", entrypoint=None, ts="2026-10-12T10:00:00.000Z")
        future = json.dumps({"v": 2, "id": "x", "kind": "prompt", "ts": "2026-10-13T10:00:00.000Z"})
        with open(self.month_file("2026-10"), "ab") as fh:
            fh.write(future.encode("utf-8") + b"\n")
            fh.write(b'{"v":1,"id":"torn')

    def test_header_counts_events_and_errors(self):
        self.seed()
        head = self.head(self.report())
        self.assertTrue(head.startswith("**apprentice** "), head)
        self.assertIn("4 events · 0 errors", head)

    def test_counts_table(self):
        self.seed()
        text = self.report()
        self.assertEqual(self.cells(text, "kind"), ["kind", "2026-09", "2026-10", "total"])
        self.assertEqual(self.cells(text, "prompt")[1:], ["1", "2", "3"])
        self.assertEqual(self.cells(text, "ask")[1:], ["0", "1", "1"])

    def test_single_month_has_no_total_column(self):
        self.event("prompt", ts="2026-10-10T10:00:00.000Z")
        self.assertEqual(self.cells(self.report(), "kind"), ["kind", "2026-10"])

    def test_at_most_three_months_newest_last(self):
        for month in ("2026-06", "2026-07", "2026-08", "2026-09"):
            self.event("prompt", ts=month + "-10T10:00:00.000Z")
        header = self.cells(self.report(), "kind")
        self.assertEqual(header, ["kind", "2026-07", "2026-08", "2026-09", "total"])

    def test_kind_labels(self):
        self.event("machine", ts="2026-10-10T10:00:00.000Z")
        self.event("ask_rejected", ts="2026-10-10T10:00:00.000Z")
        text = self.report()
        self.assertEqual(self.cells(text, "command")[1:], ["1"])
        self.assertEqual(self.cells(text, "ask rejected")[1:], ["1"])
        self.assertNotIn("| machine ", text)
        self.assertNotIn("ask_rejected", text)

    def test_no_events_no_kinds_table(self):
        text = self.report()
        self.assertNotIn("| kind ", text)
        self.assertIn("0 events", self.head(text))

    def test_full_counts_details(self):
        self.seed()
        text = self.report(full=True)
        self.assertIn("live 3, backfilled 1, total 4", text)
        self.assertEqual(self.row(text, "alice")[1], "3")
        self.assertEqual(self.row(text, "bob")[1], "1")
        self.assertEqual(self.row(text, "cli")[1], "3")
        self.assertIn("entrypoint_unknown: 1", text)

    def test_skipped_counts_in_details(self):
        self.seed()
        self.assertIn("skipped lines: bad 1, future 1", self.report(full=True))
        self.assertNotIn("skipped lines:", self.report())

    def test_bad_lines_note(self):
        self.seed()
        self.assertTrue(self.line_with(self.report(), "unreadable"))

    def test_no_bad_lines_no_note(self):
        self.event("prompt")
        self.assertFalse(self.line_with(self.report(), "unreadable"))

    def test_last_error_and_log_size(self):
        self.assertFalse(self.line_with(self.report(), "last hook error"))
        config.log_error("capture", "boom", self.home)
        text = self.report()
        self.assertIn("1 error", self.head(text))
        found = self.line_with(text, "last hook error:")
        self.assertEqual(len(found), 1)
        self.assertIn("capture boom", found[0])
        size = os.path.getsize(os.path.join(self.home, "hook-errors.log"))
        self.assertIn("hook-errors.log: %d bytes" % size, self.report(full=True))

    def test_retention_line_only_for_events_past_cutoff(self):
        self.event(ts="2026-09-10T10:00:00.000Z")
        self.assertFalse(self.line_with(self.report(), "retention"))
        self.event(ts="2020-01-10T10:00:00.000Z")
        found = self.line_with(self.report(), "retention")
        self.assertEqual(len(found), 1)
        self.assertIn("1 event", found[0])
        self.assertIn("retention_months 24", found[0])

    def test_retention_details(self):
        self.event(ts="2026-09-10T10:00:00.000Z")
        text = self.report(full=True)
        self.assertIn("retention: 24 months, cutoff month ", text)
        self.assertIn("oldest month kept 2026-09", text)


class VerdictTest(StatusTestBase):
    def healthy(self):
        self.profile_dir(".claude", enabled=True, transcript_age=0)
        self.event(profile="default", ts=_iso(self.now - _HOUR))

    def test_capturing(self):
        self.healthy()
        self.assertIn("✅ capturing", self.head(self.report()))

    def test_disabled_by_env_and_file(self):
        self.healthy()
        self.assertIn("⏸ disabled", self.head(self.report({"APPRENTICE_DISABLE": "1"})))
        self.write(os.path.join(self.home, "disabled"), "")
        self.assertIn("⏸ disabled", self.head(self.report()))

    def test_not_capturing_without_any_live_event(self):
        self.event(source="backfill")
        self.assertIn("❌ not capturing", self.head(self.report()))
        self.assertIn("❌ not capturing", self.head(status.report(self.home, {}, self.fake_home)))

    def test_not_capturing_when_live_is_old_and_transcripts_are_newer(self):
        self.profile_dir(".claude", enabled=True, transcript_age=0)
        self.event(profile="default", ts=_iso(self.now - 8 * _DAY))
        self.assertIn("❌ not capturing", self.head(self.report()))

    def test_old_live_without_newer_transcripts_is_fine(self):
        self.profile_dir(".claude", enabled=True, transcript_age=9 * _DAY)
        self.event(profile="default", ts=_iso(self.now - 8 * _DAY))
        self.assertIn("✅ capturing", self.head(self.report()))

    def test_attention_for_recent_hook_error(self):
        self.healthy()
        self.hook_error_line(2 * _DAY)
        head = self.head(self.report())
        self.assertIn("⚠ needs attention", head)
        self.assertIn("1 error", head)

    def test_old_hook_error_does_not_need_attention(self):
        self.healthy()
        self.hook_error_line(10 * _DAY)
        head = self.head(self.report())
        self.assertIn("✅ capturing", head)
        self.assertIn("0 errors", head)

    def test_attention_for_bad_lines(self):
        self.healthy()
        with open(self.month_file(time.strftime("%Y-%m", time.gmtime(self.now))), "ab") as fh:
            fh.write(b'{"v":1,"id":"torn')
        self.assertIn("⚠ needs attention", self.head(self.report()))

    def test_attention_for_interpreter_missing(self):
        self.healthy()
        self.write(os.path.join(self.home, "hook-errors.log"),
                   "%sZ session-start interpreter-missing: exit 127\n" % _iso(self.now - 60)[:19])
        self.assertIn("⚠ needs attention", self.head(self.report()))

    def test_stale_without_rules_is_a_row_flag_only(self):
        self.profile_dir(".claude", enabled=True, transcript_age=0)
        self.event(profile="default", ts=_iso(self.now - 48 * _HOUR))
        text = self.report()
        self.assertIn("✅ capturing", self.head(text))
        self.assertEqual(self.cells(text, "default")[-1], "STALE")

    def test_attention_for_stale_with_rules(self):
        self.profile_dir(".claude", enabled=True, transcript_age=0)
        self.event(profile="default", ts=_iso(self.now - 48 * _HOUR))
        self.write(os.path.join(self.home, "config.json"), json.dumps({"profile_rules": [
            {"cwd_prefix": "/home/alice/work", "profile": "default"}]}))
        path = self.write(os.path.join(self.fake_home, ".claude", "projects", "slug", "w.jsonl"),
                          json.dumps({"type": "user", "cwd": "/home/alice/work/acme",
                                      "message": {"content": "hi"}}) + "\n")
        os.utime(path, (self.now, self.now))
        text = self.report()
        self.assertIn("STALE", self.cells(text, "default")[-1])
        self.assertIn("⚠ needs attention", self.head(text))

    def test_not_capturing_outranks_attention(self):
        self.event(source="backfill")
        self.hook_error_line(_HOUR)
        self.assertIn("❌ not capturing", self.head(self.report()))

    def test_disabled_outranks_everything(self):
        self.hook_error_line(_HOUR)
        self.assertIn("⏸ disabled", self.head(self.report({"APPRENTICE_DISABLE": "1"})))


class EnablementTest(StatusTestBase):
    def setup_profiles(self, alice_age=None):
        self.profile_dir(".claude", enabled=True, transcript_age=0)
        self.profile_dir(".claude-acme-alice", enabled=False, transcript_age=alice_age)
        self.profile_dir(".claude-acme-bob", malformed=True, transcript_age=0)

    def test_profile_dirs(self):
        self.setup_profiles()
        self.write(os.path.join(self.fake_home, ".claude-nosettings", "x"), "")
        names = [os.path.basename(p) for p in status.profile_dirs(self.fake_home)]
        self.assertEqual(names, [".claude", ".claude-acme-alice", ".claude-acme-bob"])

    def test_profile_dirs_discovery_rules(self):
        os.makedirs(os.path.join(self.fake_home, ".claude-monitor"))
        self.write(os.path.join(self.fake_home, ".claude-x", "settings.json"), "{}")
        os.makedirs(os.path.join(self.fake_home, ".claude-x", "projects"))
        self.write(os.path.join(self.fake_home, ".claude-y", "settings.json"), "{}")
        self.write(os.path.join(self.fake_home, ".claude-y", ".claude.json"), "{}")
        self.write(os.path.join(self.fake_home, ".claude-z", "settings.json"), "{}")
        names = [os.path.basename(p) for p in status.profile_dirs(self.fake_home)]
        self.assertEqual(names, [".claude", ".claude-x", ".claude-y"])

    def test_claude_mem_data_dir_is_never_a_profile(self):
        self.write(os.path.join(self.fake_home, ".claude-mem", "settings.json"), "{}")
        names = [os.path.basename(p) for p in status.profile_dirs(self.fake_home)]
        self.assertEqual(names, [".claude"])
        text = self.report()
        self.assertNotIn("mem", text)
        self.assertNotIn(".claude-mem", text)

    def test_label_is_the_config_profile_name(self):
        # the name account_groups lists: the directory basename, `default` for ~/.claude
        self.setup_profiles()
        text = self.report()
        self.assertEqual(self.cells(text, "profile")[:2], ["profile", "enabled"])
        for label in ("default", ".claude-acme-alice", ".claude-acme-bob"):
            self.cells(text, label)
        self.assertNotIn("| acme-alice", text)
        env = {"CLAUDE_CONFIG_DIR": os.path.join(self.fake_home, ".claude-acme-alice")}
        self.assertEqual(config.current_profile(env), self.cells(text, ".claude-acme-alice")[0])

    def test_enabled_column(self):
        self.setup_profiles(alice_age=0)
        text = self.report()
        self.assertEqual(self.cells(text, "default")[1], "✅")
        self.assertEqual(self.cells(text, ".claude-acme-alice")[1], "⛔ disabled")
        self.assertEqual(self.cells(text, ".claude-acme-bob")[1], "⚪ not installed")

    def test_missing_key_is_not_installed(self):
        self.profile_dir(".claude")
        self.assertEqual(self.cells(self.report(), "default")[1], "⚪ not installed")

    def test_last_event_formats_in_local_time(self):
        now = _local(2026, 10, 18, 15, 0)
        self.profile_dir(".claude", enabled=True)
        self.profile_dir(".claude-a", enabled=True)
        self.profile_dir(".claude-b", enabled=True)
        self.profile_dir(".claude-c", enabled=True)
        self.event(profile="default", ts=_iso(_local(2026, 10, 18, 11, 51)))
        self.event(profile=".claude-a", ts=_iso(_local(2026, 10, 17, 9, 5)))
        self.event(profile=".claude-b", ts=_iso(_local(2026, 10, 10, 8, 0)))
        text = self.report(now=now)
        self.assertEqual(self.cells(text, "default")[2], "today 11:51")
        self.assertEqual(self.cells(text, ".claude-a")[2], "yesterday 09:05")
        self.assertEqual(self.cells(text, ".claude-b")[2], "2026-10-10")
        self.assertEqual(self.cells(text, ".claude-c")[2], "—")

    def test_flag_column_only_when_a_row_has_a_flag(self):
        self.profile_dir(".claude", enabled=True, transcript_age=0)
        self.event(profile="default", ts=_iso(self.now - _HOUR))
        self.assertEqual(self.cells(self.report(), "profile"), ["profile", "enabled", "last event"])
        self.event(profile="default", ts=_iso(self.now - 48 * _HOUR))
        self.profile_dir(".claude-late", enabled=True, transcript_age=0)
        text = self.report()
        self.assertEqual(self.cells(text, "profile")[-1], "flag")
        self.assertEqual(self.cells(text, "default")[-1], "")
        self.assertEqual(self.cells(text, ".claude-late")[-1], "STALE")

    def test_stale_flag(self):
        self.profile_dir(".claude", enabled=True, transcript_age=0)
        self.event(profile="default", ts=_iso(self.now - 48 * _HOUR))
        self.assertEqual(self.cells(self.report(), "default")[-1], "STALE")

    def test_fresh_live_event_not_stale(self):
        self.profile_dir(".claude", enabled=True, transcript_age=0)
        self.event(profile="default", ts=_iso(self.now - _HOUR))
        self.assertNotIn("STALE", self.report())

    def test_disabled_profile_is_not_flagged_stale(self):
        self.profile_dir(".claude-off", enabled=False, transcript_age=0)
        self.assertNotIn("STALE", self.report())

    def test_actions_default_profile_uses_env_form(self):
        self.profile_dir(".claude", enabled=False)
        text = self.report()
        self.assertIn(_DEFAULT_CMD, text)
        self.assertNotIn("CLAUDE_CONFIG_DIR=%s" % os.path.join(self.fake_home, ".claude"), text)
        self.assertIn("**Actions:**", text)
        self.assertTrue(self.line_with(text, "1. `%s`" % _DEFAULT_CMD))

    def test_actions_other_profile_names_its_dir(self):
        self.setup_profiles(alice_age=0)
        text = self.report()
        alice = os.path.join(self.fake_home, ".claude-acme-alice")
        bob = os.path.join(self.fake_home, ".claude-acme-bob")
        # alice has the plugin but disabled: enable; bob lacks it: add the marketplace, then install
        self.assertIn("CLAUDE_CONFIG_DIR=%s claude plugin enable %s" % (alice, _PLUGIN_KEY), text)
        self.assertNotIn("CLAUDE_CONFIG_DIR=%s claude plugin install" % alice, text)
        self.assertNotIn("CLAUDE_CONFIG_DIR=%s claude plugin marketplace add" % alice, text)
        add = "CLAUDE_CONFIG_DIR=%s claude plugin marketplace add %s" % (bob, status.marketplace_source())
        install = "CLAUDE_CONFIG_DIR=%s claude plugin install %s" % (bob, _PLUGIN_KEY)
        lines = self.lines(text)
        add_at = [i for i, line in enumerate(lines) if line.endswith("`%s`" % add)]
        install_at = [i for i, line in enumerate(lines) if line.endswith("`%s`" % install)]
        self.assertEqual(len(add_at), 1)
        self.assertEqual(install_at, [add_at[0] + 1])

    def test_marketplace_source_is_the_checkout_or_the_github_repo(self):
        self.assertEqual(status.marketplace_source(), REPO_ROOT)
        cached = os.path.join(self.fake_home, ".claude", "plugins", "cache", "vtrifonov", "apprentice", "1.0.0")
        self.assertEqual(status.marketplace_source(cached), status.GITHUB_SOURCE)

    def test_default_profile_missing_plugin_gets_both_lines(self):
        self.profile_dir(".claude")
        text = self.report()
        self.assertIn("1. `env -u CLAUDE_CONFIG_DIR claude plugin marketplace add %s`" % status.marketplace_source(),
                      text)
        self.assertIn("2. `env -u CLAUDE_CONFIG_DIR claude plugin install %s`" % _PLUGIN_KEY, text)
        self.assertNotIn("CLAUDE_CONFIG_DIR=%s claude" % os.path.join(self.fake_home, ".claude "), text)

    def test_actions_none_when_everything_is_enabled(self):
        self.profile_dir(".claude", enabled=True)
        text = self.report()
        self.assertEqual(text.rstrip().splitlines()[-1], "**Actions:** none")

    def test_actions_never_enable_a_non_profile(self):
        self.profile_dir(".claude", enabled=True)
        self.write(os.path.join(self.fake_home, ".claude-mem", "settings.json"), "{}")
        self.assertNotIn("claude-mem", self.report())

    def test_shared_projects_use_profile_rules(self):
        self.profile_dir(".claude", enabled=True)
        bob = self.profile_dir(".claude-acme-bob", enabled=True)
        shared = os.path.join(self.fake_home, ".claude", "projects")
        os.rmdir(os.path.join(bob, "projects"))
        os.symlink(shared, os.path.join(bob, "projects"))
        for name, cwd, age in (("home.jsonl", "/home/alice/personal/x", 0),
                               ("work.jsonl", "/home/alice/work/acme", 48 * _HOUR)):
            path = self.write(os.path.join(shared, "slug", name),
                              json.dumps({"type": "user", "cwd": cwd, "message": {"content": "hi"}}) + "\n")
            os.utime(path, (self.now - age, self.now - age))
        self.event(profile="default", ts=_iso(self.now))
        self.event(profile=".claude-acme-bob", ts=_iso(self.now - 47 * _HOUR))
        note = "ℹ transcripts are shared across 2 profiles; set profile_rules in config.json " \
               "to attribute them per profile."
        text = self.report()
        self.assertNotIn("STALE", text)
        self.assertIn(note, self.lines(text))
        self.write(os.path.join(self.home, "config.json"), json.dumps({"profile_rules": [
            {"cwd_prefix": "/home/alice/work", "profile": ".claude-acme-bob"},
            {"cwd_prefix": "/home/alice/personal", "profile": "default"}]}))
        text = self.report()
        self.assertNotIn("STALE", text)
        self.assertNotIn("transcripts are shared", text)
        self.assertEqual(self.cells(text, ".claude-acme-bob")[1], "✅")

    def test_shared_projects_with_rules_keep_stale_for_idle_profile(self):
        self.profile_dir(".claude", enabled=True)
        bob = self.profile_dir(".claude-acme-bob", enabled=True)
        shared = os.path.join(self.fake_home, ".claude", "projects")
        os.rmdir(os.path.join(bob, "projects"))
        os.symlink(shared, os.path.join(bob, "projects"))
        path = self.write(os.path.join(shared, "slug", "work.jsonl"),
                          json.dumps({"type": "user", "cwd": "/home/alice/work/acme",
                                      "message": {"content": "hi"}}) + "\n")
        os.utime(path, (self.now - _HOUR, self.now - _HOUR))
        self.event(profile=".claude-acme-bob", ts=_iso(self.now - 100 * _HOUR))
        self.write(os.path.join(self.home, "config.json"), json.dumps({"profile_rules": [
            {"cwd_prefix": "/home/alice/work", "profile": ".claude-acme-bob"}]}))
        text = self.report()
        self.assertEqual(self.cells(text, ".claude-acme-bob")[-1], "STALE")
        self.assertNotIn("transcripts are shared", text)

    def test_plugin_enabled(self):
        self.setup_profiles()
        self.profile_dir(".claude-none")
        base = self.fake_home
        self.assertIs(status.plugin_enabled(os.path.join(base, ".claude", "settings.json")), True)
        self.assertIs(status.plugin_enabled(os.path.join(base, ".claude-acme-alice", "settings.json")), False)
        self.assertIsNone(status.plugin_enabled(os.path.join(base, ".claude-acme-bob", "settings.json")))
        self.assertIsNone(status.plugin_enabled(os.path.join(base, ".claude-none", "settings.json")))
        self.assertIsNone(status.plugin_enabled(os.path.join(base, "missing", "settings.json")))

    def test_interpreter_missing_flag_replaces_stale(self):
        self.profile_dir(".claude", enabled=True, transcript_age=0)
        self.event(profile="default", ts=_iso(self.now - 48 * _HOUR))
        self.write(os.path.join(self.home, "hook-errors.log"),
                   "%sZ session-start interpreter-missing: exit 127\n" % _iso(self.now - _HOUR)[:19])
        text = self.report()
        flag = self.cells(text, "default")[-1]
        self.assertEqual(flag, "INTERPRETER MISSING")
        self.assertIn("⚠ needs attention", self.head(text))
        self.assertIn("xcode-select --install", text)
        self.assertIn("APPRENTICE_PYTHON", text)

    def test_old_interpreter_line_ignored(self):
        self.profile_dir(".claude", enabled=True, transcript_age=0)
        self.event(ts=_iso(self.now))
        self.write(os.path.join(self.home, "hook-errors.log"),
                   "%sZ session-start interpreter-missing: exit 127\n" % _iso(self.now - 48 * _HOUR)[:19])
        text = self.report()
        self.assertNotIn("INTERPRETER MISSING", text)
        self.assertNotIn("xcode-select", text)


class GroupsTest(StatusTestBase):
    def test_no_group_line_without_labels_or_unknown(self):
        self.event(profile="alice")
        self.assertFalse(self.line_with(self.report(), "profiles "))
        self.assertNotIn("unknown text withheld", self.report())

    def test_groups_details_no_label_yet(self):
        self.event(profile="alice")
        text = self.report({"CLAUDE_CONFIG_DIR": os.path.join(self.fake_home, ".claude-alice")}, full=True)
        self.assertIn("current profile: .claude-alice", text)
        self.assertIn("current group: no label yet", text)

    def test_groups_with_config(self):
        self.write(os.path.join(self.home, "config.json"),
                   json.dumps({"account_groups": {"work": ["alice", "bob"]}}))
        self.event(profile="alice")
        self.event(profile="bob")
        env = {"CLAUDE_CONFIG_DIR": os.path.join(self.fake_home, "alice")}
        text = self.report(env)
        found = self.line_with(text, "work: profiles alice, bob; events 2")
        self.assertEqual(len(found), 1)
        self.assertNotIn("current group", text)
        self.assertIn("current group: work", self.report(env, full=True))

    def test_unknown_group_line(self):
        self.event(profile="unknown", source="backfill")
        self.assertIn("unknown text withheld", self.report())
        self.assertNotIn("no label yet: events", self.report())
        self.write(os.path.join(self.home, "config.json"),
                   json.dumps({"account_groups": {"work": ["alice"]}, "unknown_group": "work"}))
        text = self.report()
        self.assertNotIn("unknown text withheld", text)
        self.assertIn("work: profiles alice; events 1", text)
        self.assertIn("unknown_group: work", self.report(full=True))


class SessionStartTest(StatusTestBase):
    def write_info(self, **info):
        self.write(os.path.join(self.home, "state", "last-sessionstart.json"), json.dumps(info))

    def test_not_excluded_shows_exclude_path(self):
        tree = tree_fixture.build(self.fake_home)
        self.write_info(ts="2026-10-10T10:00:00.000Z", cwd=tree["wt1"], link="skipped-not-excluded",
                        group=None, profile="default", tone_line=False)
        text = self.report()
        exclude = os.path.join(tree["main"], ".git", "info", "exclude")
        found = self.line_with(text, "SessionStart")
        self.assertEqual(len(found), 1)
        self.assertIn("skipped-not-excluded", found[0])
        self.assertIn(exclude, found[0])
        self.assertIn("CLAUDE.local.md", found[0])
        self.assertTrue(self.line_with(text, ". `echo CLAUDE.local.md >> %s`" % exclude))
        self.assertIn("tone_line: no", self.report(full=True))

    def groups_config(self):
        self.write(os.path.join(self.home, "config.json"),
                   json.dumps({"account_groups": {"home": ["default"], "work": [".claude-work"]}}))

    def test_link_problem_of_another_group_shows_no_path(self):
        tree = tree_fixture.build(self.fake_home)
        self.groups_config()
        cases = {"other group": {"group": "work", "profile": ".claude-work"},
                 "no group recorded": {"profile": "default"},
                 "both unlabelled, other profile": {"group": None, "profile": ".claude-zed"}}
        for name, extra in cases.items():
            if name == "both unlabelled, other profile":
                self.write(os.path.join(self.home, "config.json"), "{}")
            self.write_info(ts="2026-10-10T10:00:00.000Z", cwd=tree["wt1"], link="skipped-not-excluded",
                            tone_line=False, **extra)
            text = self.report(full=True)
            found = self.line_with(text, "SessionStart link")
            self.assertEqual(found, ["SessionStart link: skipped-not-excluded (a session in another account group)"],
                             name)
            self.assertNotIn(tree["main"], text, name)
            self.assertNotIn("echo CLAUDE.local.md", text, name)

    def test_link_problem_of_the_same_group_shows_the_path(self):
        tree = tree_fixture.build(self.fake_home)
        self.groups_config()
        self.write_info(ts="2026-10-10T10:00:00.000Z", cwd=tree["wt1"], link="skipped-not-excluded",
                        group="home", profile="default", tone_line=False)
        exclude = os.path.join(tree["main"], ".git", "info", "exclude")
        self.assertTrue(self.line_with(self.report(), ". `echo CLAUDE.local.md >> %s`" % exclude))

    def test_created_is_not_a_problem(self):
        self.write_info(ts="2026-10-10T10:00:00.000Z", cwd=self.fake_home, link="created", group="g",
                        tone_line=True)
        text = self.report()
        self.assertNotIn("SessionStart", text)
        self.assertNotIn("exclude_path", text)
        full = self.report(full=True)
        self.assertIn("link: created", full)
        self.assertNotIn("exclude_path", full)
        self.assertIn("tone_line: yes", full)

    def test_other_link_errors_show_the_result(self):
        self.write_info(ts="2026-10-10T10:00:00.000Z", cwd=self.fake_home, link="skipped-file-exists",
                        group=None, tone_line=False)
        found = self.line_with(self.report(), "SessionStart")
        self.assertEqual(len(found), 1)
        self.assertIn("skipped-file-exists", found[0])

    def test_benign_links_are_silent(self):
        for link in ("present", "no-source", "not-a-repo"):
            self.write_info(ts="2026-10-10T10:00:00.000Z", cwd=self.fake_home, link=link, group=None,
                            tone_line=False)
            self.assertNotIn("SessionStart", self.report(), link)

    def test_no_session_start_yet(self):
        self.assertNotIn("SessionStart", self.report())
        self.assertIn("last SessionStart: none", self.report(full=True))


class BackfillTest(StatusTestBase):
    def lock_path(self):
        return os.path.join(self.home, "state", "backfill.lock")

    def log_path(self):
        return os.path.join(self.home, "state", "backfill.log")

    def test_not_run_yet(self):
        text = self.report()
        self.assertIn("backfill: not run yet — /apprentice:backfill", self.lines(text))
        self.assertFalse(os.path.exists(self.lock_path()))

    def test_backfill_running_detection(self):
        self.write(self.log_path(), "progress: files 5/10\n")
        fd = os.open(self.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            found = self.line_with(self.report(), "backfill:")
            self.assertEqual(len(found), 1)
            self.assertIn("running", found[0])
            self.assertIn("progress: files 5/10", found[0])
        finally:
            os.close(fd)
        self.assertNotIn("backfill: running", self.report())

    def test_last_run_summary_line(self):
        summary = ("backfill done: files scanned 9, skipped (subagents 2, observer 1, entrypoint 3, marker 0, "
                   "since 4), unchanged 0, errors 0, events by kind (ask 1, prompt 3), duplicates 0, elapsed 1.0s")
        self.write(self.log_path(), "progress: x\n" + summary + "\n")
        stamp = _local(2026, 10, 14, 12, 0)
        os.utime(self.log_path(), (stamp, stamp))
        text = self.report()
        self.assertIn("backfill: last run 2026-10-14, 4 events", self.lines(text))
        self.assertNotIn("not run yet", text)
        self.assertIn("last backfill skipped: subagents 2, observer 1, entrypoint 3, marker 0, since 4",
                      self.report(full=True))

    def test_summary_with_no_events(self):
        self.write(self.log_path(), "backfill done: files scanned 0, skipped (subagents 0), "
                                    "events by kind (none), duplicates 0\n")
        self.assertTrue(self.line_with(self.report(), ", 0 events"))


class ScriptTest(StatusTestBase):
    def run_script(self, *args):
        env = dict(os.environ, APPRENTICE_HOME=self.home, HOME=self.fake_home)
        cmd = ["/usr/bin/python3", "-I", "-S", _SCRIPT] + list(args)
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    def test_script_help_and_removed_flags(self):
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"--full", result.stdout)
        for flag in ("--include-unknown", "--all-profiles"):
            result = self.run_script(flag)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b"removed flag", result.stderr)

    def test_script_prints_report(self):
        self.event()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"1 event", result.stdout.splitlines()[0])
        self.assertNotIn(b"<details>", result.stdout)

    def test_full_adds_details_block(self):
        self.event()
        result = self.run_script("--full")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout.decode("utf-8")
        self.assertIn("<details><summary>details</summary>", text)
        self.assertTrue(text.rstrip().endswith("</details>"))
        self.assertIn("live 1, backfilled 0, total 1", text)

    def test_details_block_only_with_full(self):
        self.event()
        self.assertNotIn("<details>", self.report())
        text = self.report(full=True)
        self.assertEqual(text.count("<details><summary>details</summary>"), 1)
        for section in ("events by profile:", "events by entrypoint:", "account groups:",
                        "last SessionStart:", "retention:", "skipped lines:"):
            self.assertIn(section, text)


class CommandDocTest(ApprenticeTestCase):
    def doc(self):
        with open(_COMMAND_DOC, "r", encoding="utf-8") as fh:
            return fh.read()

    def test_doc_demands_verbatim_markdown_without_code_block(self):
        text = self.doc().lower()
        self.assertIn("verbatim", text)
        self.assertIn("no code block", text)
        self.assertIn("markdown", text)

    def test_doc_passes_arguments_through(self):
        text = self.doc()
        self.assertIn("$ARGUMENTS", text)
        self.assertIn("--full", text)
