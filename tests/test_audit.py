import os

from apprentice import audit, config
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


class AuditNegationTest(ApprenticeTestCase):
    def audit(self, summary, body, quote_allowed=True, cfg=None):
        path = self.write(os.path.join(self.home, "CLAUDE.md"), body)
        return audit.audit_target(summary, path, quote_allowed, {}, cfg)

    def test_three_hits_with_negation_mismatch_contradicts(self):
        result = self.audit("Merge release branches without approval",
                            "- Never merge release branches without approval\n")
        self.assertEqual(result, "contradicts: - Never merge release branches without approval")

    def test_dont_and_do_not_contradict_positive_candidate(self):
        for body in ("- Don't deploy staging on fridays\n", "- Do not deploy staging on fridays\n",
                     "- You shouldn't deploy staging on fridays\n", "- You should not deploy staging on fridays\n",
                     "- Avoid deploying staging on fridays\n"):
            result = self.audit("Deploy staging on fridays", body)
            self.assertTrue(result.startswith("contradicts: "), body)

    def test_three_hits_with_matching_negation_is_present(self):
        result = self.audit("Never merge release branches without approval",
                            "- Never merge release branches without approval\n")
        self.assertEqual(result, "already present")

    def test_scan_continues_after_contradiction(self):
        body = "- Never deploy staging fridays\n- Always deploy staging fridays\n"
        self.assertEqual(self.audit("Deploy staging fridays", body), "already present")

    def test_short_summary_needs_all_its_words(self):
        self.assertEqual(self.audit("Use tabs", "- Use tabs in python files\n"), "already present")
        self.assertEqual(self.audit("Use tabs", "- Use spaces in python files\n"), "new")

    def test_contradicts_line_is_scrubbed_and_capped(self):
        secret = "pass" + "word=" + "hunter2hunter2"
        body = "- Never deploy staging fridays " + secret + " " + "x" * 300 + "\n"
        result = self.audit("Deploy staging fridays", body, cfg=config.load_config())
        self.assertTrue(result.startswith("contradicts: - Never deploy staging fridays"))
        self.assertNotIn("hunter2", result)
        self.assertLessEqual(len(result), len("contradicts: ") + 200)
