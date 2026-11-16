"""Synchronous /rerank adapter returning scores in original candidate order."""
from __future__ import annotations

import math
from numbers import Real

import httpx

from .embedding import RetrievalAdapterError, check_run
from .settings import RetrievalSettings


class HTTPReranker:
    def __init__(self, settings: RetrievalSettings, *, transport: httpx.BaseTransport | None = None) -> None:
        if not settings.allow_external:
            raise ValueError("External research retrieval requires explicit consent")
        if not settings.reranker_base_url or not settings.reranker_model:
            raise ValueError("Reranker endpoint and model are required")
        self._settings = settings
        self._client = httpx.Client(timeout=settings.timeout_seconds, transport=transport, follow_redirects=False, trust_env=False)

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not isinstance(query, str) or not isinstance(documents, list) or any(not isinstance(text, str) for text in documents):
            raise ValueError("Invalid reranker input")
        if len(documents) > self._settings.max_candidates:
            raise ValueError("Too many reranker candidates")
        if not documents:
            return []
        headers = {"Authorization": f"Bearer {self._settings.reranker_api_key}"} if self._settings.reranker_api_key else {}
        check_run()
        try:
            response = self._client.post(
                f"{self._settings.reranker_base_url.rstrip('/')}/rerank",
                headers=headers,
                json={"model": self._settings.reranker_model, "query": query, "documents": documents, "top_n": len(documents), "return_documents": False},
            )
            response.raise_for_status()
            body = response.json()
        except Exception:
            raise RetrievalAdapterError("Reranker request failed") from None
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list) or len(results) != len(documents):
            raise RetrievalAdapterError("Invalid reranker response count")
        ordered: dict[int, float] = {}
        for item in results:
            if not isinstance(item, dict):
                raise RetrievalAdapterError("Invalid reranker response item")
            index, score = item.get("index"), item.get("relevance_score")
            if type(index) is not int or index < 0 or index >= len(documents) or index in ordered:
                raise RetrievalAdapterError("Invalid reranker response index")
            if isinstance(score, bool) or not isinstance(score, Real):
                raise RetrievalAdapterError("Invalid reranker score")
            try:
                score = float(score)
            except (OverflowError, ValueError):
                raise RetrievalAdapterError("Invalid reranker score") from None
            if not math.isfinite(score):
                raise RetrievalAdapterError("Invalid reranker score")
            ordered[index] = score
        return [ordered[index] for index in range(len(documents))]

    def close(self) -> None:
        self._client.close()
