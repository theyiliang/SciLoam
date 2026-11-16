"""Synchronous, explicitly consented OpenAI-compatible embedding adapter."""
from __future__ import annotations

import math
from numbers import Real

import httpx

from .settings import RetrievalSettings
from app.domain.ai_ports import synchronous_run_context


def check_run():
    context = synchronous_run_context.get()
    if context is not None:
        context.check()


class RetrievalAdapterError(RuntimeError):
    """Safe diagnostic: never includes remote bodies, input text or credentials."""


def finite_vector(value: object, dimension: int) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != dimension:
        raise RetrievalAdapterError("Invalid embedding dimension")
    result = []
    for number in value:
        if isinstance(number, bool) or not isinstance(number, Real):
            raise RetrievalAdapterError("Invalid embedding value")
        try:
            number = float(number)
        except (ValueError, OverflowError):
            raise RetrievalAdapterError("Invalid embedding value") from None
        if not math.isfinite(number):
            raise RetrievalAdapterError("Invalid embedding value")
        result.append(number)
    return result


class HTTPEmbedding:
    def __init__(self, settings: RetrievalSettings, *, transport: httpx.BaseTransport | None = None) -> None:
        if not settings.allow_external:
            raise ValueError("External research retrieval requires explicit consent")
        if not settings.embedding_base_url or not settings.embedding_model:
            raise ValueError("Embedding endpoint and model are required")
        self._settings = settings
        self._client = httpx.Client(timeout=settings.timeout_seconds, transport=transport, follow_redirects=False, trust_env=False)

    @property
    def max_batch_size(self) -> int:
        return self._settings.embedding_max_batch

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
            raise ValueError("Embedding input must be a list of strings")
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._settings.embedding_max_batch):
            batch = texts[start:start + self._settings.embedding_max_batch]
            headers = {"Authorization": f"Bearer {self._settings.embedding_api_key}"} if self._settings.embedding_api_key else {}
            check_run()
            try:
                response = self._client.post(
                    f"{self._settings.embedding_base_url.rstrip('/')}/embeddings",
                    headers=headers,
                    json={"model": self._settings.embedding_model, "input": batch},
                )
                response.raise_for_status()
                body = response.json()
            except Exception:
                raise RetrievalAdapterError("Embedding request failed") from None
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, list) or len(data) != len(batch):
                raise RetrievalAdapterError("Invalid embedding response count")
            ordered: dict[int, list[float]] = {}
            for item in data:
                if not isinstance(item, dict):
                    raise RetrievalAdapterError("Invalid embedding response item")
                index = item.get("index")
                if type(index) is not int or index < 0 or index >= len(batch) or index in ordered:
                    raise RetrievalAdapterError("Invalid embedding response index")
                ordered[index] = finite_vector(item.get("embedding"), self._settings.embedding_dim)
            vectors.extend(ordered[index] for index in range(len(batch)))
        return vectors

    def close(self) -> None:
        self._client.close()


OpenAIEmbedding = HTTPEmbedding
