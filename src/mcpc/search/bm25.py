"""Okapi BM25 over pre-tokenized documents. Pure Python, no dependencies."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence


class BM25:
    """BM25 index over a fixed corpus.

    IDF is ``ln((N - df + 0.5) / (df + 0.5) + 1)``, which is never negative.
    """

    def __init__(self, docs: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._n = len(docs)
        self._lengths = [len(d) for d in docs]
        self._avgdl = sum(self._lengths) / self._n if self._n else 0.0
        self._tf: list[Counter[str]] = [Counter(d) for d in docs]
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        self._idf = {term: math.log((self._n - n + 0.5) / (n + 0.5) + 1) for term, n in df.items()}
        # term -> [(doc index, term frequency)], so scoring only visits docs containing a query term.
        self._postings: dict[str, list[tuple[int, int]]] = {}
        for i, tf in enumerate(self._tf):
            for term, count in tf.items():
                self._postings.setdefault(term, []).append((i, count))

    def score(self, query_tokens: Sequence[str]) -> list[float]:
        """Return one BM25 score per document. Repeated query tokens count once."""
        scores = [0.0] * self._n
        if not self._n:
            return scores
        for term in dict.fromkeys(query_tokens):
            idf = self._idf.get(term)
            if idf is None:
                continue
            for i, tf in self._postings[term]:
                norm = 1 - self.b + self.b * self._lengths[i] / self._avgdl if self._avgdl else 1.0
                scores[i] += idf * tf * (self.k1 + 1) / (tf + self.k1 * norm)
        return scores
