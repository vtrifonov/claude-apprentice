import fcntl
import json
import os
import subprocess
import time

from apprentice import config, events, status
from tests.base import REPO_ROOT, ApprenticeTestCase
from tests.fixtures import worktrees as tree_fixture

_SCRIPT = os.path.join(REPO_ROOT, "scripts", "status.py")
_PLUGIN_KEY = "apprentice@vtrifonov"
_HOUR = 3600.0


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


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

    def report(self, env=None):
        return status.report(self.home, env or {}, self.fake_home)

    def lines(self, text):
        return [line.rstrip() for line in text.splitlines()]

    def row(self, text, first):
        # the last match: the enablement table follows the per-profile event counts
        found = None
        for line in self.lines(text):
            parts = line.split()
            if parts and parts[0] == first:
                found = parts
        if found is None:
            self.fail("no row starting with %r in:\n%s" % (first, text))
        return found


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

    def test_counts_table(self):
        self.seed()
        text = self.report()
        self.assertEqual(self.row(text, "prompt")[1:], ["1", "2"])
        self.assertEqual(self.row(text, "ask")[1:], ["0", "1"])
        self.assertIn("2026-09", text)
        self.assertIn("live 3, backfilled 1, total 4", text)
        self.assertEqual(self.row(text, "alice")[1], "3")
        self.assertEqual(self.row(text, "bob")[1], "1")
        self.assertEqual(self.row(text, "cli")[1], "3")
        self.assertIn("entrypoint_unknown: 1", text)

    def test_skipped_counts(self):
        self.seed()
        self.assertIn("skipped lines: bad 1, future 1", self.report())

    def test_disabled_row(self):
        self.assertIn("disabled: no", self.report())
        self.assertIn("disabled: yes", self.report({"APPRENTICE_DISABLE": "1"}))
        self.write(os.path.join(self.home, "disabled"), "")
        self.assertIn("disabled: yes", self.report())

    def test_last_error_and_log_size(self):
        config.log_error("capture", "boom", self.home)
        text = self.report()
        self.assertIn("last hook error:", text)
        self.assertIn("capture boom", text)
        self.assertIn("hook-errors.log: %d bytes" % os.path.getsize(os.path.join(self.home, "hook-errors.log")), text)

    def test_retention_rows(self):
        self.event(ts="2026-09-10T10:00:00.000Z")
        text = self.report()
        self.assertIn("retention: 24 months, cutoff month ", text)
        self.assertIn("oldest month kept 2026-09", text)


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
        self.write(os.path.join(self.fake_home, ".claude-mem", "settings.json"), "{}")
        os.makedirs(os.path.join(self.fake_home, ".claude-monitor"))
        self.write(os.path.join(self.fake_home, ".claude-x", "settings.json"), "{}")
        os.makedirs(os.path.join(self.fake_home, ".claude-x", "projects"))
        self.write(os.path.join(self.fake_home, ".claude-y", "settings.json"), "{}")
        self.write(os.path.join(self.fake_home, ".claude-y", ".claude.json"), "{}")
        self.write(os.path.join(self.fake_home, ".claude-z", "settings.json"), "{}")
        names = [os.path.basename(p) for p in status.profile_dirs(self.fake_home)]
        self.assertEqual(names, [".claude", ".claude-mem", ".claude-x", ".claude-y"])

    def test_default_profile_hint_unsets_config_dir(self):
        self.profile_dir(".claude", enabled=False)
        text = self.report()
        self.assertIn("  env -u CLAUDE_CONFIG_DIR claude plugin %s %s" % (status.ENABLE_VERB, _PLUGIN_KEY), text)
        self.assertNotIn("CLAUDE_CONFIG_DIR=%s" % os.path.join(self.fake_home, ".claude"), text)

    def test_plugin_enabled(self):
        self.setup_profiles()
        self.profile_dir(".claude-none")
        base = self.fake_home
        self.assertIs(status.plugin_enabled(os.path.join(base, ".claude", "settings.json")), True)
        self.assertIs(status.plugin_enabled(os.path.join(base, ".claude-acme-alice", "settings.json")), False)
        self.assertIsNone(status.plugin_enabled(os.path.join(base, ".claude-acme-bob", "settings.json")))
        self.assertIsNone(status.plugin_enabled(os.path.join(base, ".claude-none", "settings.json")))
        self.assertIsNone(status.plugin_enabled(os.path.join(base, "missing", "settings.json")))

    def test_enablement_rows(self):
        self.setup_profiles(alice_age=0)
        self.event(profile="default")
        text = self.report()
        default = self.row(text, "default")
        self.assertEqual(default[1], "yes")
        self.assertNotIn("NOT", default)
        alice = self.row(text, ".claude-acme-alice")
        self.assertEqual(alice[1], "no")
        self.assertIn("NOT ENABLED", " ".join(alice))
        bob = self.row(text, ".claude-acme-bob")
        self.assertEqual(bob[1], "unknown")
        self.assertIn("CLAUDE_CONFIG_DIR=%s claude plugin %s %s"
                      % (os.path.join(self.fake_home, ".claude-acme-alice"), status.ENABLE_VERB, _PLUGIN_KEY), text)

    def test_stale_flag(self):
        self.profile_dir(".claude", enabled=True, transcript_age=0)
        self.event(profile="default", ts=_iso(self.now - 48 * _HOUR))
        self.assertIn("STALE", " ".join(self.row(self.report(), "default")))

    def test_fresh_live_event_not_stale(self):
        self.profile_dir(".claude", enabled=True, transcript_age=0)
        self.event(profile="default", ts=_iso(self.now - _HOUR))
        self.assertNotIn("STALE", " ".join(self.row(self.report(), "default")))

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
        text = self.report()
        self.assertNotIn("STALE", text)
        self.assertIn("transcripts are shared across 2 profiles; set profile_rules in config.json "
                      "to attribute them per profile", text)
        self.write(os.path.join(self.home, "config.json"), json.dumps({"profile_rules": [
            {"cwd_prefix": "/home/alice/work", "profile": ".claude-acme-bob"},
            {"cwd_prefix": "/home/alice/personal", "profile": "default"}]}))
        text = self.report()
        bob_row = self.row(text, ".claude-acme-bob")
        self.assertNotIn("STALE", " ".join(bob_row))
        self.assertIn(_iso(self.now - 48 * _HOUR)[:19] + "Z", bob_row)
        self.assertIn(_iso(self.now)[:19] + "Z", self.row(text, "default"))
        self.assertNotIn("transcripts are shared", text)

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
        self.assertIn("STALE", " ".join(self.row(text, ".claude-acme-bob")))
        self.assertNotIn("transcripts are shared", text)

    def test_interpreter_missing_first_and_replaces_stale(self):
        self.profile_dir(".claude", enabled=True, transcript_age=0)
        self.event(profile="default", ts=_iso(self.now - 48 * _HOUR))
        self.write(os.path.join(self.home, "hook-errors.log"),
                   "%sZ session-start interpreter-missing: exit 127\n" % _iso(self.now - _HOUR)[:19])
        text = self.report()
        self.assertTrue(text.startswith("interpreter missing since "), text[:80])
        self.assertIn("hooks are not running", text.splitlines()[0])
        flags = " ".join(self.row(text, "default"))
        self.assertIn("INTERPRETER MISSING", flags)
        self.assertNotIn("STALE", flags)

    def test_old_interpreter_line_ignored(self):
        self.event(ts=_iso(self.now))
        self.write(os.path.join(self.home, "hook-errors.log"),
                   "%sZ session-start interpreter-missing: exit 127\n" % _iso(self.now - 48 * _HOUR)[:19])
        self.assertFalse(self.report().startswith("interpreter missing"))


