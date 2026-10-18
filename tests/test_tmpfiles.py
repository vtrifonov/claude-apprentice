import os
import time

from apprentice import tmpfiles
from tests.base import ApprenticeTestCase

_HOUR = 3600.0


class TmpFilesTest(ApprenticeTestCase):
    def tmp(self, name, text="x", age=None):
        path = self.write(os.path.join(self.home, "state", "tmp", name), text)
        if age is not None:
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        return path

    def test_sweep_removes_only_regular_files_older_than_an_hour(self):
        old = self.tmp("g1-r_0000000a.txt", age=_HOUR + 60)
        fresh = self.tmp("g1-r_0000000b.txt", age=60)
        outside = self.write(os.path.join(self.home, "state", "keep.txt"), "x")
        os.utime(outside, (time.time() - 10 * _HOUR,) * 2)
        sub = os.path.join(self.home, "state", "tmp", "sub")
        os.makedirs(sub)
        os.utime(sub, (time.time() - 10 * _HOUR,) * 2)
        link = os.path.join(self.home, "state", "tmp", "link.txt")
        os.symlink(outside, link)
        self.assertEqual(tmpfiles.sweep(self.home), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.isdir(sub))
        self.assertTrue(os.path.lexists(link))
        self.assertTrue(os.path.exists(outside))

    def test_sweep_without_tmp_dir_is_a_no_op(self):
        self.assertEqual(tmpfiles.sweep(self.home), 0)

    def test_read_accepts_group_slug_names_and_deletes(self):
        for name in ("group-1-r_0000000a.txt", "group-1-r_0000000a.edit.txt", "group-1-tone-model.json",
                     "group-1-display-name.txt"):
            path = self.tmp(name, "body")
            self.assertEqual(tmpfiles.read(self.home, path, "--x"), ("body", None))
            tmpfiles.discard(self.home, path)
            self.assertFalse(os.path.exists(path), name)

    def test_read_refuses_outside_and_discard_never_deletes_outside(self):
        outside = self.write(os.path.join(self.home, "elsewhere.txt"), "x")
        content, error = tmpfiles.read(self.home, outside, "--text-file")
        self.assertIsNone(content)
        self.assertIn("--text-file must be under", error)
        tmpfiles.discard(self.home, outside)
        self.assertTrue(os.path.exists(outside))
        escape = os.path.join(self.home, "state", "tmp", "escape.txt")
        os.makedirs(os.path.dirname(escape), exist_ok=True)
        os.symlink(outside, escape)
        tmpfiles.discard(self.home, escape)
        self.assertTrue(os.path.exists(outside))

    def test_group_tag(self):
        self.assertEqual(tmpfiles.group_tag("Work Stuff"), "work-stuff")
        self.assertEqual(tmpfiles.group_tag("group-2"), "group-2")
        self.assertEqual(tmpfiles.group_tag(None), "nogroup")
        self.assertEqual(tmpfiles.group_tag("---"), "nogroup")
