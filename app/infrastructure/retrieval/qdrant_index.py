"""Lazy synchronous Qdrant index, isolated by space and immutable generation.

Collections and point IDs are derived here, never accepted from API clients. A
point UUID includes generation as well as chunk_id so that concurrent readers
of an older generation retain their snapshot. Synchronization never deletes old
generations. Retention/garbage collection is deliberately not implemented yet.
There is no persistent manifest: the pipeline may resynchronize after restart;
upsert is idempotent and filtered count verification precedes successful return.
"""
from __future__ import annotations

import hashlib
import math
from threading import RLock
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

from .embedding import RetrievalAdapterError, finite_vector, check_run
from app.domain.errors import DomainError
from .settings import RetrievalSettings

if TYPE_CHECKING:
    from app.domain.retrieval_ports import Chunk, VectorHit


class QdrantIndex:
    def __init__(self, settings: RetrievalSettings, *, client: Any = None) -> None:
        if settings.qdrant_url and not settings.allow_external:
            raise ValueError("External research retrieval requires explicit consent")
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._lock = RLock()
        self._closed = False

    @staticmethod
    def _collection_name(space_id: str) -> str:
        if not isinstance(space_id, str) or not space_id:
            raise ValueError("Invalid retrieval space")
        return "research_" + hashlib.sha256(space_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _point_id(space_id: str, generation: str, chunk_id: str) -> str:
        # Length-delimited input prevents separator ambiguities in arbitrary IDs.
        value = "".join(f"{len(part)}:{part}" for part in (space_id, generation, chunk_id))
        return str(uuid5(NAMESPACE_URL, "research-retrieval:" + value))

    def _get_client(self):
        if self._closed:
            raise RetrievalAdapterError("Research vector index is closed")
        if self._client is None:
            from qdrant_client import QdrantClient

            if self._settings.qdrant_url:
                self._client = QdrantClient(url=self._settings.qdrant_url, timeout=self._settings.timeout_seconds)
            else:
                self._client = QdrantClient(path=str(self._settings.qdrant_path), force_disable_check_same_thread=True)
        return self._client

    def _filter(self, space_id: str, generation: str):
        from qdrant_client import models

        if not isinstance(generation, str) or not generation:
            raise ValueError("Invalid retrieval generation")
        return models.Filter(must=[
            models.FieldCondition(key="space_id", match=models.MatchValue(value=space_id)),
            models.FieldCondition(key="generation", match=models.MatchValue(value=generation)),
        ])

    def _ensure_collection(self, client, collection: str) -> None:
        from qdrant_client import models

        check_run()
        if not client.collection_exists(collection_name=collection):
            check_run()
            client.create_collection(collection_name=collection, vectors_config=models.VectorParams(size=self._settings.embedding_dim, distance=models.Distance.COSINE))
        check_run()
        params = client.get_collection(collection_name=collection).config.params.vectors
        if not isinstance(params, models.VectorParams) or params.size != self._settings.embedding_dim or params.distance != models.Distance.COSINE:
            # Never recreate/delete an existing collection on model/schema mismatch.
            raise RetrievalAdapterError("Research vector index dimension or schema mismatch")

    def synchronize(self, space_id: str, generation: str, chunks: list[Chunk], embeddings: list[list[float]]) -> None:
        from qdrant_client import models

        collection = self._collection_name(space_id)
        query_filter = self._filter(space_id, generation)
        if len(chunks) != len(embeddings):
            raise RetrievalAdapterError("Chunk and embedding counts differ")
        points = []
        seen: set[str] = set()
        for chunk, embedding in zip(chunks, embeddings):
            if chunk.space_id != space_id or not isinstance(chunk.chunk_id, str) or not chunk.chunk_id or chunk.chunk_id in seen or not isinstance(chunk.version_id, str) or not chunk.version_id:
                raise RetrievalAdapterError("Invalid chunk identity")
            seen.add(chunk.chunk_id)
            points.append(models.PointStruct(
                id=self._point_id(space_id, generation, chunk.chunk_id),
                vector=finite_vector(embedding, self._settings.embedding_dim),
                payload={"chunk_id": chunk.chunk_id, "generation": generation, "space_id": space_id, "version_id": chunk.version_id},
            ))
        with self._lock:
            try:
                client = self._get_client()
                self._ensure_collection(client, collection)
                for start in range(0, len(points), 64):
                    check_run()
                    client.upsert(collection_name=collection, points=points[start:start + 64], wait=True)
                check_run()
                count = client.count(collection_name=collection, count_filter=query_filter, exact=True).count
                if count != len(points):
                    raise RetrievalAdapterError("Research vector synchronization count mismatch")
            except (RetrievalAdapterError, DomainError):
                raise
            except Exception:
                raise RetrievalAdapterError("Research vector synchronization failed") from None

    def search(self, space_id: str, generation: str, vector: list[float], limit: int) -> list[VectorHit]:
        from app.domain.retrieval_ports import VectorHit

        collection = self._collection_name(space_id)
        query_filter = self._filter(space_id, generation)
        vector = finite_vector(vector, self._settings.embedding_dim)
        if type(limit) is not int or not 1 <= limit <= self._settings.max_candidates:
            raise ValueError("Invalid vector search limit")
        with self._lock:
            try:
                client = self._get_client()
                check_run()
                if not client.collection_exists(collection_name=collection):
                    return []
                self._ensure_collection(client, collection)
                # Filter is part of recall, not post-filtering of a global top-k.
                check_run()
                points = client.query_points(collection_name=collection, query=vector, query_filter=query_filter, limit=limit, with_payload=True, with_vectors=False).points
                hits = []
                seen: set[str] = set()
                for point in points:
                    payload = point.payload or {}
                    chunk_id = payload.get("chunk_id")
                    score = point.score
                    if payload.get("space_id") != space_id or payload.get("generation") != generation or not isinstance(chunk_id, str) or not chunk_id or chunk_id in seen or isinstance(score, bool) or not isinstance(score, (float, int)) or not math.isfinite(score):
                        raise RetrievalAdapterError("Invalid research vector result")
                    seen.add(chunk_id)
                    hits.append(VectorHit(chunk_id=chunk_id, score=float(score)))
                return hits
            except (RetrievalAdapterError, DomainError):
                raise
            except Exception:
                raise RetrievalAdapterError("Research vector search failed") from None

    def close(self) -> None:
        with self._lock:
            if not self._closed and self._client is not None and self._owns_client:
                self._client.close()
            self._closed = True
