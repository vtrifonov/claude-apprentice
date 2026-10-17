import argparse
import contextlib
import io
import json
import os
import signal
import subprocess
import time

from apprentice import backfill, config, events
from tests.base import REPO_ROOT, ApprenticeTestCase
from tests.fixtures.gen import generate_transcript

_TRANSCRIPTS = os.path.join(REPO_ROOT, "tests", "fixtures", "transcripts")
_SCRIPT = os.path.join(REPO_ROOT, "scripts", "backfill.py")
_CWD = "/home/alice/code/acme/widgets"
_SLUG = "-home-alice-code-acme-widgets"
_NOTICE = "backfill already running (pid "


def _fixture(name):
    return os.path.join(_TRANSCRIPTS, name)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _line(role, content, ts, session="sess-1"):
    return json.dumps({"type": role, "timestamp": ts, "sessionId": session, "cwd": _CWD,
                       "message": {"role": role, "content": content}}) + "\n"


def _args(**kw):
    base = {"since": None, "project": None, "dry_run": False, "log": None}
    base.update(kw)
    return argparse.Namespace(**base)


class BackfillRunTest(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.projects = os.path.join(self.fake_home, ".claude", "projects")
        self.lines = []
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        super().tearDown()

    def place(self, name, text=None, slug=_SLUG, source=None, mtime=None, root=None):
        path = os.path.join(root or self.projects, slug, name)
        self.write(path, text if text is not None else _read(_fixture(source or name)))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def run_backfill(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            summary = backfill.run(_args(**kw), os.environ, self.lines.append)
        self.stdout = out.getvalue()
        return summary

    def stored(self):
        return list(events.iter_events(self.home))

    def cursor_entry(self, path):
        return backfill.Cursor.load(self.home).get(path)

    def gen_set(self, files, lines, slug="gen"):
        folder = os.path.join(self.projects, slug)
        os.makedirs(folder, exist_ok=True)
        for i in range(files):
            generate_transcript(os.path.join(folder, "g%04d.jsonl" % i), lines=lines, seed=i + 1, words_per_line=20)

    def popen(self, *argv):
        proc = subprocess.Popen(["/usr/bin/python3", "-I", "-S", _SCRIPT] + list(argv), env=dict(os.environ),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.procs.append(proc)
        return proc

    def wait_for_pid(self, proc, timeout=10.0):
        lock = os.path.join(self.home, "state", "backfill.lock")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail("backfill exited before taking the lock")
            try:
                if _read(lock).strip().isdigit():
                    return
            except OSError:
                pass
            time.sleep(0.01)
        self.fail("lock never held a pid")

    # discovery

    def test_discover_skips_subagents_observer_entrypoint_marker(self):
        plain = self.place("plain.jsonl", mtime=1000)
        cli = self.place("entry_cli.jsonl", mtime=2000)
        desktop = self.place("entry_desktop.jsonl", mtime=3000)
        self.place(os.path.join("sess-1", "subagents", "agent-a.jsonl"), source="plain.jsonl")
        self.place("s.jsonl", slug="-home-alice-code-acme-widgets-claude-mem-observer", source="plain.jsonl")
        self.place("entry_sdk_ts.jsonl")
        self.place("preclass_marker.jsonl")
        cfg = config.load_config(self.home)
        paths, stats = backfill.discover(cfg, None, None, os.environ)
        self.assertEqual(paths, [plain, cli, desktop])
        self.assertEqual((stats["skipped_subagents"], stats["skipped_observer"], stats["skipped_entrypoint"],
                          stats["skipped_marker"]), (1, 1, 1, 1))
        self.assertIsNone(stats["entrypoints"][plain])
        self.assertEqual(stats["entrypoints"][cli], "cli")

    def test_discover_dedups_symlinked_profile_root(self):
        path = self.place("plain.jsonl")
        alt = os.path.join(self.fake_home, ".claude-alt")
        os.symlink(os.path.join(self.fake_home, ".claude"), alt)
        os.environ["CLAUDE_CONFIG_DIR"] = alt
        other = self.place("entry_cli.jsonl", root=os.path.join(self.fake_home, ".claude-other", "projects"))
        paths, _ = backfill.discover(config.load_config(self.home), None, None, os.environ)
        self.assertEqual(paths, [path])
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.dirname(os.path.dirname(os.path.dirname(other)))
        paths, _ = backfill.discover(config.load_config(self.home), None, None, os.environ)
        self.assertEqual(sorted(paths), sorted([path, other]))

    def test_since_and_project(self):
        march = time.mktime((2024, 3, 10, 12, 0, 0, 0, 0, -1))
        may = time.mktime((2024, 5, 10, 12, 0, 0, 0, 0, -1))
        old = self.place("plain.jsonl", mtime=march)
        new = self.place("interrupt.jsonl", mtime=may)
        other = self.place("image.jsonl", slug="-home-alice-other", mtime=may)
        cfg = config.load_config(self.home)
        self.assertEqual(backfill.discover(cfg, None, None, os.environ)[0], [old, new, other])
        self.assertEqual(sorted(backfill.discover(cfg, "2024-04", None, os.environ)[0]), sorted([new, other]))
        self.assertEqual(backfill.discover(cfg, None, "-home-alice-other", os.environ)[0], [other])
        self.assertEqual(backfill.discover(cfg, "2024-04", _SLUG, os.environ)[0], [new])

    # cursor

    def test_half_line_processed_once(self):
        full = _read(_fixture("plain.jsonl"))
        lines = full.splitlines(True)
        cut = len(lines[2]) // 2
        path = self.place("half.jsonl", text="".join(lines[:2]) + lines[2][:cut])
        self.run_backfill()
        self.assertEqual(len(self.stored()), 1)
        entry = self.cursor_entry(path)
        self.assertEqual(entry["offset"], len("".join(lines[:2]).encode("utf-8")))
        self.assertEqual(entry["lines_done"], 2)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lines[2][cut:])
        self.run_backfill()
        self.assertEqual([e["text"] for e in self.stored()], ["Please add a widget cache", "Now add tests for it"])
        self.assertEqual(self.cursor_entry(path)["offset"], len(full.encode("utf-8")))

    def test_unchanged_file_skipped_grown_resumes_shrunk_reprocesses(self):
        lines = _read(_fixture("plain.jsonl")).splitlines(True)
        path = self.place("p.jsonl", text="".join(lines))
        self.assertEqual(self.run_backfill()["events_written"], 2)
        self.assertEqual(self.cursor_entry(path)["lines_done"], 3)
        self.assertEqual(self.run_backfill()["events_written"], 0)
        self.assertEqual(self.cursor_entry(path)["lines_done"], 3)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(_line("user", "Then document the cache", "2030-01-05T10:00:05Z"))
        self.assertEqual(self.run_backfill()["events_written"], 1)
        self.assertEqual(self.cursor_entry(path)["lines_done"], 4)
        self.write(path, "".join(lines[:2]))
        summary = self.run_backfill()
        self.assertEqual(self.cursor_entry(path)["lines_done"], 2)
        self.assertEqual(summary["events_written"], 0)
        self.assertEqual(summary["dropped"]["prompt_key"], 1)
        keys = [(e["session_id"], e["ts"], e["sha1"]) for e in self.stored() if e["kind"] == "prompt"]
        self.assertEqual(len(keys), 3)
        self.assertEqual(len(set(keys)), 3)

    def test_cursor_flush_on_sigint(self):
        self.gen_set(200, 400)
        proc = self.popen()
        self.wait_for_pid(proc)
        time.sleep(0.3)
        try:
            proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
        proc.wait(timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr.read())
        with open(os.path.join(self.home, "state", "backfill-cursor.json"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIsInstance(data["files"], dict)
        self.assertGreater(len(data["files"]), 0)

    def test_cursor_maybe_save_every_50_files_and_5s(self):
        now = [1000.0]
        saved_clock = backfill.CLOCK
        backfill.CLOCK = lambda: now[0]
        self.addCleanup(setattr, backfill, "CLOCK", saved_clock)
        config.ensure_dirs(self.home)
        cursor = backfill.Cursor.load(self.home)
        calls = [0]
        real_save = cursor.save

        def counting_save():
            calls[0] += 1
            real_save()

        cursor.save = counting_save
        cursor_file = os.path.join(self.home, "state", "backfill-cursor.json")
        for i in range(49):
            cursor.set("/f/%d.jsonl" % i, 10, 1.0, backfill.FileState(offset=10, lines_done=1))
            cursor.maybe_save()
        self.assertEqual(calls[0], 0)
        cursor.set("/f/49.jsonl", 10, 1.0, backfill.FileState(offset=10, lines_done=1))
        cursor.maybe_save()
        self.assertEqual(calls[0], 1)
        self.assertEqual(len(json.loads(_read(cursor_file))["files"]), 50)
        now[0] += 5.0
        cursor.set("/f/50.jsonl", 10, 1.0, backfill.FileState(offset=10, lines_done=1))
        cursor.maybe_save()
        self.assertEqual(calls[0], 2)
        self.assertEqual(len(json.loads(_read(cursor_file))["files"]), 51)

    # lock

    def test_two_concurrent_runs_one_notice(self):
        self.gen_set(60, 400)
        first, second = self.popen(), self.popen()
        outs = [(p.wait(timeout=120), p.stdout.read().decode("utf-8")) for p in (first, second)]
        codes = sorted(code for code, _ in outs)
        self.assertEqual(codes, [0, 1], outs)
        notice = [out for code, out in outs if code == 1][0]
        self.assertIn(_NOTICE, notice)
        self.assertEqual(sum(_NOTICE in out for _, out in outs), 1)

    def test_lock_released_after_holder_killed(self):
        self.gen_set(200, 400)
        proc = self.popen()
        self.wait_for_pid(proc)
        time.sleep(0.2)
        self.assertIsNone(proc.poll(), "the holder finished before it could be killed")
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        summary = self.run_backfill(project="nothing-here")
        self.assertIn(summary.get("exit", 0), (0,))
        self.assertIn("files_scanned", summary)
        self.assertNotIn(_NOTICE, self.stdout)

    # dedup

    def seed_hook(self, kind, session, ts, **fields):
        record = events.envelope(kind, "hook", session, "default", "cli", _CWD, None, None, None, None, ts=ts)
        record.update(fields)
        config.ensure_dirs(self.home)
        events.append_event(self.home, record)
        return record

    def test_watermark_live_session_pre_only(self):
        self.seed_hook("prompt", "sess-w", "2030-01-05T10:00:00.000Z", text="live")
        text = _line("user", "Before the hooks", "2030-01-05T09:59:50Z", session="sess-w")
        text += _line("user", "After the hooks", "2030-01-05T10:00:10Z", session="sess-w")
        self.place("w.jsonl", text=text)
        summary = self.run_backfill()
        backfilled = [e["text"] for e in self.stored() if e["source"] == "backfill"]
        self.assertEqual(backfilled, ["Before the hooks"])
        self.assertEqual(summary["dropped"]["watermark"], 1)

    def test_ask_rejected_live_and_transcript_once(self):
        self.seed_hook("ask_rejected", "sess-1", "2030-01-05T10:00:10.000Z", tool_use_id="tu1", questions=[])
        self.place("ask_rejected.jsonl")
        summary = self.run_backfill()
        rejected = [e for e in self.stored() if e["kind"] == "ask_rejected"]
        self.assertEqual([e["source"] for e in rejected], ["hook"])
        self.assertEqual(summary["dropped"]["ask_key"], 1)
        self.assertEqual([e["kind"] for e in self.stored() if e["source"] == "backfill"], ["prompt"])

    def test_rerun_idempotent(self):
        for name in ("plain.jsonl", "ask_plain.jsonl", "compact.jsonl", "interrupt.jsonl"):
            self.place(name)
        first = self.run_backfill()["events_written"]
        self.assertGreater(first, 0)
        self.assertEqual(self.run_backfill()["events_written"], 0)
        os.remove(os.path.join(self.home, "state", "backfill-cursor.json"))
        self.assertEqual(self.run_backfill()["events_written"], 0)
        self.assertEqual(len(self.stored()), first)

    def test_duplicate_of_cross_session_fork_fixture(self):
        self.place("fork.jsonl", mtime=1000)
        self.place("fork2.jsonl", mtime=2000)
        summary = self.run_backfill()
        prompts = {e["session_id"]: e for e in self.stored() if e["kind"] == "prompt"}
        self.assertIsNone(prompts["sess-fork-a"]["duplicate_of"])
        self.assertEqual(prompts["sess-fork-b"]["duplicate_of"], prompts["sess-fork-a"]["id"])
        self.assertEqual(summary["duplicates"], 1)

    def test_retention_cutoff_skips_old_lines(self):
        self.write(os.path.join(self.home, "config.json"), json.dumps({"retention_months": 2}))
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        text = _line("user", "Ancient request", "2001-01-05T10:00:00Z")
        text += _line("user", "Recent request", recent)
        self.place("r.jsonl", text=text)
        self.run_backfill()
        self.assertEqual([e["text"] for e in self.stored()], ["Recent request"])

    # run surface

    def test_dry_run_writes_only_log(self):
        self.place("plain.jsonl")
        summary = self.run_backfill(dry_run=True)
        self.assertEqual(summary["events_by_kind"], {"prompt": 2})
        self.assertEqual(self.stored(), [])
        self.assertEqual(events.month_files(self.home), [])
        self.assertFalse(os.path.exists(os.path.join(self.home, "state", "backfill-cursor.json")))
        self.assertTrue(any("files scanned" in line for line in self.lines))
        self.assertEqual(self.run_backfill()["events_written"], 2)

    def test_summary_line_fields(self):
        self.place("plain.jsonl")
        self.place(os.path.join("s", "subagents", "a.jsonl"), source="plain.jsonl")
        self.place("s.jsonl", slug="x-claude-mem-observer", source="plain.jsonl")
        self.place("entry_sdk_cli.jsonl")
        self.place("preclass_marker.jsonl")
        summary = self.run_backfill()
        line = self.lines[-1]
        for field in ("files scanned 1", "skipped (subagents 1, observer 1, entrypoint 1, marker 1",
                      "events by kind", "prompt 2", "dropped by rule", "duplicates 0", "elapsed"):
            self.assertIn(field, line)
        self.assertIn(line, self.stdout)
        self.assertEqual(summary["files_scanned"], 1)

    def test_removed_flags_usage_error(self):
        for flag in ("--include-unknown", "--all-profiles"):
            result = subprocess.run(["/usr/bin/python3", "-I", "-S", _SCRIPT, flag], env=dict(os.environ),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 2, flag)
            self.assertIn(b"removed flag; set account_groups and unknown_group in config.json instead",
                          result.stderr)

    def test_events_land_in_own_month_file(self):
        text = _line("user", "Late in January", "2030-01-31T23:59:59Z")
        text += _line("user", "Early in February", "2030-02-01T00:00:01Z")
        self.place("m.jsonl", text=text)
        self.run_backfill()
        self.assertEqual([e["text"] for e in events.iter_events(self.home, ["2030-01"])], ["Late in January"])
        self.assertEqual([e["text"] for e in events.iter_events(self.home, ["2030-02"])], ["Early in February"])

    def test_command_file(self):
        body = _read(os.path.join(REPO_ROOT, "commands", "backfill.md"))
        self.assertTrue(body.startswith("---\ndescription: Backfill apprentice events from past transcripts\n"))
        self.assertIn("argument-hint: [--since YYYY-MM] [--project <slug>] [--dry-run]\n", body)
        self.assertIn("allowed-tools: Bash(nohup *), Bash(${APPRENTICE_PYTHON:-/usr/bin/python3} *)\n", body)
        self.assertIn('nohup "${APPRENTICE_PYTHON:-/usr/bin/python3}" -I -S "<root>/scripts/backfill.py" $ARGUMENTS'
                      ' > "${APPRENTICE_HOME:-$HOME/.claude/apprentice}/state/backfill.log" 2>&1 &', body)
        for needle in ("${CLAUDE_PLUGIN_ROOT}", "state/plugin-root", "/apprentice:status", "126", "127", "xcrun",
                       "xcode-select --install", "APPRENTICE_PYTHON"):
            self.assertIn(needle, body)

    def test_script_runs_and_writes_log(self):
        self.place("plain.jsonl")
        log = os.path.join(self.fake_home, "bf.log")
        result = subprocess.run(["/usr/bin/python3", "-I", "-S", _SCRIPT, "--log", log], env=dict(os.environ),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"files scanned 1", result.stdout)
        self.assertIn("files scanned 1", _read(log))
        self.assertEqual(len(self.stored()), 2)

    def test_stdout_redirected_to_log_writes_summary_once(self):
        self.place("plain.jsonl")
        log = os.path.join(self.home, "state", "backfill.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "w") as out:
            code = subprocess.call(["/usr/bin/python3", "-I", "-S", _SCRIPT], env=dict(os.environ), stdout=out,
                                   stderr=subprocess.STDOUT)
        self.assertEqual(code, 0)
        self.assertEqual(_read(log).count("files scanned 1"), 1)
