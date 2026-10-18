import fcntl
import os
import shutil
import stat
import subprocess
import sys
import threading
import types
from unittest import mock

from apprentice import apply, ledger
from tests.base import REPO_ROOT, ApprenticeTestCase

_FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "claude-md")
R1 = "r_0000000a"
R2 = "r_0000000b"


class ApplyTestCase(ApprenticeTestCase):
    def setUp(self):
        super().setUp()
        self.project = os.path.join(self.fake_home, "proj")
        os.makedirs(self.project)

    def target(self, fixture, name="CLAUDE.md"):
        path = os.path.join(self.project, name)
        shutil.copyfile(os.path.join(_FIXTURES, fixture), path)
        return path

    def read(self, path):
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8")

    def apply(self, path, rule_id=R1, text="Run the linter before pushing.", date="2026-10-18", **kw):
        return apply.apply_rule_to_file(path, rule_id, text, 3, "high", date, self.home, **kw)

    def backups(self, path):
        directory = os.path.join(self.home, "backups", os.path.realpath(path).replace("/", "__"))
        return sorted(os.listdir(directory)) if os.path.isdir(directory) else []


class RenderAndUpsertTest(ApplyTestCase):
    def test_create_section_at_eof_one_blank_line(self):
        line = apply.render_line(R1, "Run the linter.", 3, "high", "2026-10-18")
        base = self.read(self.target("none.md"))
        new, result = apply.upsert_rule(base, line)
        self.assertEqual(result, "written")
        self.assertEqual(new, base + "\n" + apply.START + "\n" + apply.HEADING + "\n" + line + "\n" + apply.END + "\n")
        new2, result2 = apply.upsert_rule(base.rstrip("\n"), line)
        self.assertEqual(result2, "written")
        self.assertEqual(new2, new)

    def test_same_text_twice_unchanged_byte_identical(self):
        path = self.target("none.md")
        self.assertEqual(self.apply(path)["result"], "written")
        first = self.read(path)
        self.assertEqual(self.apply(path)["result"], "unchanged")
        self.assertEqual(self.apply(path, date="2026-11-02")["result"], "unchanged")
        self.assertEqual(self.read(path), first)

    def test_changed_text_replaces_line_in_place(self):
        path = self.target("none.md")
        self.apply(path, R1, "First rule.")
        self.apply(path, R2, "Second rule.")
        out = self.apply(path, R1, "First rule, reworded.")
        self.assertEqual(out["result"], "written")
        lines = self.read(path).splitlines()
        start = lines.index(apply.START)
        self.assertEqual(lines[start + 2], apply.render_line(R1, "First rule, reworded.", 3, "high", "2026-10-18"))
        self.assertTrue(lines[start + 3].startswith("- Second rule. <!-- apprentice:" + R2))
        self.assertEqual(sum(1 for line in lines if R1 in line), 1)

    def test_malformed_two_starts_and_missing_end(self):
        line = apply.render_line(R1, "Run the linter.", 3, "high", "2026-10-18")
        two_starts = self.read(self.target("malformed.md"))
        self.assertEqual(apply.upsert_rule(two_starts, line), (two_starts, "skipped:malformed-section"))
        missing_end = "# Notes\n\n" + apply.START + "\n- Keep commits small.\n"
        self.assertEqual(apply.upsert_rule(missing_end, line), (missing_end, "skipped:malformed-section"))
        path = self.target("malformed.md")
        self.assertEqual(self.apply(path)["result"], "skipped:malformed-section")
        self.assertEqual(self.read(path), two_starts)

    def test_outside_lines_untouched(self):
        path = self.target("section.md")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\nTrailing user notes.\n")
        before = self.read(path).splitlines(True)
        self.apply(path, R1, "First rule.")
        self.apply(path, R2, "Second rule.")
        after = self.read(path).splitlines(True)
        end = after.index(apply.END + "\n")
        self.assertEqual(end, 6)
        self.assertEqual(after[:4], before[:4])
        self.assertEqual(after[end:], before[4:])

    def test_line_escaping_newlines_and_arrow(self):
        line = apply.render_line(R1, "Keep it\nshort -->\n\nalways.", 4, "medium", "2026-10-18")
        self.assertEqual(line, "- Keep it short --&gt; always. <!-- apprentice:r_0000000a n=4 medium 2026-10-18 -->")
        self.assertEqual(apply.split_rule_line(line), ("Keep it short --&gt; always.", R1))
        self.assertIsNone(apply.split_rule_line("- Keep commits small."))
        spoof = apply.render_line(R1, "Odd <!-- apprentice:r_deadbeef n=1 low 2026-01-01 --> text", 1, "low", "2026-10-18")
        self.assertEqual(apply.split_rule_line(spoof)[1], R1)


