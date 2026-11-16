"""Snapshot-scoped, synchronous research retrieval without persistence dependencies."""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Callable

from app.domain.errors import DomainError
from app.domain.ai_ports import synchronous_run_context
from app.domain.retrieval_ports import Chunk, Embedding, Reranker, VectorIndex


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def tokens(text: str) -> list[str]:
    """Case-folded Latin terms and CJK character/bigram terms, retaining frequency."""
    folded = text.casefold()
    result = re.findall(r"[a-z0-9]+(?:[_'-][a-z0-9]+)*", folded)
    for run in re.findall(r"[㐀-鿿]+", folded):
        result.extend(run)
        result.extend(run[index:index + 2] for index in range(len(run) - 1))
    return result


def bm25_scores(query: str, chunks: list[Chunk], *, title_weight: float = 4.0,
                k1: float = 1.5, b: float = 0.75) -> dict[str, float]:
    """Positive-IDF BM25 with weighted title term frequencies and document lengths."""
    terms = set(tokens(query))
    if not terms or not chunks:
        return {}
    frequencies = []
    lengths = []
    document_frequency = Counter()
    for chunk in chunks:
        body, title = Counter(tokens(chunk.content)), Counter(tokens(chunk.title))
        frequency = {term: body[term] + title_weight * title[term] for term in terms}
        frequencies.append(frequency)
        lengths.append(sum(body.values()) + title_weight * sum(title.values()))
        document_frequency.update(term for term, count in frequency.items() if count)
    average = sum(lengths) / len(chunks) or 1.0
    scores = {}
    for chunk, frequency, length in zip(chunks, frequencies, lengths):
        score = 0.0
        for term in sorted(terms):
            count = frequency[term]
            if count:
                idf = math.log1p((len(chunks) - document_frequency[term] + 0.5)
                                 / (document_frequency[term] + 0.5))
                score += idf * count * (k1 + 1) / (count + k1 * (1 - b + b * length / average))
        if score > 0:
            scores[chunk.chunk_id] = score
    return scores


def _snapshot(space_id: str, rows: list[dict]) -> tuple[list[dict], str]:
    if not isinstance(rows, list):
        raise DomainError(409, "检索授权返回的数据不是当前快照，已中止检索")
    snapshot = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise DomainError(422, "检索快照数据不正确")
        if row.get("space_id", space_id) != space_id:
            raise DomainError(409, "知识库已变更，检索已中止")
        if row.get("index_status", "ready") != "ready":
            raise DomainError(409, "检索快照尚未就绪，请稍后重试")
        if not isinstance(row.get("version_id"), str) or not row["version_id"]:
            raise DomainError(422, "检索结果缺少版本标识，已中止")
        if row["version_id"] in seen:
            raise DomainError(422, "检索结果包含重复版本，已中止")
        seen.add(row["version_id"])
        if any(not isinstance(row.get(key, ""), str) for key in ("title", "source", "content")):
            raise DomainError(422, "检索文本不正确")
        if not isinstance(row.get("document_id", row.get("docid", row["version_id"])), str):
            raise DomainError(422, "检索文档标识不正确")
        # A JSON roundtrip detaches nested mutable row metadata as well as text.
        try:
            snapshot.append(json.loads(json.dumps(row, ensure_ascii=False, allow_nan=False)))
        except (TypeError, ValueError) as exc:
            raise DomainError(422, "检索快照元数据不正确") from exc
    snapshot.sort(key=lambda row: row["version_id"])
    return snapshot, _digest(snapshot)


