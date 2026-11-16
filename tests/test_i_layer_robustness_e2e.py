"""I-layer robustness: provider degradation, input extremes and run limits.

Evidence split agreed for the I layer:
  * this file = 代码级 baseline, deterministic, runs in CI (fault stubs injected
    at the port boundary, no network);
  * a one-off real disconnect drill is separate end-to-end evidence and is not
    covered here (see the note in the scoring workbook).

The stubs raise what a dead service raises at the adapter boundary
(``ConnectionError``), so what is proven is: the pipeline detects the failure,
emits ``retrieval.degraded``, continues on local lexical retrieval, and still
answers from authorized evidence. What is not proven here: real client error
shapes (5xx/gRPC/timeout), retry/backoff parameters, or that the user is told
about the degradation in prose.
"""
from __future__ import annotations

import asyncio
import json
import secrets

import pytest

from app.application.agents.orchestrator import ResearchAssistant
from app.application.usecases.chat_runs import RunManager
from app.application.usecases.retrieval import ResearchRetrieval
from app.domain.errors import DomainError
from app.infrastructure.events.run_bus import InMemoryRunBus
from app.infrastructure.persistence.factory import build_research_service

OWN_TITLE = "本地实验记录"
OWN_BODY = "本地实验记录：在 25 摄氏度下重复三次，结果一致"
QUESTION = "本地实验重复了几次？"


class Runtime:
    """Offline stand-in for the model runtime; never calls the network."""

    def __init__(self):
        self.calls = 0

    def new_request_state(self):
        return object()

    async def run_coordinator(self, agent, payload, **options):
        self.calls += 1
        await options["check"]()
        tools = {tool.name: tool.invoke for tool in agent.tools}
        found = await tools["search_documents"](OWN_BODY)
        hits = found.get("evidence") or []
        if not hits:
            return json.dumps({"answer_status": "insufficient", "claims": [
                {"source_kind": "clarification", "text": "当前库中没有检索到相关记录。"}]},
                ensure_ascii=False)
        item = await tools["read_document"](hits[0]["version_id"])
        return json.dumps({"answer_status": "sufficient", "claims": [{
            "source_kind": "material", "text": item["content"],
            "version_ids": [item["version_id"]],
            "evidence_quotes": [{"evidence_id": item["evidence_id"],
                                 "version_id": item["version_id"],
                                 "text": item["content"]}]}]}, ensure_ascii=False)

    async def run_specialist(self, *args, **kwargs):
        pytest.fail("No fixed specialist stage")


class FakeEmbedding:
    """Port-level stand-in for the embedding HTTP service."""

    max_batch_size = 10

    def __init__(self, fail: bool):
        self.fail = fail
        self.calls = 0

    def embed_batch(self, texts):
        self.calls += 1
        if self.fail:
            raise ConnectionError("embedding service unreachable")
        return [[float(len(text)), 1.0] for text in texts]


class FakeIndex:
    """Port-level stand-in for Qdrant."""

    def __init__(self, fail: bool):
        self.fail = fail
        self.sync_calls = 0
        self.search_calls = 0

    def synchronize(self, space_id, generation, chunks, embeddings):
        self.sync_calls += 1
        if self.fail:
            raise ConnectionError("qdrant unreachable")

    def search(self, space_id, generation, vector, limit):
        self.search_calls += 1
        if self.fail:
            raise ConnectionError("qdrant unreachable")
        return []


def build(tmp_path, *, embedding_fail=False, index_fail=False, vector=True):
    retrieval = None
    embedding = index = None
    if vector:
        embedding = FakeEmbedding(embedding_fail)
        index = FakeIndex(index_fail)
        retrieval = ResearchRetrieval(embedding, index)
    service = build_research_service(tmp_path / "research.db", retrieval=retrieval)
    owner = service.bootstrap("owner@example.org", secrets.token_urlsafe(18), "实验课题")["user"]["id"]
    space = next(s["id"] for s in service.list_spaces(owner) if s["kind"] == "basis")
    service.add_document(owner, space, OWN_TITLE, OWN_BODY)
    return {
        "service": service, "owner": owner, "space": space,
        "embedding": embedding, "index": index,
    }


async def run_chat(world, question=QUESTION):
    manager = RunManager(world["service"], ResearchAssistant(world["service"], Runtime()),
                         InMemoryRunBus())
    conversation = world["service"].create_conversation(world["owner"], world["space"])["id"]
    run = await manager.start(world["owner"], world["space"], conversation, question)
    reader = manager.subscribe(world["owner"], run["run_id"])
    events = []
    try:
        while True:
            event = await reader.__anext__()
            events.append({"type": event.type, "data": event.data})
            if event.type in ("answer.final", "run.completed", "run.failed"):
                break
    finally:
        await reader.aclose()
        await manager.aclose()
    return events, run


def degraded_reasons(events) -> list[str]:
    return [e["data"].get("reason") for e in events if e["type"] == "retrieval.degraded"]


def assert_still_answers(events, label: str) -> None:
    final = next(e for e in events if e["type"] == "answer.final")
    assert final["data"]["fallback"] is False, f"{label} 最终回答退化成兜底文案"
    assert OWN_BODY in final["data"]["answer"], f"{label} 词法兜底未返回本空间证据"
    assert final["data"]["citations"], f"{label} 没有可用引用"


