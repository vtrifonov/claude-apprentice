"""Stopwords and system-dictionary lookup. The only reader of stopwords.txt and the dictionary file."""
from __future__ import annotations

import os
from typing import Dict, FrozenSet, Optional

STOPWORDS_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "data", "stopwords.txt")
DICT_PATH = "/usr/share/dict/words"

_stopwords: Optional[FrozenSet[str]] = None
_dictionaries: Dict[str, Optional[FrozenSet[str]]] = {}


def stopwords() -> FrozenSet[str]:
    global _stopwords
    if _stopwords is None:
        with open(STOPWORDS_PATH, encoding="utf-8") as fh:
            _stopwords = frozenset(line.strip() for line in fh if line.strip())
    return _stopwords


def dictionary() -> Optional[FrozenSet[str]]:
    """The system word list lowercased, or None when it cannot be read ("cannot judge")."""
    path = DICT_PATH
    if path not in _dictionaries:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                _dictionaries[path] = frozenset(line.strip().lower() for line in fh if line.strip())
        except OSError:
            _dictionaries[path] = None
    return _dictionaries[path]


def known_word(w: str) -> Optional[bool]:
    w = w.lower()
    if w in stopwords():
        return True
    d = dictionary()
    if d is None:
        return None
    return w in d
