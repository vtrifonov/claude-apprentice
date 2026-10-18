"""Run-wide isolation: no test, helper or subprocess may fall back to the real ~/.claude/apprentice."""
import atexit
import os
import shutil
import tempfile

REAL_HOME = os.path.realpath(os.path.expanduser("~"))


def _scratch(prefix):
    path = os.path.realpath(tempfile.mkdtemp(prefix=prefix))
    atexit.register(shutil.rmtree, path, True)
    return path


def _unsafe(path):
    real = os.path.realpath(os.path.expanduser(path))
    return real == REAL_HOME or real.startswith(REAL_HOME + os.sep)


RUN_HOME = _scratch("apprentice-run-home-")
FAKE_HOME = _scratch("apprentice-run-fake-home-")

if not os.environ.get("APPRENTICE_HOME") or _unsafe(os.environ["APPRENTICE_HOME"]):
    os.environ["APPRENTICE_HOME"] = RUN_HOME
