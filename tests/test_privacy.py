import os
import re
import unittest

from tests.base import REPO_ROOT

# Banned tokens are assembled from short fragments so no literal appears in the repo.
_EMPLOYER = re.compile("".join(["pa", "y", "ha", "wk"]), re.IGNORECASE)
_FIRST_NAME = re.compile("".join(["v", "a", "si", "l"]), re.IGNORECASE)
_TICKET_A = re.compile(r"\b" + "".join(["F", "T"]) + "-")
_TICKET_B = re.compile(r"\b" + "".join(["C", "T"]) + "-")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_PATTERNS = (_EMPLOYER, _TICKET_A, _TICKET_B, _FIRST_NAME, _EMAIL)
_SKIP_DIRS = {".git", "__pycache__"}
_MAX_FIXTURE_LINE = 2000


def _walk():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name in _SKIP_DIRS:  # a worktree's .git is a file, not a directory
                continue
            yield os.path.join(dirpath, name)


def _rel(path):
    return os.path.relpath(path, REPO_ROOT)


class PrivacyGateTest(unittest.TestCase):
    def test_no_banned_tokens(self):
        hits = []
        for path in _walk():
            with open(path, "rb") as fh:
                data = fh.read()
            text = data.decode("utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if any(p.search(line) for p in _PATTERNS):
                    hits.append("%s:%d" % (_rel(path), lineno))
        self.assertEqual(hits, [], "banned token at: " + ", ".join(hits))

    def test_jsonl_only_in_fixtures(self):
        fixtures = os.path.join(REPO_ROOT, "tests", "fixtures") + os.sep
        stray = [_rel(p) for p in _walk() if p.endswith(".jsonl") and not p.startswith(fixtures)]
        self.assertEqual(stray, [], "jsonl outside tests/fixtures: " + ", ".join(stray))

    def test_fixture_lines_short(self):
        fixtures = os.path.join(REPO_ROOT, "tests", "fixtures") + os.sep
        long_lines = []
        for path in _walk():
            if not path.startswith(fixtures):
                continue
            with open(path, "rb") as fh:
                for lineno, line in enumerate(fh, 1):
                    if len(line.rstrip(b"\r\n")) > _MAX_FIXTURE_LINE:
                        long_lines.append("%s:%d" % (_rel(path), lineno))
        self.assertEqual(long_lines, [], "fixture line over 2000 bytes: " + ", ".join(long_lines))


if __name__ == "__main__":
    unittest.main()