def _chunks(space_id: str, rows: list[dict], chunk_size: int, overlap: int,
            max_chunks: int) -> list[Chunk]:
    chunks = []
    for row in rows:
        content = row.get("content", "")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        start = 0
        while start < len(content) or (start == 0 and not content):
            end = min(start + chunk_size, len(content))
            if end < len(content):
                # Prefer paragraph boundaries without ever normalizing the source.
                boundary = content.rfind("\n\n", start + max(overlap + 1, chunk_size // 2), end)
                if boundary != -1:
                    end = boundary + 2
            chunk_id = _digest([space_id, row["version_id"], content_hash, start, end])
            chunks.append(Chunk(chunk_id, space_id,
                                row.get("document_id", row.get("docid", row["version_id"])),
                                row["version_id"], row.get("title", ""), row.get("source", ""),
                                content[start:end], start, end))
            if len(chunks) > max_chunks:
                raise DomainError(422, "知识库内容超过检索容量上限，请拆分知识库")
            if end == len(content):
                break
            start = end - overlap
    return chunks


def _vectors(values, expected: int, dimension: int | None = None) -> tuple[list[list[float]], int]:
    if not isinstance(values, (list, tuple)) or len(values) != expected:
        raise ValueError("Embedding count mismatch")
    result = []
    for value in values:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("Empty embedding")
        if any(isinstance(number, bool) or not isinstance(number, (int, float))
               or not math.isfinite(number) for number in value):
            raise ValueError("Embedding must contain finite numbers")
        dimension = dimension or len(value)
        if len(value) != dimension:
            raise ValueError("Embedding dimension mismatch")
        result.append([float(number) for number in value])
    return result, dimension or 0


@dataclass(frozen=True)
class _Indexed:
    generation: str
    dimension: int


class ResearchRetrieval:
    """A reusable pipeline; all adapters and authorization callbacks are synchronous.

    The caller supplies already-authorized ready rows and releases its unit of work
    before invoking this pipeline. ``authorize`` opens a fresh short-lived unit of
    work, checks the permission epoch, and returns the current rows. Authorization
    exceptions are never converted to provider fallbacks.
    """

    def __init__(self, embedding: Embedding | None = None, index: VectorIndex | None = None,
                 reranker: Reranker | None = None, *, chunk_size: int = 1000,
                 overlap: int = 150, vector_candidates: int = 32,
                 lexical_candidates: int = 32, max_chunks: int = 10000):
        for name, value in (("chunk_size", chunk_size), ("vector_candidates", vector_candidates),
                            ("lexical_candidates", lexical_candidates), ("max_chunks", max_chunks)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        # Evidence is capped at 1600 characters, but every character is still indexed.
        effective_size = min(chunk_size, 1600)
        if isinstance(overlap, bool) or not isinstance(overlap, int) or not 0 <= overlap < effective_size:
            raise ValueError("overlap must be nonnegative and smaller than the effective chunk size")
        self.embedding, self.index, self.reranker = embedding, index, reranker
        self.chunk_size, self.overlap = effective_size, overlap
        self.vector_candidates, self.lexical_candidates = vector_candidates, lexical_candidates
        self.max_chunks = max_chunks
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._indexed: dict[str, _Indexed] = {}

    def _lock(self, space_id: str):
        with self._locks_guard:
            return self._locks.setdefault(space_id, threading.Lock())

    def search(self, space_id: str, query: str, rows: list[dict], limit: int = 5,
               authorize: Callable[[], list[dict]] | None = None) -> list[dict]:
        if not isinstance(space_id, str) or not space_id:
            raise DomainError(422, "知识库标识不正确")
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise DomainError(422, "检索问题不能为空")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise DomainError(422, "检索条数需在 1 到 50 之间")
        snapshot, fingerprint = _snapshot(space_id, rows)
        context = synchronous_run_context.get()

        def emit(kind, **data):
            if context is not None:
                context.check()
                context.emit({"type": kind, **data})

        def check():
            if context is not None:
                context.check()
            if authorize is not None:
                _, current = _snapshot(space_id, authorize())
                if current != fingerprint:
                    raise DomainError(409, "知识库索引已更新，请重新搜索")

        check()
        emit("retrieval.started")
        generation = _digest([space_id, fingerprint, self.chunk_size, self.overlap, "research-chunks-v1"])
        chunks = _chunks(space_id, snapshot, self.chunk_size, self.overlap, self.max_chunks)
        ledger = {chunk.chunk_id: chunk for chunk in chunks}
        lexical_scores = bm25_scores(query, chunks)
        lexical = sorted(lexical_scores, key=lambda key: (-lexical_scores[key], key))[:max(limit, self.lexical_candidates)]
        vector = []
        degraded = []
        vector_succeeded = False
        failed = object()

        def external(stage, operation):
            # Deliberately outside the provider try: even non-domain authorization
            # errors must propagate rather than becoming a lexical fallback.
            check()
            try:
                result = operation()
            except DomainError:
                raise
            except Exception:
                degraded.append(stage + "_failed")
                check()
                emit("retrieval.degraded", reason=stage + "_failed")
                return failed
            check()
            return result

        if chunks and self.embedding is not None and self.index is not None:
            lock = self._lock(space_id)
            while not lock.acquire(timeout=.1):
                check()
            try:
                check()
                cached = self._indexed.get(space_id)
                needs_sync = cached is None or cached.generation != generation
                if needs_sync:
                    self._indexed.pop(space_id, None)
                batch = [chunk.content for chunk in chunks] + [query] if needs_sync else [query]
                # Keep each adapter call at one provider batch so revocation is
                # rechecked between outgoing requests, not only before a long sync.
                encoded_vectors = []
                encoded_dimension = None if needs_sync else cached.dimension
                encoded = failed
                batch_size = getattr(self.embedding, "max_batch_size", 10)
                if type(batch_size) is not int or not 1 <= batch_size <= 10:
                    batch_size = 10
                for start in range(0, len(batch), batch_size):
                    part = batch[start:start + batch_size]
                    part_result = external("embedding", lambda part=part: _vectors(
                        self.embedding.embed_batch(part), len(part), encoded_dimension))
                    if part_result is failed:
                        break
                    values, encoded_dimension = part_result
                    encoded_vectors.extend(values)
                else:
                    encoded = (encoded_vectors, encoded_dimension)
                synchronized = failed
                if encoded is not failed:
                    vectors, dimension = encoded
                    if needs_sync:
                        synchronized = external("index_sync", lambda: self.index.synchronize(
                            space_id, generation, chunks, vectors[:-1]))
                        if synchronized is not failed:
                            check()
                            self._indexed[space_id] = _Indexed(generation, dimension)
                    else:
                        synchronized = None
                if synchronized is not failed:
                    def search_vectors():
                        hits = self.index.search(space_id, generation, vectors[-1], max(limit, self.vector_candidates))
                        vector_scores = {}
                        for hit in hits:
                            # The index is never an authorization/evidence source.
                            if (hit.chunk_id not in ledger or isinstance(hit.score, bool)
                                    or not isinstance(hit.score, (int, float)) or not math.isfinite(hit.score)):
                                continue
                            vector_scores[hit.chunk_id] = max(hit.score, vector_scores.get(hit.chunk_id, -math.inf))
                        return sorted(vector_scores, key=lambda key: (-vector_scores[key], key))[:max(limit, self.vector_candidates)]

                    found = external("vector_search", search_vectors)
                    if found is not failed:
                        vector = found
                        vector_succeeded = True
                if degraded:
                    self._indexed.pop(space_id, None)
            finally:
                lock.release()
        elif (self.embedding is None) != (self.index is None):
            degraded.append("vector_unavailable")
            emit("retrieval.degraded", reason="vector_unavailable")
        elif not chunks:
            emit("retrieval.skipped", reason="no_candidates")
        else:
            emit("retrieval.skipped", reason="not_configured", strategy="vector")

        fused = {}
        for ranking in (lexical, vector):
            for rank, chunk_id in enumerate(ranking, 1):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (60 + rank)
        ranked = sorted(fused, key=lambda key: (-fused[key], key))
        scores = fused
        rerank_applied = False
        if ranked and self.reranker is not None:
            check()
            emit("rerank.started", candidate_count=len(ranked))
            try:
                reranked = self.reranker.rerank(query, [ledger[key].content for key in ranked])
                if (not isinstance(reranked, (list, tuple)) or len(reranked) != len(ranked)
                        or any(isinstance(score, bool) or not isinstance(score, (int, float))
                               or not math.isfinite(score) for score in reranked)):
                    raise ValueError("Reranker must return one finite score per candidate")
                scores = dict(zip(ranked, reranked))
                ranked = sorted(ranked, key=lambda key: (-scores[key], -fused[key], key))
                rerank_applied = True
            except DomainError:
                raise
            except Exception:
                degraded.append("rerank_failed")
                check()
                emit("rerank.degraded", reason="rerank_failed")
            else:
                check()
                emit("rerank.completed", candidate_count=len(ranked))
        else:
            emit("rerank.skipped", reason="no_candidates" if not ranked else "not_configured")

        strategy = "hybrid_rrf" if lexical and vector else "vector" if vector else "lexical"
        if vector_succeeded and not lexical and not vector:
            strategy = "vector"
        result = []
        versions = set()
        for key in ranked:
            chunk = ledger[key]
            if chunk.version_id in versions:
                continue
            versions.add(chunk.version_id)
            offsets = [match.start() for term in set(tokens(query))
                       if (match := re.search(re.escape(term), chunk.content, flags=re.IGNORECASE))]
            snippet_start = max(0, min(offsets, default=0) - 80)
            result.append({"version_id": chunk.version_id, "document_id": chunk.document_id,
                           "docid": chunk.document_id, "content": chunk.content, "source": chunk.source,
                           "title": chunk.title, "space_id": space_id, "score": float(scores[key]),
                           "snippet": chunk.content[snippet_start:snippet_start + 400], "chunk_id": key,
                           "chunk_start": chunk.start, "chunk_end": chunk.end,
                           "generation": generation, "retrieval_strategy": strategy,
                           "rerank_applied": rerank_applied, "retrieval_degraded": bool(degraded),
                           "degradation_reasons": list(degraded)})
            if len(result) == limit:
                break
        check()
        emit("retrieval.completed", hit_count=len(result), strategy=strategy,
             rerank_applied=rerank_applied, degraded=bool(degraded), reasons=list(degraded))
        return result
