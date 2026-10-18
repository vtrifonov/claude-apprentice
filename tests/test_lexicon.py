import os
import re
import unittest
from unittest import mock

from apprentice import lexicon
from tests.base import ApprenticeTestCase


class LexiconTests(ApprenticeTestCase):
    def missing_dict(self):
        return mock.patch.object(lexicon, "DICT_PATH", os.path.join(self.home, "no-such-words"))

    def test_stopwords_nonempty_lowercase_and_contains_the(self):
        words = lexicon.stopwords()
        self.assertIn("the", words)
        self.assertGreaterEqual(len(words), 200)
        for word in words:
            self.assertRegex(word, re.compile(r"^[a-z']+$"))

    def test_known_word_stopword_true_without_dictionary(self):
        with self.missing_dict():
            self.assertIs(lexicon.known_word("The"), True)

    def test_known_word_none_when_dictionary_missing(self):
        with self.missing_dict():
            self.assertIsNone(lexicon.known_word("zorbalice"))

    def test_known_word_case_insensitive_with_temp_dictionary(self):
        path = self.write(os.path.join(self.home, "words"), "Widget\npanel\n")
        with mock.patch.object(lexicon, "DICT_PATH", path):
            self.assertIs(lexicon.known_word("WIDGET"), True)
            self.assertIs(lexicon.known_word("Panel"), True)
            self.assertIs(lexicon.known_word("widgetz"), False)

    def test_dictionary_cached_per_path(self):
        first = self.write(os.path.join(self.home, "first"), "alpha\n")
        second = self.write(os.path.join(self.home, "second"), "beta\n")
        with mock.patch.object(lexicon, "DICT_PATH", first):
            self.assertIs(lexicon.known_word("alpha"), True)
            self.write(first, "beta\n")
            self.assertIs(lexicon.known_word("alpha"), True)
        with mock.patch.object(lexicon, "DICT_PATH", second):
            self.assertIs(lexicon.known_word("alpha"), False)

    def test_empty_string_does_not_raise(self):
        path = self.write(os.path.join(self.home, "words"), "alpha\n")
        with mock.patch.object(lexicon, "DICT_PATH", path):
            self.assertIs(lexicon.known_word(""), False)
        with self.missing_dict():
            self.assertIsNone(lexicon.known_word(""))


if __name__ == "__main__":
    unittest.main()
