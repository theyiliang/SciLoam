"""Offline adapter tests: MockTransport and in-memory Qdrant only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
import math
from types import SimpleNamespace

import httpx
import pytest
from qdrant_client import QdrantClient, models

from app.domain.retrieval_ports import Chunk
from app.infrastructure.retrieval.embedding import HTTPEmbedding, RetrievalAdapterError
from app.infrastructure.retrieval.factory import build_retrieval
from app.infrastructure.retrieval.qdrant_index import QdrantIndex
from app.infrastructure.retrieval.reranker import HTTPReranker
from app.infrastructure.retrieval.settings import ResearchRetrievalSettings, load_retrieval_settings


def settings(**changes):
    return replace(ResearchRetrievalSettings(mode="hybrid", allow_external=True, embedding_base_url="https://embedding.test/v1", embedding_dim=2, reranker_base_url="https://rerank.test/v1", reranker_model="reranker"), **changes)


def chunk(chunk_id="chunk-a", space="space-a", content="PRIVATE CONTENT"):
    return Chunk(chunk_id, space, "doc", "version", "PRIVATE TITLE", "PRIVATE SOURCE", content, 0, len(content))


def test_settings_are_safe_by_default_and_exact_consent(tmp_path):
    config = load_retrieval_settings({"RESEARCH_DATA_DIR": str(tmp_path / "uncreated")})
    assert config.mode == "local" and config.allow_external is False
    assert config.qdrant_path == tmp_path / "uncreated" / "qdrant-research"
    assert build_retrieval(config) == (None, None, None)
    assert not config.data_dir.exists()
    for value in ("true", "yes", "01", "0", ""):
        assert load_retrieval_settings({"RESEARCH_RAG_ALLOW_EXTERNAL": value}).allow_external is False
    with pytest.raises(ValueError, match="RESEARCH_RAG_ALLOW_EXTERNAL"):
        build_retrieval(replace(config, mode="hybrid"))
    with pytest.raises(ValueError, match="consent"):
        HTTPEmbedding(replace(config, embedding_base_url="https://embedding.test"))
    with pytest.raises(ValueError, match="consent"):
        HTTPReranker(replace(config, reranker_base_url="https://rerank.test", reranker_model="model"))


@pytest.mark.parametrize("changes", [
    {"mode": "invalid"}, {"embedding_dim": True}, {"embedding_dim": 0},
    {"embedding_max_batch": 0}, {"embedding_max_batch": 11},
    {"timeout_seconds": math.inf}, {"timeout_seconds": 0}, {"timeout_seconds": True},
    {"max_candidates": 33}, {"embedding_base_url": "file:///private"},
    {"qdrant_url": "https://user:secret@example.com"},
])
def test_invalid_settings(changes):
    with pytest.raises(ValueError):
        settings(**changes)


def test_embedding_batches_orders_and_omits_dimensions():
    batches = []

    def handler(request):
        assert request.url.path == "/v1/embeddings"
        assert request.headers["authorization"] == "Bearer secret"
        assert request.extensions["timeout"]["read"] == 15.0
        body = json.loads(request.content)
        assert "dimensions" not in body
        batches.append(body["input"])
        return httpx.Response(200, json={"data": [{"index": index, "embedding": [index + 0.5, 1]} for index in reversed(range(len(body["input"])))]})

    adapter = HTTPEmbedding(settings(embedding_api_key="secret"), transport=httpx.MockTransport(handler))
    try:
        assert adapter.embed_batch([]) == []
        output = adapter.embed_batch([f"text-{index}" for index in range(23)])
        assert [len(batch) for batch in batches] == [10, 10, 3]
        assert len(output) == 23 and output[0] == [0.5, 1.0] and output[9] == [9.5, 1.0]
    finally:
        adapter.close()


@pytest.mark.parametrize("data", [
    [], [{"index": 0, "embedding": [1, 2]}],
    [{"index": 0, "embedding": [1, 2]}, {"index": 0, "embedding": [3, 4]}],
    [{"index": True, "embedding": [1, 2]}, {"index": 1, "embedding": [3, 4]}],
    [{"index": -1, "embedding": [1, 2]}, {"index": 1, "embedding": [3, 4]}],
    [{"index": 2, "embedding": [1, 2]}, {"index": 1, "embedding": [3, 4]}],
    [{"index": 0, "embedding": [True, 2]}, {"index": 1, "embedding": [3, 4]}],
    [{"index": 0, "embedding": ["1", 2]}, {"index": 1, "embedding": [3, 4]}],
    [{"index": 0, "embedding": [1]}, {"index": 1, "embedding": [3, 4]}],
    ["PRIVATE CONTENT", {"index": 1, "embedding": [3, 4]}],
])
def test_embedding_rejects_malformed_results(data):
    adapter = HTTPEmbedding(settings(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": data})))
    try:
        with pytest.raises(RetrievalAdapterError) as error:
            adapter.embed_batch(["PRIVATE CONTENT", "second"])
        assert "PRIVATE CONTENT" not in str(error.value)
    finally:
        adapter.close()


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_embedding_rejects_nonfinite_json_numbers(value):
    response = '{"data":[{"index":0,"embedding":[' + value + ',1]}]}'
    adapter = HTTPEmbedding(settings(), transport=httpx.MockTransport(lambda _: httpx.Response(200, content=response)))
    try:
        with pytest.raises(RetrievalAdapterError, match="value"):
            adapter.embed_batch(["private"])
    finally:
        adapter.close()


@pytest.mark.parametrize("status,content", [(200, ""), (200, "PRIVATE CONTENT"), (503, "PRIVATE CONTENT")])
def test_embedding_errors_do_not_expose_remote_body(status, content):
    adapter = HTTPEmbedding(settings(), transport=httpx.MockTransport(lambda _: httpx.Response(status, text=content)))
    try:
        with pytest.raises(RetrievalAdapterError) as error:
            adapter.embed_batch(["PRIVATE CONTENT"])
        assert "PRIVATE" not in str(error.value)
    finally:
        adapter.close()


def test_reranker_returns_original_order_and_optional_auth():
    def handler(request):
        assert request.url.path == "/v1/rerank"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert body["documents"] == ["a", "b"] and body["top_n"] == 2
        assert body["return_documents"] is False
        return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.8}, {"index": 0, "relevance_score": 0.2}]})

    adapter = HTTPReranker(settings(), transport=httpx.MockTransport(handler))
    try:
        assert adapter.rerank("query", []) == []
        assert adapter.rerank("query", ["a", "b"]) == [0.2, 0.8]
        with pytest.raises(ValueError):
            adapter.rerank("query", ["a"] * 33)
    finally:
        adapter.close()


@pytest.mark.parametrize("results", [
    [], [{"index": 0, "relevance_score": 1}],
    [{"index": 0, "relevance_score": 1}, {"index": 0, "relevance_score": 0}],
    [{"index": True, "relevance_score": 1}, {"index": 1, "relevance_score": 0}],
    [{"index": 0, "relevance_score": True}, {"index": 1, "relevance_score": 0}],
    [{"index": 0, "relevance_score": "PRIVATE CONTENT"}, {"index": 1, "relevance_score": 0}],
    [{"index": 4, "relevance_score": 1}, {"index": 1, "relevance_score": 0}],
])
def test_reranker_rejects_incomplete_or_invalid_results(results):
    adapter = HTTPReranker(settings(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": results})))
    try:
        with pytest.raises(RetrievalAdapterError) as error:
            adapter.rerank("private", ["PRIVATE CONTENT", "b"])
        assert "PRIVATE CONTENT" not in str(error.value)
    finally:
        adapter.close()


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_reranker_rejects_nonfinite_scores(value):
    adapter = HTTPReranker(settings(), transport=httpx.MockTransport(lambda _: httpx.Response(200, content='{"results":[{"index":0,"relevance_score":' + value + '}]}')))
    try:
        with pytest.raises(RetrievalAdapterError):
            adapter.rerank("private", ["private"])
    finally:
        adapter.close()


def test_reranker_auth_and_error_redaction():
    def handler(request):
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(503, text="PRIVATE CONTENT secret")

    adapter = HTTPReranker(settings(reranker_api_key="secret"), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RetrievalAdapterError) as error:
            adapter.rerank("private", ["PRIVATE CONTENT"])
        assert "PRIVATE" not in str(error.value) and "secret" not in str(error.value)
    finally:
        adapter.close()


def test_factory_and_qdrant_are_lazy_without_disk_lock(tmp_path):
    config = settings(data_dir=tmp_path / "uncreated", reranker_base_url="", reranker_model="")
    adapters = build_retrieval(config)
    try:
        assert isinstance(adapters[0], HTTPEmbedding)
        assert isinstance(adapters[1], QdrantIndex)
        assert adapters[2] is None
        assert adapters[1]._client is None
        assert not config.data_dir.exists()
    finally:
        for adapter in adapters:
            if adapter is not None:
                adapter.close()
    assert not config.data_dir.exists()


def test_qdrant_space_generation_isolation_and_payload_minimization():
    with closing(QdrantClient(":memory:")) as client:
        adapter = QdrantIndex(settings(), client=client)
        adapter.synchronize("space-a", "old", [chunk()], [[1, 0]])
        adapter.synchronize("space-a", "current", [chunk()], [[0, 1]])
        adapter.synchronize("space-b", "current", [chunk("chunk-b", "space-b")], [[1, 0]])
        # Idempotent synchronization and old generation survive new upsert.
        adapter.synchronize("space-a", "current", [chunk()], [[0, 1]])
        assert [hit.chunk_id for hit in adapter.search("space-a", "old", [1, 0], 1)] == ["chunk-a"]
        assert adapter.search("space-a", "old", [1, 0], 1)[0].score > 0.99
        assert adapter.search("space-a", "current", [1, 0], 1)[0].score == 0
        assert adapter.search("space-a", "missing", [1, 0], 1) == []
        assert adapter.search("not-indexed", "current", [1, 0], 1) == []
        assert len(client.get_collections().collections) == 2
        collection = adapter._collection_name("space-a")
        assert "space-a" not in collection
        points, _ = client.scroll(collection_name=collection, limit=10, with_payload=True)
        assert len(points) == 2
        for point in points:
            assert set(point.payload) == {"chunk_id", "generation", "space_id", "version_id"}
            assert "PRIVATE" not in json.dumps(point.payload)
        # Local calls are safe under worker thread switching through the RLock.
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: adapter.search("space-a", "current", [0, 1], 1), range(16)))
        assert all(result[0].chunk_id == "chunk-a" for result in results)


def test_generation_filter_is_applied_before_top_k():
    with closing(QdrantClient(":memory:")) as client:
        adapter = QdrantIndex(settings(), client=client)
        adapter.synchronize("space-a", "old", [chunk("best-but-old")], [[1, 0]])
        adapter.synchronize("space-a", "new", [chunk("current")], [[0, 1]])
        assert [hit.chunk_id for hit in adapter.search("space-a", "new", [1, 0], 1)] == ["current"]


def test_qdrant_dimension_mismatch_never_recreates_collection():
    with closing(QdrantClient(":memory:")) as client:
        adapter = QdrantIndex(settings(), client=client)
        collection = adapter._collection_name("space-a")
        client.create_collection(collection_name=collection, vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE))
        with pytest.raises(RetrievalAdapterError, match="dimension"):
            adapter.synchronize("space-a", "current", [chunk()], [[1, 0]])
        assert client.get_collection(collection).config.params.vectors.size == 3


def test_qdrant_validates_all_points_before_writing():
    with closing(QdrantClient(":memory:")) as client:
        adapter = QdrantIndex(settings(), client=client)
        for chunks, vectors in [
            ([chunk()], []), ([chunk(), chunk()], [[1, 0], [1, 0]]),
            ([chunk(space="other")], [[1, 0]]), ([chunk()], [[True, 0]]),
            ([chunk()], [[math.nan, 0]]), ([chunk()], [[1, 0, 0]]),
        ]:
            with pytest.raises(RetrievalAdapterError):
                adapter.synchronize("space-a", "generation", chunks, vectors)
        assert client.get_collections().collections == []


def test_qdrant_count_verification_failure():
    with closing(QdrantClient(":memory:")) as client:
        adapter = QdrantIndex(settings(), client=client)
        client.count = lambda **kwargs: SimpleNamespace(count=0)
        with pytest.raises(RetrievalAdapterError, match="count"):
            adapter.synchronize("space-a", "generation", [chunk()], [[1, 0]])


def test_lazy_qdrant_client_is_singleton_across_threads(monkeypatch):
    import qdrant_client

    real_client = qdrant_client.QdrantClient
    created = []

    def memory_client(**kwargs):
        assert kwargs["force_disable_check_same_thread"] is True
        assert kwargs["path"].endswith("qdrant-research")
        client = real_client(":memory:")
        created.append(client)
        return client

    monkeypatch.setattr(qdrant_client, "QdrantClient", memory_client)
    adapter = QdrantIndex(settings())
    assert created == []
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(lambda _: adapter.search("space-a", "generation", [1, 0], 1), range(16))) == [[]] * 16
        assert len(created) == 1
        adapter.synchronize("space-a", "generation", [chunk()], [[1, 0]])
        assert len(created) == 1
    finally:
        adapter.close()
    adapter.close()
    with pytest.raises(RetrievalAdapterError, match="closed"):
        adapter.search("space-a", "generation", [1, 0], 1)
    assert len(created) == 1


def test_external_qdrant_requires_consent_even_with_injected_client():
    with pytest.raises(ValueError, match="consent"):
        QdrantIndex(settings(allow_external=False, qdrant_url="https://qdrant.test"), client=object())


def test_empty_generation_is_a_valid_empty_snapshot():
    with closing(QdrantClient(":memory:")) as client:
        adapter = QdrantIndex(settings(), client=client)
        adapter.synchronize("space-a", "old", [chunk()], [[1, 0]])
        adapter.synchronize("space-a", "empty", [], [])
        assert adapter.search("space-a", "empty", [1, 0], 5) == []
        assert len(adapter.search("space-a", "old", [1, 0], 5)) == 1
