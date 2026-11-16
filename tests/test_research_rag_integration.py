"""Real service/UoW and in-memory Qdrant with offline model adapters."""
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from app.application.usecases.retrieval import ResearchRetrieval
from app.domain.errors import DomainError
from app.infrastructure.persistence.factory import build_research_service
from app.infrastructure.retrieval.qdrant_index import QdrantIndex
from app.infrastructure.retrieval.settings import ResearchRetrievalSettings


class Embedding:
    def __init__(self):
        self.calls = []

    def embed_batch(self, texts):
        self.calls.append(list(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]


class Reranker:
    def __init__(self):
        self.seen = []

    def rerank(self, query, documents):
        self.seen.extend(documents)
        return [10.0 if "召回率" in text else 0.5 for text in documents]


def build(tmp_path):
    embedding, reranker = Embedding(), Reranker()
    index = QdrantIndex(ResearchRetrievalSettings(embedding_dim=3), client=QdrantClient(":memory:"))
    retrieval = ResearchRetrieval(embedding, index, reranker, chunk_size=100, overlap=20)
    service = build_research_service(tmp_path / "research.db", retrieval=retrieval)
    owner = service.bootstrap("owner@example.org", "integration-password-123", "RAG课题")["user"]
    return service, owner["id"], embedding, reranker, index


def test_service_retrieval_uses_hybrid_rerank_and_version_generation(tmp_path):
    service, actor, embedder, reranker, index = build(tmp_path)
    try:
        private = next(s for s in service.list_spaces(actor) if s["kind"] == "private")
        doc = service.add_document(actor, private["id"], "实验评价", "背景描述。" * 60 + "\n\n实验评价使用召回率。", source="研究记录")
        first = service.search(actor, private["id"], "评价指标")
        assert first and first[0]["rerank_applied"]
        assert first[0]["retrieval_strategy"] == "hybrid_rrf"
        assert "召回率" in first[0]["content"]
        assert first[0]["content"] in service.get_version(actor, first[0]["version_id"])["content"]
        assert first[0]["chunk_end"] - first[0]["chunk_start"] == len(first[0]["content"])
        service.search(actor, private["id"], "召回率")
        assert len(embedder.calls[-1]) == 1, "same snapshot only embeds the query"
        service.add_version(actor, doc["id"], "新版本采用精确率。")
        updated = service.search(actor, private["id"], "评价")
        assert updated[0]["version_id"] != first[0]["version_id"]
        assert updated[0]["generation"] != first[0]["generation"]
        assert all("背景描述" not in item["content"] for item in updated)
    finally:
        index.close()


def test_private_content_never_reaches_other_users_retriever(tmp_path):
    service, actor, embedder, reranker, index = build(tmp_path)
    try:
        invite = service.create_invite(actor, "member@example.org")
        member = service.register("member@example.org", "member-password-123", invite["invite_token"])["user"]["id"]
        private = next(s for s in service.list_spaces(actor) if s["kind"] == "private")
        service.add_document(actor, private["id"], "私有材料", "SECRET_PRIVATE_EXPERIMENT")
        with pytest.raises(DomainError):
            service.search(member, private["id"], "实验")
        assert not embedder.calls and not reranker.seen
    finally:
        index.close()


def test_revoke_during_embedding_is_not_a_fallback(tmp_path):
    service, owner, embedder, reranker, index = build(tmp_path)
    try:
        invitation = service.create_invite(owner, "member@example.org")
        member = service.register("member@example.org", "member-password-123", invitation["invite_token"])["user"]["id"]
        space = service.create_space(owner, "共享任务")
        service.grant_space(owner, space["id"], member, "reader")
        service.add_document(owner, space["id"], "实验", "实验评价使用召回率。")
        original = embedder.embed_batch
        def revoke(texts):
            result = original(texts)
            service.revoke_space(owner, space["id"], member)
            return result
        embedder.embed_batch = revoke
        with pytest.raises(DomainError):
            service.search(member, space["id"], "实验")
        assert not reranker.seen
    finally:
        index.close()
