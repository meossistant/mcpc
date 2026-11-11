"""BM25 scoring checked against hand-computed values."""

from __future__ import annotations

import math

import pytest

from mcpc.search.bm25 import BM25

K1 = 1.5
B = 0.75


def _idf(n: int, df: int) -> float:
    return math.log((n - df + 0.5) / (df + 0.5) + 1)


def _term(idf: float, tf: int, length: int, avgdl: float) -> float:
    return idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / avgdl))


def test_hand_computed_scores() -> None:
    docs = [["a", "b", "a"], ["b", "c"], ["c"]]
    bm25 = BM25(docs)
    avgdl = 2.0  # (3 + 2 + 1) / 3

    scores = bm25.score(["a", "b"])

    expected0 = _term(_idf(3, 1), 2, 3, avgdl) + _term(_idf(3, 2), 1, 3, avgdl)
    expected1 = _term(_idf(3, 2), 1, 2, avgdl)
    assert scores == pytest.approx([expected0, expected1, 0.0])
    # By hand: a = ln(8/3)*5/4.0625 = 1.20716; b = ln(1.6)*2.5/3.0625 = 0.38367; sum = 1.59085
    assert _idf(3, 1) == pytest.approx(math.log(8 / 3))
    assert scores[0] == pytest.approx(1.590851, abs=1e-5)


def test_repeated_query_tokens_count_once() -> None:
    bm25 = BM25([["a", "b"], ["b"]])
    assert bm25.score(["a", "a", "a"]) == bm25.score(["a"])


def test_unknown_terms_score_zero() -> None:
    assert BM25([["a"], ["b"]]).score(["zzz"]) == [0.0, 0.0]


def test_empty_corpus_and_empty_query() -> None:
    assert BM25([]).score(["a"]) == []
    assert BM25([["a"], ["b"]]).score([]) == [0.0, 0.0]


def test_empty_documents() -> None:
    assert BM25([[], []]).score(["a"]) == [0.0, 0.0]
    scores = BM25([[], ["a"]]).score(["a"])
    assert scores[0] == 0.0 and scores[1] > 0


def test_idf_is_never_negative_for_ubiquitous_terms() -> None:
    scores = BM25([["a"], ["a"], ["a"]]).score(["a"])
    assert all(s > 0 for s in scores)


def test_shorter_document_scores_higher_for_same_tf() -> None:
    scores = BM25([["a", "x", "y", "z"], ["a"]]).score(["a"])
    assert scores[1] > scores[0]


def test_more_occurrences_score_higher() -> None:
    scores = BM25([["a", "a", "a", "x"], ["a", "x", "y", "z"]]).score(["a"])
    assert scores[0] > scores[1]


def test_custom_parameters() -> None:
    docs = [["a", "a"], ["b"]]
    flat = BM25(docs, k1=1.5, b=0.0).score(["a"])
    assert flat[0] == pytest.approx(_idf(2, 1) * 2 * 2.5 / (2 + 1.5))
