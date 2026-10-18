import glob
import os
import re

from tests.base import REPO_ROOT, ApprenticeTestCase

SKILL = os.path.join(REPO_ROOT, "skills", "distill", "SKILL.md")
SUGGEST = os.path.join(REPO_ROOT, "commands", "suggest.md")

DEFAULT_OPTIONS = ["Accept (Recommended)", "Decline", "Later", "Edit or rescope"]
RESCOPE_TRACKED = ["Rescope to global CLAUDE.md (Recommended)", "Accept into the tracked file", "Decline", "Later"]
RESCOPE_LOCAL = ["Rescope to global CLAUDE.md (Recommended)", "Decline", "Later", "Edit or rescope"]


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _frontmatter(text):
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not match:
        return None
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _in_order(test, text, items):
    pos = -1
    for item in items:
        found = text.find(item, pos + 1)
        test.assertNotEqual(found, -1, "missing or out of order: %s" % item)
        pos = found


class CommandsTest(ApprenticeTestCase):
    def test_every_command_has_frontmatter(self):
        paths = sorted(glob.glob(os.path.join(REPO_ROOT, "commands", "*.md")))
        self.assertTrue(paths)
        for path in paths:
            fields = _frontmatter(_read(path))
            self.assertIsNotNone(fields, path)
            self.assertIn("description", fields, path)
            self.assertIn("allowed-tools", fields, path)

    def test_suggest_command(self):
        text = _read(SUGGEST)
        fields = _frontmatter(text)
        self.assertEqual(fields["description"], "Review learned-rule candidates one by one")
        for name in ("AskUserQuestion", "Skill"):
            self.assertIn(name, fields["allowed-tools"])
        for needle in ("apprentice:distill", "AskUserQuestion", "Skill", "scripts/distill.py",
                       "scripts/preclassify.py", "apprentice:distill skill not found"):
            self.assertIn(needle, text)

    def test_skill_frontmatter(self):
        fields = _frontmatter(_read(SKILL))
        self.assertEqual(fields["name"], "distill")
        self.assertTrue(fields["description"].startswith("Judgment step for /apprentice:suggest"))

    def test_skill_picker_options(self):
        text = _read(SKILL)
        _in_order(self, text, DEFAULT_OPTIONS)
        _in_order(self, text, RESCOPE_TRACKED)
        _in_order(self, text, RESCOPE_LOCAL)

    def test_skill_stop_words_and_single_question(self):
        text = _read(SKILL)
        self.assertIn("`stop`", text)
        self.assertIn("`enough`", text)
        self.assertLessEqual(text.count("questions: ["), 1)
        self.assertNotRegex(text.lower(), r"second question|two questions|additional question")

    def test_skill_forbids_private_files(self):
        text = _read(SKILL)
        for needle in ("events/", "ledger/", "state/pack-private/"):
            self.assertIn(needle, text)