async def test_qdrant_down_falls_back_to_lexical_and_emits_degraded(tmp_path):
    """AI-D5-M-01：Qdrant 不可用 → 词法检索继续工作。"""
    world = build(tmp_path, index_fail=True)
    events, run = await run_chat(world)
    assert world["index"].sync_calls >= 1, "索引同步没有被触发，用例无效"
    assert "index_sync_failed" in degraded_reasons(events), f"未产生降级事件：{degraded_reasons(events)}"
    assert_still_answers(events, "Qdrant 降级")


async def test_embedding_down_falls_back_to_lexical_and_emits_degraded(tmp_path):
    """AI-D5-M-02：Embedding 不可用 → 词法检索继续工作。"""
    world = build(tmp_path, embedding_fail=True)
    events, run = await run_chat(world)
    assert world["embedding"].calls >= 1, "向量化没有被触发，用例无效"
    assert "embedding_failed" in degraded_reasons(events), f"未产生降级事件：{degraded_reasons(events)}"
    assert_still_answers(events, "Embedding 降级")


async def test_all_vector_services_down_still_answers_from_lexical(tmp_path):
    """AI-D5-M-03：向量链路整体不可用（故障桩口径）。"""
    world = build(tmp_path, embedding_fail=True, index_fail=True)
    events, run = await run_chat(world)
    reasons = degraded_reasons(events)
    assert reasons, "三服务同时不可用却没有任何降级事件"
    assert set(reasons) & {"embedding_failed", "index_sync_failed", "vector_unavailable"}, reasons
    assert_still_answers(events, "向量链路整体降级")


async def test_empty_and_overlong_questions_are_rejected_before_the_model(tmp_path):
    """AI-D5-A-01（边界）：空/超长输入在运行入口被拒绝，不进入模型。"""
    world = build(tmp_path, vector=False)
    manager = RunManager(world["service"], ResearchAssistant(world["service"], Runtime()),
                         InMemoryRunBus())
    conversation = world["service"].create_conversation(world["owner"], world["space"])["id"]
    try:
        for bad in ("", "   ", "x" * 8001):
            with pytest.raises(DomainError) as info:
                await manager.start(world["owner"], world["space"], conversation, bad)
            assert info.value.status_code == 422
    finally:
        await manager.aclose()


async def test_symbol_only_question_completes_without_crash(tmp_path):
    """AI-D5-A-01（纯符号）：不崩溃，走正常完整性校验或澄清路径。"""
    world = build(tmp_path, vector=False)
    events, run = await run_chat(world, question="！@#￥%……&*（）——+{}【】")
    terminal = events[-1]["type"]
    assert terminal in ("answer.final", "run.completed"), terminal
    final = next(e for e in events if e["type"] == "answer.final")
    assert final["data"]["answer"].strip(), "纯符号输入未产生任何可展示回答"


async def test_five_parallel_runs_reject_cleanly_instead_of_crashing(tmp_path):
    """AI-D5-T-01：5 题并发 → 单账号并发上限内受理，其余返回 429，不崩溃。"""
    world = build(tmp_path, vector=False)
    manager = RunManager(world["service"], ResearchAssistant(world["service"], Runtime()),
                         InMemoryRunBus(), max_concurrent=2, actor_limit=1)
    conversations = [world["service"].create_conversation(world["owner"], world["space"])["id"]
                     for _ in range(5)]

    async def attempt(conversation):
        try:
            return await manager.start(world["owner"], world["space"], conversation, QUESTION)
        except DomainError as exc:
            return exc

    try:
        results = await asyncio.gather(*(attempt(c) for c in conversations))
        accepted = [r for r in results if isinstance(r, dict)]
        rejected = [r for r in results if isinstance(r, DomainError)]
        assert len(accepted) == 1, f"并发受理数不符：{len(accepted)}"
        assert len(rejected) == 4 and all(r.status_code == 429 for r in rejected), \
            [getattr(r, "status_code", r) for r in rejected]

        reader = manager.subscribe(world["owner"], accepted[0]["run_id"])
        try:
            while True:
                event = await reader.__anext__()
                if event.type in ("answer.final", "run.completed", "run.failed"):
                    break
        finally:
            await reader.aclose()
    finally:
        await manager.aclose()


async def test_request_id_is_idempotent_and_mismatch_is_conflict(tmp_path):
    """AI-D5-T-02：同一 request_id 复用旧运行；换问题则 409。"""
    world = build(tmp_path, vector=False)
    manager = RunManager(world["service"], ResearchAssistant(world["service"], Runtime()),
                         InMemoryRunBus())
    conversation = world["service"].create_conversation(world["owner"], world["space"])["id"]
    try:
        first = await manager.start(world["owner"], world["space"], conversation, QUESTION,
                                    request_id="idem-h-1")
        replay = await manager.start(world["owner"], world["space"], conversation, QUESTION,
                                     request_id="idem-h-1")
        assert replay["run_id"] == first["run_id"], "相同 request_id 生成了新的运行"

        with pytest.raises(DomainError) as info:
            await manager.start(world["owner"], world["space"], conversation, "另一个问题",
                                request_id="idem-h-1")
        assert info.value.status_code == 409
        assert json.loads(json.dumps(first, ensure_ascii=False))["request_id"] == "idem-h-1"

        reader = manager.subscribe(world["owner"], first["run_id"])
        try:
            while True:
                event = await reader.__anext__()
                if event.type in ("answer.final", "run.completed", "run.failed"):
                    break
        finally:
            await reader.aclose()
    finally:
        await manager.aclose()