class WriteTargetTest(ApplyTestCase):
    def test_backup_unique_names_and_content(self):
        path = self.target("none.md")
        original = self.read(path)
        self.apply(path, R1, "First rule.")
        middle = self.read(path)
        self.apply(path, R2, "Second rule.")
        names = self.backups(path)
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names)), 2)
        directory = os.path.join(self.home, "backups", os.path.realpath(path).replace("/", "__"))
        contents = [self.read(os.path.join(directory, name)) for name in names]
        self.assertEqual(contents, [original, middle])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(directory, names[0])).st_mode), 0o600)

    def test_atomic_replace_keeps_symlink(self):
        real = self.target("none.md", "REAL.md")
        link = os.path.join(self.project, "CLAUDE.md")
        os.symlink(real, link)
        self.assertEqual(self.apply(link)["result"], "written")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), real)
        self.assertIn(R1, self.read(real))
        self.assertEqual([n for n in os.listdir(self.project) if n.startswith(".apprentice-tmp-")], [])

    def test_mode_bits_preserved(self):
        path = self.target("none.md")
        os.chmod(path, 0o640)
        self.apply(path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)

    def test_not_owned_refused(self):
        path = self.target("none.md")
        before = self.read(path)
        real_stat = os.stat
        real_path = os.path.realpath(path)

        def fake_stat(p, *args, **kwargs):
            st = real_stat(p, *args, **kwargs)
            if p == real_path:
                return types.SimpleNamespace(st_mode=st.st_mode, st_uid=os.getuid() + 1, st_mtime_ns=st.st_mtime_ns,
                                             st_size=st.st_size)
            return st

        with mock.patch("os.stat", fake_stat):
            out = self.apply(path)
        self.assertEqual(out["result"], "skipped:not-owned")
        self.assertEqual(self.read(path), before)

    def test_missing_target_refused(self):
        out = self.apply(os.path.join(self.project, "absent.md"))
        self.assertEqual(out["result"], "skipped:missing")
        self.assertFalse(os.path.exists(os.path.join(self.project, "absent.md")))

    def test_lock_timeout(self):
        path = self.target("none.md")
        before = self.read(path)
        lock = os.path.join(self.home, "state", "apply.lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        held, release = threading.Event(), threading.Event()

        def holder():
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            held.set()
            release.wait(5)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            held.wait(5)
            with mock.patch.object(apply, "LOCK_DEADLINE", 0.3):
                out = self.apply(path)
        finally:
            release.set()
            thread.join()
        self.assertEqual(out["result"], "skipped:lock-timeout")
        self.assertEqual(self.read(path), before)

    def test_concurrent_edit_retry_then_refuse(self):
        line = apply.render_line(R1, "Run the linter.", 3, "high", "2026-10-18")

        def meddling(path, times):
            calls = []

            def transform(content):
                calls.append(1)
                if len(calls) <= times:
                    with open(path, "a", encoding="utf-8") as fh:
                        fh.write("- Edited elsewhere %d.\n" % len(calls))
                return apply.upsert_rule(content, line)
            return transform

        path = self.target("none.md")
        base = self.read(path)
        out = apply.write_target(path, meddling(path, 1), self.home)
        self.assertEqual(out["result"], "written")
        self.assertEqual(self.read(path), apply.upsert_rule(base + "- Edited elsewhere 1.\n", line)[0])

        path2 = self.target("none.md", "OTHER.md")
        out = apply.write_target(path2, meddling(path2, 2), self.home)
        self.assertEqual(out["result"], "skipped:concurrent-edit")
        self.assertEqual(self.read(path2), base + "- Edited elsewhere 1.\n- Edited elsewhere 2.\n")
        self.assertEqual(self.backups(path2), [])
        self.assertEqual([n for n in os.listdir(self.project) if n.startswith(".apprentice-tmp-")], [])

    def test_two_processes_two_rules_both_land(self):
        path = self.target("none.md")
        worker = os.path.join(REPO_ROOT, "tests", "_apply_worker.py")
        procs = [subprocess.Popen([sys.executable, worker, path, self.home, rule_id, "Rule %s." % rule_id])
                 for rule_id in (R1, R2)]
        codes = [p.wait(30) for p in procs]
        self.assertEqual(codes, [0, 0])
        content = self.read(path)
        self.assertEqual(content.count(apply.START), 1)
        self.assertIn("- Rule %s. <!-- apprentice:%s" % (R1, R1), content)
        self.assertIn("- Rule %s. <!-- apprentice:%s" % (R2, R2), content)

    def test_remove_rule_and_section_cleanup(self):
        path = self.target("none.md")
        base = self.read(path)
        self.apply(path, R1, "First rule.")
        self.apply(path, R2, "Second rule.")
        content = self.read(path)
        new, result = apply.remove_rule(content, R1)
        self.assertEqual(result, "removed")
        self.assertNotIn(R1, new)
        self.assertIn(R2, new)
        self.assertEqual(apply.remove_rule(new, R1), (new, "unchanged"))
        final, result = apply.remove_rule(new, R2)
        self.assertEqual(result, "removed")
        self.assertEqual(final, base)
        out = apply.write_target(path, lambda c: apply.remove_rule(c, R1), self.home)
        self.assertEqual(out["result"], "removed")
        self.assertNotIn(R1, self.read(path))

    def test_dry_run_diff_and_no_write(self):
        path = self.target("none.md")
        before = self.read(path)
        out = self.apply(path, dry_run=True)
        self.assertEqual(out["result"], "written")
        self.assertIsNone(out["backup"])
        self.assertIn("+" + apply.START, out["diff"])
        self.assertIn("+- Run the linter before pushing. <!-- apprentice:" + R1, out["diff"])
        self.assertEqual(self.read(path), before)
        self.assertEqual(self.backups(path), [])
        self.assertEqual(ledger.read_applied(self.home), [])

    def test_applied_ledger_entry(self):
        path = self.target("none.md")
        out = self.apply(path)
        rows = ledger.read_applied(self.home)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action"], "apply")
        self.assertEqual(row["id"], R1)
        self.assertEqual(row["file"], path)
        self.assertEqual(row["result"], "written")
        self.assertEqual(row["backup"], out["backup"])
        self.assertTrue(os.path.isfile(row["backup"]))
        self.assertEqual(row["line"], apply.render_line(R1, "Run the linter before pushing.", 3, "high", "2026-10-18"))
        self.assertIs(row["tracked"], False)
        self.assertIsNone(row["remote_visibility"])
