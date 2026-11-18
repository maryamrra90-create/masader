"""Sentence embeddings (384 dimensions, L2-normalised) behind one interface (architecture ADR-08).

Two implementations:
  * FastEmbedEmbedder: paraphrase-multilingual-MiniLM-L12-v2 through fastembed/ONNX, as designed. It needs
    the model files from Hugging Face, which were not reachable from the build environment, so it is an
    optional dependency and is NOT exercised by the test suite.
  * HashingEmbedder (default, EMBEDDER=hashing): a deterministic feature-hashing embedding of word
    unigrams, bigrams and character trigrams. It needs no download and is good at the job dedup gives it
    (spotting copies and light rewrites of the same text), but it has no cross-lingual meaning: an Arabic
    and an English version of one report are NOT close under it. This is a documented substitution.
"""
from __future__ import annotations

import functools
import hashlib
import math
from typing import Protocol

import numpy as np

from ..settings import settings
from .text import words

DIM = 384


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> np.ndarray: ...


class HashingEmbedder:
    name = "hashing-ngram-v1"

    @staticmethod
    def _features(text: str) -> list[tuple[str, float]]:
        w = words(text)
        feats = [(f"w:{t}", 1.0) for t in w]
        feats += [(f"b:{a}_{b}", 1.0) for a, b in zip(w, w[1:], strict=False)]
        for t in w:
            padded = f"#{t}#"
            feats += [(f"c:{padded[i:i + 3]}", 0.3) for i in range(max(1, len(padded) - 2))]
        return feats

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            for feat, weight in self._features(text):
                h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
                out[row, h % DIM] += weight if (h >> 32) & 1 else -weight
            norm = math.sqrt(float(out[row] @ out[row]))
            if norm:
                out[row] /= norm
        return out


class FastEmbedEmbedder:  # pragma: no cover - needs the model download (not available in the build env)
    name = "paraphrase-multilingual-MiniLM-L12-v2-onnx"

    def __init__(self):
        from fastembed import TextEmbedding  # noqa: PLC0415 - optional dependency

        self.model = TextEmbedding("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")

    def embed(self, texts: list[str]) -> np.ndarray:
        vecs = np.array(list(self.model.embed(texts, batch_size=32)), dtype=np.float32)
        return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


@functools.lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    if settings.embedder == "fastembed":
        return FastEmbedEmbedder()
    return HashingEmbedder()


def to_pgvector(vec: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
