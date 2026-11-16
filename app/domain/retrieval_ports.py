"""Synchronous, provider-neutral research retrieval contracts.

Offsets are Python character offsets into the immutable version's original text;
``end`` is exclusive. Vector indexes return identifiers, never trusted evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    space_id: str
    document_id: str
    version_id: str
    title: str
    source: str
    content: str
    start: int
    end: int


@dataclass(frozen=True)
class VectorHit:
    chunk_id: str
    score: float


class Embedding(Protocol):
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class VectorIndex(Protocol):
    def synchronize(self, space_id: str, generation: str, chunks: list[Chunk],
                    embeddings: list[list[float]]) -> None: ...

    def search(self, space_id: str, generation: str, vector: list[float],
               limit: int) -> list[VectorHit]: ...


class Reranker(Protocol):
    """Return one finite score per document, in the original input order."""

    def rerank(self, query: str, documents: list[str]) -> list[float]: ...
