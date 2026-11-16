"""D-layer end-to-end: cross-space isolation through the orchestration layer.

The unit suites (test_sciloam_access / review_snapshots / lifecycle) prove the
domain permission rules. They do not exercise the path where run events and
citations are produced, which is where V-01 (reading another space's document)
and V-02 (leaking credentials) would actually surface. These tests drive a real
chat run through RunManager + ResearchAssistant over real storage.
"""
from __future__ import annotations

import json
import secrets

import pytest

from app.application.agents.orchestrator import ResearchAssistant
from app.application.usecases.chat_runs import RunManager
from app.domain.errors import DomainError
from app.infrastructure.events.run_bus import InMemoryRunBus
from app.infrastructure.persistence.factory import build_research_service

SECRET_TITLE = "越权哨兵文档标题_ZZ"
SECRET_BODY = "越权哨兵正文内容_ZZ_7788：本段文字只应出现在 A 空间"
OWN_BODY = "Bob 自己的证据：本地实验在 25 摄氏度下重复三次"
PASSWORD = "Owner-Password-Sentinel-9911"


class Runtime:
    """Offline stand-in for the model runtime; never calls the network."""

    def __init__(self, action):
        self.action = action
        self.calls = 0

    def new_request_state(self):
        return object()

    async def run_coordinator(self, agent, payload, **options):
        self.calls += 1
        await options["check"]()
        tools = {tool.name: tool.invoke for tool in agent.tools}
        return json.dumps(await self.action(tools, payload), ensure_ascii=False)

    async def run_specialist(self, *args, **kwargs):
        pytest.fail("No fixed specialist stage")


def material_claim(item):
    """Build a citation-bearing claim from a real ``read_document`` result."""
    return {"source_kind": "material", "text": item["content"],
            "version_ids": [item["version_id"]],
            "evidence_reason": "The source states it directly.",
            "evidence_quotes": [{"evidence_id": item["evidence_id"],
                                 "version_id": item["version_id"],
                                 "text": item["content"]}]}


@pytest.fixture
def spaces(tmp_path):
    service = build_research_service(tmp_path / "research.db")
    alice = service.bootstrap("alice@example.org", PASSWORD, "Alice 课题")["user"]["id"]
    invite = service.create_invite(alice, "bob@example.org", "member")
    bob = service.register("bob@example.org", secrets.token_urlsafe(24),
                           invite["invite_token"])["user"]["id"]
    alice_space = next(s["id"] for s in service.list_spaces(alice) if s["kind"] == "basis")
    bob_space = service.create_space(bob, "Bob 私有空间")
    secret = service.add_document(alice, alice_space, SECRET_TITLE, SECRET_BODY)
    own = service.add_document(bob, bob_space["id"], "Bob 证据", OWN_BODY)
    conv = service.create_conversation(bob, bob_space["id"])["id"]
    return {
        "service": service, "alice": alice, "bob": bob,
        "alice_space": alice_space, "bob_space": bob_space["id"],
        "alice_version": secret["latest_version"]["id"],
        "bob_version": own["latest_version"]["id"],
        "conv": conv,
    }


def assert_no_foreign_documents(blob: str) -> None:
    for sentinel in (SECRET_TITLE, SECRET_BODY, PASSWORD):
        assert sentinel not in blob, f"事件/回答里出现了其他空间的标记：{sentinel}"


def assert_no_credentials(blob: str) -> None:
    for sentinel in (PASSWORD, "password_hash", "session_token", "Bearer "):
        assert sentinel not in blob, f"事件/回答里出现了凭据痕迹：{sentinel}"


def test_search_is_scoped_to_the_requested_space(spaces):
    service, alice, bob = spaces["service"], spaces["alice"], spaces["bob"]
    # 同一关键词在它自己的空间里必须能搜到，否则「搜不到」是假通过
    assert service.search(alice, spaces["alice_space"], SECRET_BODY)
    assert service.search(bob, spaces["alice_space"], SECRET_BODY)  # 成员对 basis 是 reader
    assert service.search(bob, spaces["bob_space"], SECRET_BODY) == []
    assert service.search(bob, spaces["bob_space"], OWN_BODY)


