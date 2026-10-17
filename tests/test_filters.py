import hashlib

from apprentice import filters
from tests.base import ApprenticeTestCase


class FiltersTest(ApprenticeTestCase):
    def test_rule1_drop_angle(self):
        self.assertEqual(filters.classify("<command-name>x</command-name>")["outcome"], "drop")

    def test_rule1_system_reminder_early_drop(self):
        self.assertEqual(filters.classify("Note: system-reminder inside")["outcome"], "drop")

    def test_rule1_system_reminder_late_kept(self):
        text = "x" * 50 + " system-reminder"
        self.assertEqual(filters.classify(text)["outcome"], "prompt")

    def test_rule2_drop_prefixes(self):
        for text in ("Continue this fork with the following request: x",
                     "Caveat: The messages below were generated",
                     "This session is being continued from a previous one"):
            self.assertEqual(filters.classify(text)["outcome"], "drop", text)
        self.assertEqual(filters.classify("Please continue this fork")["outcome"], "prompt")

    def test_rule3_skill_command(self):
        result = filters.classify("/apprentice:distill now please")
        self.assertEqual(result["outcome"], "machine")
        self.assertEqual(result["command"], "/apprentice:distill")
        self.assertEqual(filters.classify("see /tmp/file")["outcome"], "prompt")
        self.assertEqual(filters.classify("/usr/bin/python3 run")["outcome"], "prompt")

    def test_rule4_image_only_text_none(self):
        result = filters.classify("[Image #3]")
        self.assertEqual(result["outcome"], "prompt")
        self.assertTrue(result["image_only"])
        self.assertIsNone(result["text"])
        self.assertEqual(result["tags"], ["image_only"])
        self.assertFalse(filters.classify("[Image #3] what is this")["image_only"])

    def test_rule5_long_pasted_300(self):
        result = filters.classify("a " * 800)
        self.assertTrue(result["pasted"])
        self.assertEqual(len(result["text"]), 300)
        self.assertIn("pasted", result["tags"])
        self.assertFalse(filters.classify("a" * 1500)["pasted"])

    def test_rule5_three_quote_lines(self):
        self.assertTrue(filters.classify("> a\n> b\n> c\nthoughts")["pasted"])
        self.assertFalse(filters.classify("> a\n> b\nthoughts")["pasted"])

    def test_rule5_code_fence(self):
        self.assertTrue(filters.classify("look\n```\ncode\n```")["pasted"])

    def test_rule5_stack_trace(self):
        self.assertTrue(filters.classify("err\n    at foo (x.js:1)")["pasted"])
        self.assertTrue(filters.classify('err\n  File "x.py", line 1')["pasted"])
        self.assertTrue(filters.classify("Traceback (most recent call last):")["pasted"])
        self.assertFalse(filters.classify("look at that")["pasted"])

    def test_rule6_ack_register(self):
        result = filters.classify("continue")
        self.assertEqual(result["outcome"], "prompt")
        self.assertEqual(result["register"], "ack")
        self.assertEqual(result["tags"], [])

    def test_rule6_ack_with_punct(self):
        self.assertEqual(filters.classify("ok!")["register"], "ack")
        self.assertEqual(filters.classify("Yes.")["register"], "ack")
        self.assertNotEqual(filters.classify("ok but why")["register"], "ack")

    def test_rule7_empty_drop(self):
        self.assertEqual(filters.classify("")["outcome"], "drop")
        self.assertEqual(filters.classify("  \n ")["outcome"], "drop")

    def test_rule_order_machine_before_pasted(self):
        result = filters.classify("/compact " + "x" * 2000)
        self.assertEqual(result["outcome"], "machine")

    def test_tag_but_first_word_only(self):
        self.assertIn("correction", filters.classify("but why")["tags"])
        self.assertIn("correction", filters.classify("Wait, stop")["tags"])
        self.assertNotIn("correction", filters.classify("nothing but trouble")["tags"])
        self.assertNotIn("correction", filters.classify("butter it up")["tags"])

    def test_tag_correction_phrases(self):
        for phrase in ("why did we", "why do we", "can't we", "shouldn't we", "didn't we",
                       "isn't it", "what do you mean", "are you sure", "i said",
                       "i thought we", "again", "undo", "revert", "the fix is different"):
            self.assertIn("correction", filters.classify("so " + phrase + " x")["tags"], phrase)
        self.assertNotIn("correction", filters.classify("so i saidx")["tags"])

    def test_tag_again_sets_correction_not_strong(self):
        result = filters.classify("run the build again")
        self.assertEqual(result["tags"], ["correction"])
        self.assertFalse(result["strong_marker"])

    def test_tag_standing(self):
        for phrase in ("remember", "store in your memory", "always", "never", "from now on",
                       "i said"):
            self.assertIn("standing", filters.classify("hey " + phrase + " x")["tags"], phrase)
        self.assertNotIn("standing", filters.classify("hey neverland")["tags"])

    def test_tag_decision(self):
        for text in ("let's do it", "go with b", "approve the plan", "i allow it", "q1 - yes",
                     "2: no", "3 > x", "a, please", "b) fine", "c - ok"):
            self.assertIn("decision", filters.classify(text)["tags"], text)
        for text in ("apple pie", "later then", "e is fine"):
            self.assertNotIn("decision", filters.classify(text)["tags"], text)

    def test_tag_status_ask(self):
        for phrase in ("where are we", "what is remaining", "any progress", "did you",
                       "is it running"):
            self.assertIn("status_ask", filters.classify(phrase + " now")["tags"], phrase)
        self.assertNotIn("status_ask", filters.classify("hello there")["tags"])

    def test_tag_report(self):
        for word in ("merged", "closed", "done"):
            self.assertIn("report", filters.classify("it is " + word + " now")["tags"], word)
        self.assertNotIn("report", filters.classify("undone")["tags"])

    def test_tags_sorted(self):
        tags = filters.classify("but always done")["tags"]
        self.assertEqual(tags, sorted(tags))
        self.assertEqual(tags, ["correction", "report", "standing"])

    def test_strong_marker_phrases(self):
        for phrase in ("but i said", "i thought we", "store in your memory", "remember to",
                       "remember that", "from now on"):
            self.assertTrue(filters.classify("hey " + phrase + " x")["strong_marker"], phrase)
        self.assertFalse(filters.classify("remember")["strong_marker"])
        self.assertFalse(filters.classify("again")["strong_marker"])

    def test_register_rules(self):
        self.assertEqual(filters.classify("where are we")["register"], "status_ask")
        self.assertEqual(filters.classify("but no")["register"], "pushback")
        self.assertEqual(filters.classify("it is merged")["register"], "report")
        self.assertEqual(filters.classify("how does this work?")["register"], "question")
        self.assertEqual(filters.classify("add a test")["register"], "direction")

    def test_sha1_of_full_text_before_cut(self):
        text = "a " * 800
        result = filters.classify(text)
        self.assertEqual(result["sha1"], hashlib.sha1(text.encode("utf-8")).hexdigest())
        self.assertEqual(result["len"], len(text))
        self.assertEqual(result["words"], 800)

    def test_text_cap_4000(self):
        text = "a" * 1500
        result = filters.classify(text)
        self.assertEqual(result["text"], text)
        self.assertLessEqual(len(result["text"]), 4000)

    def test_normalized_sha1_collapses_ws(self):
        self.assertEqual(filters.normalized_sha1("  Hello \n  World "),
                         filters.normalized_sha1("hello world"))
        self.assertEqual(filters.normalized_sha1("hello world"),
                         hashlib.sha1(b"hello world").hexdigest())
