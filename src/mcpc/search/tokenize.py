"""Tokenizer for the BM25 search index (plan §7). Pure and deterministic."""

from __future__ import annotations

import re

STOPWORDS: frozenset[str] = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "by",
        "with",
        "is",
        "are",
        "be",
        "it",
        "this",
        "that",
        "from",
        "as",
        "at",
        "me",
        "my",
        "i",
        "you",
        "your",
    ]
)

# A chunk is a whitespace/punctuation-delimited run that may contain identifier separators.
_CHUNK_RE = re.compile(r"[\w\-./]+")
_SEPARATOR_RE = re.compile(r"[_\-./]+")
# Boundaries inside camelCase / PascalCase: "searchIssues" -> search|Issues, "HTTPServer" -> HTTP|Server.
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_VOWELS = frozenset("aeiou")
_MIN_STEM = 3


def tokenize(text: str) -> list[str]:
    """Split text into lowercase, stemmed search tokens.

    Identifiers (camelCase, snake_case, kebab-case, dotted, slashed) are split into parts and
    also emit their joined lowercase form: ``searchIssues`` -> ``search``, ``issue``,
    ``searchissues``. The joined form is not stemmed. Stopwords are dropped, digits are kept,
    and non-ASCII words are kept unstemmed.
    """
    tokens: list[str] = []
    for chunk in _CHUNK_RE.findall(text):
        parts = _split_chunk(chunk)
        for part in parts:
            if part and part not in STOPWORDS:
                tokens.append(_stem(part))
        if len(parts) > 1:
            tokens.append("".join(parts))
    return tokens


def _split_chunk(chunk: str) -> list[str]:
    """Split one chunk into lowercase parts (separators and camelCase boundaries)."""
    parts: list[str] = []
    for piece in _SEPARATOR_RE.split(chunk):
        if not piece:
            continue
        parts.extend(p.lower() for p in _CAMEL_RE.sub(" ", piece).split() if p)
    return parts


def _stem(word: str) -> str:
    """Light suffix stripping: plural step first, then ``ing`` / ``ed``. ASCII words only."""
    if not word.isascii():
        return word
    word = _strip_plural(word)
    return _strip_verb_suffix(word)


def _strip_plural(word: str) -> str:
    if word.endswith("ies") and len(word) - 3 >= _MIN_STEM:
        return word[:-3] + "y"
    if word.endswith("es") and len(word) - 2 >= _MIN_STEM:
        stem = word[:-2]
        if stem.endswith(("s", "x", "z", "ch", "sh")):
            return stem
    # "ss" (class), "us" (status) and "is" (analysis) are not plurals.
    if word.endswith("s") and not word.endswith(("ss", "us", "is")) and len(word) - 1 >= _MIN_STEM:
        return word[:-1]
    return word


def _strip_verb_suffix(word: str) -> str:
    for suffix in ("ing", "ed"):
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            # Require a vowel so "string" / "spring" are left alone.
            if len(stem) >= _MIN_STEM and _VOWELS.intersection(stem):
                return stem
    return word
