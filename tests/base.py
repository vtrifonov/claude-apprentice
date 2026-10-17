import os
import shutil
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

_CLEARED = ("CLAUDE_CONFIG_DIR", "APPRENTICE_DISABLE", "APPRENTICE_INTERNAL", "CLAUDE_CODE_ENTRYPOINT")


class ApprenticeTestCase(unittest.TestCase):
    def setUp(self):
        real_home = os.path.realpath(os.path.expanduser("~"))
        self._saved_env = {}
        for key in ("APPRENTICE_HOME", "HOME") + _CLEARED:
            self._saved_env[key] = os.environ.get(key)
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.fake_home = os.path.realpath(tempfile.mkdtemp())
        self._tmp_dirs = [self.home, self.fake_home]
        for path in self._tmp_dirs:
            if path.startswith(real_home + os.sep) or path == real_home:
                self._restore_and_clean()
                self.fail("temp dir resolves under the real home: isolation unsafe")
        os.environ["APPRENTICE_HOME"] = self.home
        os.environ["HOME"] = self.fake_home
        for key in _CLEARED:
            os.environ.pop(key, None)

    def _restore_and_clean(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for path in self._tmp_dirs:
            shutil.rmtree(path, ignore_errors=True)

    def tearDown(self):
        self._restore_and_clean()

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path
