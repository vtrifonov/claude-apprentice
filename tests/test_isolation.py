import os
import tempfile
import unittest

from tests import hookrun
from tests.base import isolated_env


def _under_temp(path):
    temp = os.path.realpath(tempfile.gettempdir())
    return os.path.realpath(path).startswith(temp + os.sep)


class IsolationGuardTest(unittest.TestCase):
    """Outside ApprenticeTestCase no code may reach the real ~/.claude/apprentice."""

    def test_package_sets_apprentice_home_under_temp(self):
        self.assertTrue(_under_temp(os.environ.get("APPRENTICE_HOME", "")), os.environ.get("APPRENTICE_HOME"))

    def test_hook_helper_env_is_under_temp(self):
        env = hookrun.hook_env({})
        self.assertTrue(_under_temp(env["APPRENTICE_HOME"]), env["APPRENTICE_HOME"])

    def test_isolated_env_is_under_temp(self):
        env = isolated_env()
        self.assertTrue(_under_temp(env["APPRENTICE_HOME"]), env["APPRENTICE_HOME"])
        self.assertTrue(_under_temp(env["HOME"]), env["HOME"])
