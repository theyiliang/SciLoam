"""Real authorized HTTP calls: actual retrieval stage events and cooperative stop."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from app.application.usecases.retrieval import ResearchRetrieval
from app.domain.retrieval_ports import VectorHit
from tests.test_sciloam_streaming_http import live, start


class Embedding:
    max_batch_size = 1

    def __init__(self, *, fail=False, pause=False):
        self.fail, self.pause = fail, pause
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = []

    def embed_batch(self, texts):
        self.calls.append(texts)
        self.entered.set()
        if self.pause:
            assert self.release.wait(5)
        if self.fail:
            raise RuntimeError("PRIVATE_PROVIDER_URL_AND_SECRET")
        return [[1., 0.] for _ in texts]


class Index:
    def __init__(self):
        self.chunks = []
        self.calls = []

    def synchronize(self, space, generation, chunks, vectors):
        self.calls.append("sync")
        self.chunks = chunks

    def search(self, space, generation, vector, limit):
        self.calls.append("search")
        return [VectorHit(chunk.chunk_id, 1.) for chunk in self.chunks]


class Reranker:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def rerank(self, query, documents):
        self.calls += 1
        if self.fail:
            raise RuntimeError("PRIVATE_RERANK_SECRET")
        return [1.] * len(documents)


def local_assistant(app):
    app.state.container.runs.assistant.runtime = None


@pytest.mark.parametrize("failure", [None, "embedding", "rerank"])
def test_http_actual_retrieval_rerank_events_are_safe(live, failure):
    app, client, actor, space, conversation, provider = live
    embedding, index, reranker = Embedding(fail=failure == "embedding"), Index(), Reranker(fail=failure == "rerank")
    app.state.service._retrieval = ResearchRetrieval(embedding, index, reranker)
    local_assistant(app)
    response = client.post("/api/chat", json={"space_id": space, "conversation_id": conversation,
        "question": "lactate growth", "request_id": "retrieval"})
    assert response.status_code == 200, response.text
    result = response.json()
    events = result["events"]
    kinds = [event["type"] for event in events]
    assert "retrieval.started" in kinds and "retrieval.completed" in kinds
    assert "rerank.started" in kinds
    assert ("rerank.degraded" if failure == "rerank" else "rerank.completed") in kinds
    if failure == "embedding":
        assert {"type": "retrieval.degraded", "reason": "embedding_failed"} in events
        assert not index.calls
    assert reranker.calls == 1
    assert "PRIVATE_" not in json.dumps(events)
    assert all(set(event) <= {"type", "reason", "strategy", "hit_count", "candidate_count", "rerank_applied", "degraded", "reasons", "agent", "specialist", "mode", "max_messages", "max_chars"} for event in events)
    wire = client.get(f"/api/chat/runs/{result['run_id']}/events")
    assert wire.status_code == 200
    assert "event: rerank.started" in wire.text
    assert "PRIVATE_" not in wire.text


@pytest.mark.parametrize("stop", ["cancel", "logout"])
def test_http_cooperative_stop_between_embedding_batches(live, stop):
    app, client, actor, space, conversation, provider = live
    embedding, index, reranker = Embedding(pause=True), Index(), Reranker()
    app.state.service._retrieval = ResearchRetrieval(embedding, index, reranker)
    local_assistant(app)
    manager = app.state.container.runs
    manager.disconnect_grace_seconds = 10
    run, body = start(client, space, conversation)
    assert embedding.entered.wait(3)
    if stop == "cancel":
        response = client.post(f"/api/chat/runs/{run['run_id']}/cancel")
    else:
        response = client.post("/api/auth/logout")
    assert response.status_code == 200, response.text
    embedding.release.set()
    # Acquire the pipeline's per-space lock: it is released only after the
    # paused worker observes cancellation/revocation and exits, not after a nap.
    lock = app.state.service._retrieval._lock(space)
    assert lock.acquire(timeout=3)
    lock.release()
    assert len(embedding.calls) == 1
    assert index.calls == [] and reranker.calls == 0
    history = app.state.service.get_conversation(actor, conversation)["messages"]
    assert not any(item["role"] == "assistant" for item in history)
