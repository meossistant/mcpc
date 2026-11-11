"""Tokenizer behaviour from plan section 7."""

from __future__ import annotations

import pytest

from mcpc.search.tokenize import STOPWORDS, tokenize


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("searchIssues", ["search", "issue", "searchissues"]),
        ("SearchIssues", ["search", "issue", "searchissues"]),
        ("HTTPServer", ["http", "server", "httpserver"]),
        ("search_issues", ["search", "issue", "searchissues"]),
        ("search-issues", ["search", "issue", "searchissues"]),
        ("repos.list", ["repo", "list", "reposlist"]),
        ("owner/repo", ["owner", "repo", "ownerrepo"]),
        ("getHTTPResponseCode", ["get", "http", "response", "code", "gethttpresponsecode"]),
    ],
)
def test_identifier_splitting_emits_joined_form(text: str, expected: list[str]) -> None:
    assert tokenize(text) == expected


def test_lowercases_and_splits_on_punctuation() -> None:
    assert tokenize("Hello, World!") == ["hello", "world"]


def test_single_word_has_no_joined_duplicate() -> None:
    assert tokenize("book") == ["book"]


def test_stopwords_are_dropped() -> None:
    assert tokenize("find a book by the author") == ["find", "book", "author"]
    assert (
        tokenize("a an the and or of to in on for by with is are be it this that from as at me my i you your")
        == []
    )


def test_stopword_list_contents() -> None:
    assert {"a", "the", "your", "me"} <= STOPWORDS


@pytest.mark.parametrize(
    ("word", "stem"),
    [
        ("libraries", "library"),
        ("queries", "query"),
        ("boxes", "box"),
        ("searches", "search"),
        ("wishes", "wish"),
        ("buzzes", "buzz"),
        ("books", "book"),
        ("issues", "issue"),
        ("files", "file"),
        ("class", "class"),
        ("searching", "search"),
        ("listing", "list"),
        ("searched", "search"),
        ("created", "creat"),
    ],
)
def test_light_stemming(word: str, stem: str) -> None:
    assert tokenize(word) == [stem]


@pytest.mark.parametrize("word", ["bed", "red", "sing", "king", "ring", "bus", "yes"])
def test_short_stems_are_kept(word: str) -> None:
    """Stripping only happens when at least three characters remain."""
    assert tokenize(word) == [word]


def test_plural_strip_on_short_words() -> None:
    assert tokenize("ties pies") == ["tie", "pie"]


def test_string_is_not_stemmed_to_nothing() -> None:
    assert tokenize("string") == ["string"]


def test_digits_are_kept() -> None:
    assert tokenize("python3 v2 2024") == ["python3", "v2", "2024"]
    assert tokenize("web3Provider") == ["web3", "provider", "web3provider"]


def test_non_ascii_words_kept_without_stemming() -> None:
    assert tokenize("Café livres") == ["café", "livre"]
    assert tokenize("тесты поиска") == ["тесты", "поиска"]
    assert tokenize("検索") == ["検索"]


def test_deterministic() -> None:
    text = "Search GitHub issues, by labelName or repo_owner."
    assert tokenize(text) == tokenize(text)


def test_empty_and_punctuation_only() -> None:
    assert tokenize("") == []
    assert tokenize("  ...  ") == []
    assert tokenize("_-/") == []