class GroupsTest(StatusTestBase):
    def test_groups_rows_no_label_yet(self):
        self.event(profile="alice")
        text = self.report({"CLAUDE_CONFIG_DIR": os.path.join(self.fake_home, ".claude-alice")})
        self.assertIn("current profile: .claude-alice", text)
        self.assertIn("current group: no label yet", text)

    def test_groups_with_config(self):
        self.write(os.path.join(self.home, "config.json"),
                   json.dumps({"account_groups": {"work": ["alice", "bob"]}}))
        self.event(profile="alice")
        self.event(profile="bob")
        text = self.report({"CLAUDE_CONFIG_DIR": os.path.join(self.fake_home, "alice")})
        self.assertIn("current group: work", text)
        self.assertIn("work: profiles alice, bob; events 2", text)

    def test_unknown_group_line(self):
        self.event(profile="unknown", source="backfill")
        self.assertIn("unknown text withheld", self.report())
        self.assertNotIn("no label yet: events", self.report())
        self.write(os.path.join(self.home, "config.json"),
                   json.dumps({"account_groups": {"work": ["alice"]}, "unknown_group": "work"}))
        text = self.report()
        self.assertIn("unknown_group: work", text)
        self.assertNotIn("unknown text withheld", text)


class SessionStartTest(StatusTestBase):
    def write_info(self, **info):
        self.write(os.path.join(self.home, "state", "last-sessionstart.json"), json.dumps(info))

    def test_not_excluded_shows_exclude_path(self):
        tree = tree_fixture.build(self.fake_home)
        self.write_info(ts="2026-10-10T10:00:00.000Z", cwd=tree["wt1"], link="skipped-not-excluded",
                        group=None, tone_line=False)
        text = self.report()
        exclude = os.path.join(tree["main"], ".git", "info", "exclude")
        self.assertIn("link: skipped-not-excluded", text)
        self.assertIn("exclude_path: " + exclude, text)
        self.assertIn("CLAUDE.local.md", text.split("exclude_path:")[1])
        self.assertIn("tone_line: no", text)

    def test_created_has_no_exclude_path(self):
        self.write_info(ts="2026-10-10T10:00:00.000Z", cwd=self.fake_home, link="created", group="g",
                        tone_line=True)
        text = self.report()
        self.assertIn("link: created", text)
        self.assertNotIn("exclude_path", text)
        self.assertIn("tone_line: yes", text)

    def test_no_session_start_yet(self):
        self.assertIn("last SessionStart: none", self.report())