async def test_chat_run_rejects_selected_version_outside_readable_scope(spaces):
    service, bob, conv = spaces["service"], spaces["bob"], spaces["conv"]
    manager = RunManager(service, ResearchAssistant(service, Runtime(None)), InMemoryRunBus())
    try:
        with pytest.raises(DomainError) as info:
            await manager.start(bob, spaces["bob_space"], conv, "这份材料说了什么",
                                selected_version_id=spaces["alice_version"])
        assert info.value.status_code == 403
    finally:
        await manager.aclose()


async def test_events_answer_and_citations_never_carry_other_space_documents(spaces):
    service, bob, conv = spaces["service"], spaces["bob"], spaces["conv"]
    own_version = spaces["bob_version"]
    tool_observations = {}

    async def action(tools, payload):
        # 真实调用检索工具：跨空间过滤必须发生在这里，而不是靠模型自觉
        foreign = await tools["search_documents"](SECRET_BODY)
        tool_observations["search_foreign"] = json.dumps(foreign, ensure_ascii=False)
        own_rows = await tools["search_documents"](OWN_BODY)
        tool_observations["search_own"] = json.dumps(own_rows, ensure_ascii=False)
        hits = own_rows.get("evidence") or []
        assert hits and hits[0].get("version_id") == own_version, \
            f"本空间检索未返回自己的版本：{own_rows}"
        item = await tools["read_document"](own_version)
        tool_observations["read"] = json.dumps(item, ensure_ascii=False)
        return {"answer_status": "sufficient", "claims": [material_claim(item)]}

    manager = RunManager(service, ResearchAssistant(service, Runtime(action)), InMemoryRunBus())
    run = await manager.start(bob, spaces["bob_space"], conv, "本地实验重复了几次？")
    reader = manager.subscribe(bob, run["run_id"])
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

    # 1) 检索工具本身不返回他空间内容
    assert tool_observations, "模型侧工具没有被调用，测试无效"
    foreign_rows = json.loads(tool_observations["search_foreign"])
    assert foreign_rows.get("evidence") == [], f"他空间检索返回了内容：{foreign_rows}"
    assert_no_foreign_documents(tool_observations["search_foreign"])
    assert SECRET_TITLE not in tool_observations["read"]

    # 2) 事件流 + 最终回答不含他空间任何痕迹
    blob = json.dumps(events, ensure_ascii=False)
    assert_no_foreign_documents(blob)
    assert_no_credentials(blob)
    assert spaces["alice_version"] not in blob

    # 3) 阳性对照：合法内容确实出现在事件/回答里，证明上面的「不出现」不是因为流是空的
    assert OWN_BODY in blob, "本空间的证据没有出现在回答中，跨空间断言是空转"

    final = next(e for e in events if e["type"] == "answer.final")
    assert final["data"]["fallback"] is False
    cited = {c.get("version_id") for c in final["data"]["citations"] if isinstance(c, dict)}
    assert cited == {own_version}, f"引用了非本空间版本：{cited}"
    kinds = [e["type"] for e in events]
    assert "retrieval.started" in kinds


async def test_saved_conversation_carries_no_foreign_documents(spaces):
    service, bob, conv = spaces["service"], spaces["bob"], spaces["conv"]
    own_version = spaces["bob_version"]

    async def action(tools, payload):
        await tools["search_documents"](SECRET_BODY)
        # read_document 只接受本请求已发现的版本；先用本空间检索登记，
        # 同时验证「能搜到自己」这一点，避免空转。
        own_rows = await tools["search_documents"](OWN_BODY)
        assert own_rows.get("evidence"), f"本空间检索没有结果：{own_rows}"
        item = await tools["read_document"](own_version)
        return {"answer_status": "sufficient", "claims": [material_claim(item)]}

    manager = RunManager(service, ResearchAssistant(service, Runtime(action)), InMemoryRunBus())
    run = await manager.start(bob, spaces["bob_space"], conv, "本地实验重复了几次？")
    reader = manager.subscribe(bob, run["run_id"])
    try:
        while True:
            event = await reader.__anext__()
            if event.type in ("answer.final", "run.completed"):
                break
    finally:
        await reader.aclose()
        await manager.aclose()

    # 落库后的会话历史同样不能带出其他空间的文档痕迹
    conversation = service.get_conversation(bob, conv)
    blob = json.dumps(conversation, ensure_ascii=False)
    assert_no_foreign_documents(blob)
    assert_no_credentials(blob)
    assert own_version in blob, "本空间引用没有落库，断言是空转"
