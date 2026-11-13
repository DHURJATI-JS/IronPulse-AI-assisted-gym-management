"""Deterministic local text embeddings for pgvector (no model download, works offline on every OS).

Feature hashing of word unigrams, bigrams and character trigrams into ``EMBEDDING_DIM`` signed
buckets, sub-linear TF weighting and L2 normalisation. Cosine distance (``<=>``) then behaves like a
fuzzy lexical similarity - good enough for exercise search and assistant memory recall.
"""

from __future__ import annotations

import hashlib
import math
import re

from app.models.base import EMBEDDING_DIM

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is", "are", "be", "it", "this", "that",
    "my", "me", "i", "you", "your", "at", "as", "by", "from", "do", "does", "can", "please", "how", "what",
}


def _bucket(token: str) -> tuple[int, float]:
    h = hashlib.blake2b(token.encode(), digest_size=8).digest()
    idx = int.from_bytes(h[:4], "little") % EMBEDDING_DIM
    sign = 1.0 if h[4] & 1 else -1.0
    return idx, sign


def embed(text: str) -> list[float]:
    words = [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]
    counts: dict[str, float] = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1.0
        padded = f"#{w}#"
        for i in range(len(padded) - 2):
            tri = "c:" + padded[i : i + 3]
            counts[tri] = counts.get(tri, 0) + 0.35
    for a, b in zip(words, words[1:]):
        bg = f"b:{a}_{b}"
        counts[bg] = counts.get(bg, 0) + 0.8
    vec = [0.0] * EMBEDDING_DIM
    for tok, c in counts.items():
        idx, sign = _bucket(tok)
        vec[idx] += sign * (1.0 + math.log(c)) if c >= 1 else sign * c
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def embed_many(texts: list[str]) -> list[list[float]]:
    return [embed(t) for t in texts]
