"""Research retrieval unit tests: fake synchronous ports, no network or database."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import FrozenInstanceError
import math
import threading

import pytest

from app.application.usecases.retrieval import ResearchRetrieval, bm25_scores, tokens
from app.domain.errors import DomainError
from app.domain.retrieval_ports import Chunk, VectorHit


def row(version="v1", content="battery evidence", title="Research", **extra):
    return {"version_id": version, "document_id": "doc-" + version,
            "title": title, "source": "local:paper", "content": content,
            "index_status": "ready", **extra}


class Embeddings:
    def __init__(self):
        self.calls = []
        self.fail = False

    def embed_batch(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("provider is offline")
        return [[1.0, 0.5] for _ in texts]


class Index:
    def __init__(self):
        self.syncs = []
        self.searches = []
        self.chunks = []
        self.hits = None
        self.fail_sync = False
        self.fail_search = False

    def synchronize(self, space_id, generation, chunks, embeddings):
        self.syncs.append((space_id, generation, chunks, embeddings))
        if self.fail_sync:
            raise RuntimeError("index unavailable")
        self.chunks = list(chunks)

    def search(self, space_id, generation, vector, limit):
        self.searches.append((space_id, generation, vector, limit))
        if self.fail_search:
            raise RuntimeError("search unavailable")
        if self.hits is not None:
            return self.hits(self.chunks)
        return [VectorHit(chunk.chunk_id, 1.0 / (rank + 1))
                for rank, chunk in enumerate(self.chunks)][:limit]


class Rerank:
    def __init__(self, scores=None):
        self.calls = []
        self.scores = scores

    def rerank(self, query, documents):
        self.calls.append((query, documents))
        if isinstance(self.scores, Exception):
            raise self.scores
        return self.scores if self.scores is not None else list(map(float, range(len(documents))))


def pipeline(**kwargs):
    embedding, index = Embeddings(), Index()
    return ResearchRetrieval(embedding, index, **kwargs), embedding, index


def test_ports_are_frozen_value_objects():
    chunk = Chunk("c", "s", "d", "v", "t", "src", "body", 0, 4)
    with pytest.raises(FrozenInstanceError):
        chunk.content = "changed"
    assert VectorHit("c", 0.5).score == 0.5


def test_tokens_and_bm25_are_bilingual_frequency_aware_and_title_weighted():
    assert tokens("BATTERY battery 锂电池") == ["battery", "battery", "锂", "电", "池", "锂电", "电池"]
    chunks = [Chunk("title", "s", "d1", "v1", "battery", "", "unrelated", 0, 9),
              Chunk("body", "s", "d2", "v2", "", "", "battery", 0, 7)]
    scores = bm25_scores("battery", chunks)
    assert scores["title"] > scores["body"] > 0
    assert bm25_scores("%%%", chunks) == {}


def test_local_search_keeps_literal_source_and_finds_late_evidence():
    content = "preface " * 1000 + " The mitochondria experiment found ATP generation. " + "ending " * 100
    rows = [row(content=content), row("v2", "锂电池容量随使用时间变化", title="Other study"),
            row("v3", "充电循环会降低容量。", title="锂电池寿命研究")]
    retrieval = ResearchRetrieval()
    hit = retrieval.search("s", "mitochondria", rows)[0]
    assert hit["content"] == content[hit["chunk_start"]:hit["chunk_end"]]
    assert "mitochondria" in hit["snippet"] and hit["snippet"] in content
    assert len(hit["content"]) <= 1600 and len(hit["snippet"]) <= 400
    assert hit["docid"] == hit["document_id"]
    assert hit["retrieval_strategy"] == "lexical" and not hit["retrieval_degraded"]
    assert retrieval.search("s", "如何研究锂电池的寿命和容量？", rows)[0]["version_id"] == "v3"
    assert retrieval.search("s", "%%%", rows) == []


def test_title_only_empty_body_is_still_searchable():
    result = ResearchRetrieval().search("s", "battery", [row(content="", title="battery findings")])
    assert len(result) == 1
    assert result[0]["content"] == result[0]["snippet"] == ""
    assert result[0]["chunk_start"] == result[0]["chunk_end"] == 0


def test_mutation_during_synchronize_aborts_before_vector_search_and_does_not_cache():
    retrieval, embedding, index = pipeline()
    rows = [row()]
    original_sync = index.synchronize

    def synchronize(*args):
        original_sync(*args)
        rows[0]["content"] = "replacement battery"

    index.synchronize = synchronize
    with pytest.raises(DomainError) as error:
        retrieval.search("s", "battery", deepcopy(rows), authorize=lambda: rows)
    assert error.value.status_code == 409
    assert not index.searches
    index.synchronize = original_sync
    retrieval.search("s", "battery", rows)
    assert len(embedding.calls[-1]) == 2


def test_query_embedding_dimension_mismatch_falls_back_then_rebuilds():
    retrieval, embedding, index = pipeline()
    retrieval.search("s", "battery", [row()])
    original_embed = embedding.embed_batch
    embedding.embed_batch = lambda texts: [[1.0] for _ in texts]
    result = retrieval.search("s", "battery", [row()])
    assert result[0]["degradation_reasons"] == ["embedding_failed"]
    assert len(index.searches) == 1
    embedding.embed_batch = original_embed
    retrieval.search("s", "battery", [row()])
    assert len(index.syncs) == 2


def test_chunks_cover_every_character_with_overlap_and_paragraph_boundaries():
    retrieval, _, index = pipeline(chunk_size=30, overlap=5)
    content = "  battery paragraph.\n\n" + "中文字和\r\n Unicode\t" * 20 + " tail  "
    hit = retrieval.search("s", "battery", [row(content=content)])[0]
    chunks = index.chunks
    assert chunks[0].start == 0 and chunks[-1].end == len(content)
    assert all(chunk.content == content[chunk.start:chunk.end] for chunk in chunks)
    assert all(right.start == left.end - 5 for left, right in zip(chunks, chunks[1:]))
    assert all(len(chunk.content) <= 30 for chunk in chunks)
    assert hit["content"] in content


def test_identical_snapshot_indexes_once_query_embedding_is_always_fresh():
    retrieval, embedding, index = pipeline()
    rows = [row(), row("v2", "other battery findings")]
    first = retrieval.search("s", "battery", rows)
    second = retrieval.search("s", "other", list(reversed(rows)))
    assert len(index.syncs) == 1
    assert len(embedding.calls[0]) == len(index.chunks) + 1
    assert embedding.calls[1] == ["other"]
    assert first[0]["generation"] == second[0]["generation"]
    assert first[0]["retrieval_strategy"] == "hybrid_rrf"
    assert rows[0]["content"] == "battery evidence"


def test_content_title_membership_config_and_space_change_generation():
    retrieval, embedding, index = pipeline()
    rows = [row()]
    retrieval.search("s", "battery", rows)
    first_ids = [chunk.chunk_id for chunk in index.chunks]
    first_generation = index.syncs[-1][1]
    rows[0]["content"] = "battery changed in place"
    retrieval.search("s", "battery", rows)
    assert first_ids != [chunk.chunk_id for chunk in index.chunks]
    assert first_generation != index.syncs[-1][1]
    rows[0]["title"] = "Updated title"
    retrieval.search("s", "battery", rows)
    retrieval.search("other-space", "battery", rows)
    assert len(index.syncs) == 4
    other, _, other_index = pipeline(chunk_size=50, overlap=5)
    other.search("s", "battery", rows)
    assert other_index.syncs[0][1] != index.syncs[2][1]
    # Separate instances cannot inherit a cache built with a different model.
    fresh = ResearchRetrieval(embedding, index)
    fresh.search("s", "battery", rows)
    assert len(index.syncs) == 5


def test_snapshot_withdrawal_reindexes_and_empty_snapshot_never_searches_old_index():
    retrieval, _, index = pipeline()
    retrieval.search("s", "battery", [row(), row("v2")])
    result = retrieval.search("s", "battery", [row("v2")])
    assert {item["version_id"] for item in result} == {"v2"}
    assert len(index.syncs) == 2
    assert retrieval.search("s", "battery", []) == []
    assert len(index.searches) == 2


@pytest.mark.parametrize("stage", ["embedding", "index_sync", "vector_search"])
def test_vector_failure_falls_back_explicitly_and_does_not_cache_failures(stage):
    retrieval, embedding, index = pipeline()
    if stage == "embedding":
        embedding.fail = True
    elif stage == "index_sync":
        index.fail_sync = True
    else:
        index.fail_search = True
    hit = retrieval.search("s", "battery", [row()])[0]
    assert hit["retrieval_strategy"] == "lexical"
    assert hit["retrieval_degraded"] and hit["degradation_reasons"] == [stage + "_failed"]
    embedding.fail = index.fail_sync = index.fail_search = False
    retrieval.search("s", "battery", [row()])
    assert len(embedding.calls[-1]) == 2
    assert len(index.syncs) == (1 if stage == "embedding" else 2)


@pytest.mark.parametrize("bad", [[], [[float("nan")], [1.0]], [[True], [1.0]], [[1.0], [1.0, 2.0]]])
def test_invalid_embeddings_never_reach_index(bad):
    retrieval, embedding, index = pipeline()
    embedding.embed_batch = lambda texts: bad
    result = retrieval.search("s", "battery", [row()])
    assert result[0]["degradation_reasons"] == ["embedding_failed"]
    assert not index.syncs and not index.searches


def test_untrusted_vector_ids_and_scores_are_ignored_and_chunk_rrf_is_deduplicated():
    retrieval, _, index = pipeline()
    index.hits = lambda chunks: [VectorHit("foreign-chunk", 1000),
                                 VectorHit(chunks[0].chunk_id, float("nan")),
                                 VectorHit(chunks[0].chunk_id, 1), VectorHit(chunks[0].chunk_id, 2)]
    hit = retrieval.search("s", "battery", [row()])[0]
    assert math.isclose(hit["score"], 2 / 61)
    assert hit["retrieval_strategy"] == "hybrid_rrf"
    assert "foreign" not in hit["content"]


def test_vector_only_semantics_and_final_version_dedup():
    retrieval, _, index = pipeline(chunk_size=20, overlap=3, vector_candidates=1, lexical_candidates=1)
    rows = [row(content="battery " * 15), row("v2", "different study")]
    # Request minimum candidate budget grows with limit, not configured 1.
    index.hits = lambda chunks: [VectorHit(chunk.chunk_id, 1 / (rank + 1)) for rank, chunk in enumerate(chunks)]
    result = retrieval.search("s", "unknown semantic synonym", rows, limit=10)
    assert len(result) == 2
    assert len({item["version_id"] for item in result}) == 2
    assert all(item["retrieval_strategy"] == "vector" for item in result)
    assert index.searches[-1][3] == 10


def test_reranker_receives_only_authorized_candidate_bodies_and_uses_original_position():
    rerank = Rerank()
    retrieval, _, index = pipeline(reranker=rerank)
    rows = [row("v1", "battery one"), row("v2", "battery two")]
    index.hits = lambda chunks: [VectorHit("not-authorized", 1)] + [VectorHit(chunk.chunk_id, 1) for chunk in chunks]
    result = retrieval.search("s", "battery", rows)
    sent = rerank.calls[0][1]
    assert set(sent) == {"battery one", "battery two"}
    assert result[0]["content"] == sent[-1]
    assert all(item["rerank_applied"] for item in result)


@pytest.mark.parametrize("bad", [[], [float("inf")], [True], RuntimeError("rerank down")])
def test_invalid_or_failing_rerank_keeps_rrf_and_records_fallback(bad):
    retrieval, _, _ = pipeline(reranker=Rerank(bad))
    hit = retrieval.search("s", "battery", [row()])[0]
    assert not hit["rerank_applied"]
    assert hit["degradation_reasons"] == ["rerank_failed"]
    assert math.isclose(hit["score"], 2 / 61)


@pytest.mark.parametrize("change_at", [1, 2, 3, 4, 5, 6])
def test_authorization_is_checked_before_each_external_step_and_output(change_at):
    rerank = Rerank()
    retrieval, embedding, index = pipeline(reranker=rerank)
    rows = [row()]
    calls = 0

    def authorize():
        nonlocal calls
        calls += 1
        if calls == change_at:
            raise DomainError(403, "permission revoked")
        return deepcopy(rows)

    with pytest.raises(DomainError) as error:
        retrieval.search("s", "battery", rows, authorize=authorize)
    assert error.value.status_code == 403
    assert calls == change_at
    if change_at <= 2:
        assert not embedding.calls
    if change_at <= 3:
        assert not index.syncs
    if change_at <= 5:
        assert not rerank.calls


def test_non_domain_authorization_errors_are_not_provider_fallbacks():
    retrieval, _, index = pipeline()
    calls = 0

    def authorize():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("authorization storage is unavailable")
        return [row()]

    with pytest.raises(RuntimeError, match="authorization storage"):
        retrieval.search("s", "battery", [row()], authorize=authorize)
    assert not index.syncs


def test_authorize_changed_rows_space_or_ready_state_abort_without_disclosure():
    for changed in ([row(content="withdrawn replacement")], [], [row(space_id="foreign")],
                    [row(index_status="pending")]):
        retrieval, embedding, _ = pipeline()
        with pytest.raises(DomainError) as error:
            retrieval.search("s", "battery", [row()], authorize=lambda: changed)
        assert error.value.status_code == 409
        assert not embedding.calls


def test_after_rerank_reauthorization_prevents_post_call_revocation_leak():
    rows = [row()]

    class RevokingRerank:
        def rerank(self, query, documents):
            rows.clear()
            return [1.0] * len(documents)

    retrieval = ResearchRetrieval(reranker=RevokingRerank())
    with pytest.raises(DomainError) as error:
        retrieval.search("s", "battery", deepcopy(rows), authorize=lambda: rows)
    assert error.value.status_code == 409


def test_same_space_concurrency_initializes_index_once():
    retrieval, embedding, index = pipeline()
    barrier = threading.Barrier(4)

    def search(_):
        barrier.wait()
        return retrieval.search("s", "battery", [row()])

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(search, range(4)))
    assert all(results)
    assert len(index.syncs) == 1
    assert sorted(map(len, embedding.calls)) == [1, 1, 1, 2]


def test_limits_and_chunk_budget_fail_explicitly_without_truncating_documents():
    for limit in [0, 51, True, 1.5]:
        with pytest.raises(DomainError) as error:
            ResearchRetrieval().search("s", "battery", [row()], limit=limit)
        assert error.value.status_code == 422
    retrieval, embedding, _ = pipeline(chunk_size=10, overlap=2, max_chunks=2)
    with pytest.raises(DomainError, match="检索容量上限"):
        retrieval.search("s", "battery", [row(content="battery " * 30)])
    assert not embedding.calls
    retrieval, _, index = pipeline(chunk_size=5000, overlap=100)
    result = retrieval.search("s", "battery", [row(content="battery " * 500)])
    assert all(len(chunk.content) <= 1600 for chunk in index.chunks)
    assert index.chunks[-1].end == 4000
    assert all(len(item["content"]) <= 1600 for item in result)