class BackfillTest(StatusTestBase):
    def lock_path(self):
        return os.path.join(self.home, "state", "backfill.lock")

    def test_backfill_running_detection(self):
        self.write(os.path.join(self.home, "state", "backfill.log"), "progress: files 5/10\n")
        fd = os.open(self.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            text = self.report()
            self.assertIn("backfill running: yes", text)
            self.assertIn("progress: files 5/10", text)
        finally:
            os.close(fd)
        self.assertIn("backfill running: no", self.report())

    def test_not_running_when_no_lock_file(self):
        self.assertIn("backfill running: no", self.report())
        self.assertFalse(os.path.exists(self.lock_path()))

    def test_last_summary_skip_counts(self):
        summary = ("backfill done: files scanned 9, skipped (subagents 2, observer 1, entrypoint 3, marker 0, "
                   "since 4), unchanged 0, errors 0, events by kind (prompt 3), duplicates 0, elapsed 1.0s")
        self.write(os.path.join(self.home, "state", "backfill.log"), "progress: x\n" + summary + "\n")
        text = self.report()
        self.assertIn("last backfill skipped: subagents 2, observer 1, entrypoint 3, marker 0, since 4", text)


class ScriptTest(StatusTestBase):
    def run_script(self, *args):
        env = dict(os.environ, APPRENTICE_HOME=self.home, HOME=self.fake_home)
        cmd = ["/usr/bin/python3", "-I", "-S", _SCRIPT] + list(args)
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    def test_script_help_and_removed_flags(self):
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0)
        for flag in ("--include-unknown", "--all-profiles"):
            result = self.run_script(flag)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b"removed flag", result.stderr)

    def test_script_prints_report(self):
        self.event()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"live 1, backfilled 0, total 1", result.stdout)
