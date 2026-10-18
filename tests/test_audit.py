import os

from apprentice import audit
from tests.base import ApprenticeTestCase

_FIXTURES = os.path.join(os.path.dirname(os.path.realpath(__file__)), "fixtures", "claude-md")
_DF = {"deploy": 5, "panel": 1, "lane": 1, "budget": 3}
_SUMMARY = "the budget panel and lane deploy"


def _fixture(name):
    return os.path.join(_FIXTURES, name)


class DocumentFrequencyTest(ApprenticeTestCase):
    def test_document_frequency_counts_once_per_text(self):
        df = audit.document_frequency(["deploy the panel", "deploy panel panel", "lane"])
        self.assertEqual(df, {"deploy": 2, "panel": 2, "lane": 1})

    def test_document_frequency_empty(self):
        self.assertEqual(audit.document_frequency([]), {})


class RarestWordsTest(ApprenticeTestCase):
    def test_rarest_words_df_order_and_ties(self):
        self.assertEqual(audit.rarest_words(_SUMMARY, _DF), ["lane", "panel", "budget"])

    def test_rarest_words_unseen_token_sorts_first(self):
        df = {"deploy": 1, "panel": 2}
        self.assertEqual(audit.rarest_words("deploy panel zebra", df)[0], "zebra")

    def test_rarest_words_fewer_than_k(self):
        self.assertEqual(audit.rarest_words("the panel", _DF), ["panel"])


class AuditTargetTest(ApprenticeTestCase):
    def test_present_same_bullet(self):
        result = audit.audit_target(_SUMMARY, _fixture("present.md"), True, _DF)
        self.assertEqual(result, "already present")

    def test_present_across_bullet_continuation_lines(self):
        # lane is on the bullet's first line, release and candidate on its continuation line
        df = {"budget": 4, "panel": 4, "lane": 4, "release": 1, "candidate": 1}
        summary = "release candidate lane"
        self.assertEqual(audit.rarest_words(summary, df), ["candidate", "release", "lane"])
        self.assertEqual(audit.audit_target(summary, _fixture("present.md"), True, df), "already present")

    def test_not_present_when_spread_over_two_bullets(self):
        df = {"release": 1, "naming": 1, "panel": 1}
        result = audit.audit_target("release naming panel", _fixture("present.md"), True, df)
        self.assertEqual(result, "new")

    def test_contradicts_quotes_line_when_allowed(self):
        result = audit.audit_target(_SUMMARY, _fixture("contradicts.md"), True, _DF)
        self.assertEqual(result, "contradicts: - Never touch the panel lane during release.")

    def test_contradicts_count_only_when_withheld(self):
        result = audit.audit_target(_SUMMARY, _fixture("contradicts.md"), False, _DF)
        self.assertEqual(result, "contradicts: 1 line(s) in a target from other account groups (text withheld)")

    def test_new_when_missing_file(self):
        self.assertEqual(audit.audit_target(_SUMMARY, _fixture("absent.md"), True, _DF), "new")
        self.assertEqual(audit.audit_target(_SUMMARY, None, True, _DF), "new")

    def test_new_when_one_word_only(self):
        self.assertEqual(audit.audit_target(_SUMMARY, _fixture("none.md"), True, _DF), "new")

    def test_marked_section_rule_line_is_searched(self):
        df = {"commit": 1, "subjects": 1, "short": 1}
        result = audit.audit_target("short commit subjects", _fixture("section.md"), True, df)
        self.assertEqual(result, "already present")

    def test_malformed_markers_still_audited(self):
        df = {"commit": 1, "subjects": 1, "short": 1}
        result = audit.audit_target("short commit subjects", _fixture("malformed.md"), True, df)
        self.assertEqual(result, "already present")
