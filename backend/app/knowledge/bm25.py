from __future__ import annotations

import math
import re
from collections import Counter


_TOKEN_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+|[a-zA-Z0-9_-]+")
_CJK_PATTERN = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]+$")
_NORMALIZATIONS = {
    "七天": "7天",
    "七日": "7天",
    "没有": "无",
    "没更新": "无更新",
    "怎么办": "",
    "可以吗": "",
}


def normalize_search_text(value: str) -> str:
    normalized = "".join(value.casefold().split())
    for source, target in _NORMALIZATIONS.items():
        normalized = normalized.replace(source, target)
    return normalized


def tokenize(value: str) -> list[str]:
    """Tokenize Chinese as character bigrams and keep Latin/number terms intact."""
    tokens: list[str] = []
    for part in _TOKEN_PATTERN.findall(normalize_search_text(value)):
        if not _CJK_PATTERN.fullmatch(part):
            tokens.append(part)
        elif len(part) == 1:
            tokens.append(part)
        else:
            tokens.extend(part[index : index + 2] for index in range(len(part) - 1))
    return tokens


def bm25_scores(
    query: str,
    documents: list[str],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """Return Okapi BM25 scores for each document in corpus order."""
    if not documents:
        return []

    tokenized_documents = [tokenize(document) for document in documents]
    query_terms = set(tokenize(query))
    if not query_terms:
        return [0.0] * len(documents)

    document_count = len(tokenized_documents)
    average_length = (
        sum(len(tokens) for tokens in tokenized_documents) / document_count
    ) or 1.0
    document_frequency: Counter[str] = Counter()
    for tokens in tokenized_documents:
        document_frequency.update(set(tokens))

    scores: list[float] = []
    for tokens in tokenized_documents:
        frequencies = Counter(tokens)
        document_length = len(tokens)
        length_normalization = k1 * (
            1.0 - b + b * document_length / average_length
        )
        score = 0.0
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if frequency == 0:
                continue
            frequency_in_corpus = document_frequency[term]
            inverse_document_frequency = math.log(
                1.0
                + (document_count - frequency_in_corpus + 0.5)
                / (frequency_in_corpus + 0.5)
            )
            score += inverse_document_frequency * (
                frequency * (k1 + 1.0) / (frequency + length_normalization)
            )
        scores.append(score)
    return scores
