"""Compose optional research retrieval adapters without opening a vector store."""
from __future__ import annotations

from .embedding import HTTPEmbedding
from .qdrant_index import QdrantIndex
from .reranker import HTTPReranker
from .settings import ResearchRetrievalSettings, load_retrieval_settings


def build_retrieval(settings: ResearchRetrievalSettings | None = None) -> tuple[HTTPEmbedding | None, QdrantIndex | None, HTTPReranker | None]:
    settings = settings if settings is not None else load_retrieval_settings()
    if settings.mode == "local":
        # Default local lexical retrieval does not import/open Qdrant or HTTP.
        return None, None, None
    if not settings.allow_external:
        raise ValueError("RESEARCH_RETRIEVAL_MODE=hybrid requires RESEARCH_RAG_ALLOW_EXTERNAL=1")
    if bool(settings.reranker_base_url) != bool(settings.reranker_model):
        raise ValueError("Reranker endpoint and model must be configured together")
    embedding = HTTPEmbedding(settings)
    try:
        index = QdrantIndex(settings)
        reranker = HTTPReranker(settings) if settings.reranker_base_url else None
        return embedding, index, reranker
    except Exception:
        embedding.close()
        raise
