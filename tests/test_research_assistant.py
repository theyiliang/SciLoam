"""Offline research-assistant tests; no provider clients make network calls."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatModelBase, ChatResponse

from app.application.agents.legacy_orchestrator import ResearchAssistant, answer
from app.application.agents.evidence import _Request
from app.application.agents.tools import describe_agents
from app.infrastructure.ai.factory import build_runtime
from app.infrastructure.ai.model_gateway import ResearchAISettings
from app.domain.errors import DomainError


def configured_assistant(service, *, model_factory):
    return ResearchAssistant(service, build_runtime(
        ResearchAISettings(llm_min_interval_seconds=0, llm_max_retries=0), model_factory=model_factory))


class FakeService:
    def __init__(self):
        self.allowed = True
        self.checks = 0
        self.messages = []
        self.searches = []
        self.version_checks = []
        self.versions = {
            "v1": {"space_id": "space", "document_id": "doc1", "title": "Experiment A",
                   "content": "Observed result A; uncertainty remains.", "source": "lab-A", "index_status": "ready"},
            "v2": {"space_id": "space", "document_id": "doc2", "title": "Experiment B",
                   "content": "Observed result B with a different method.", "source": "lab-B", "index_status": "ready"},
        }
        self.results = ["v1"]
        self.tasks = [{"id": "t1", "title": "Replication", "status": "pending_acceptance",
                       "due_date": "2026-11-01", "assignee_id": "alice",
                       "description": "PRIVATE DESCRIPTION",
                       "progress": [{"content": "PRIVATE OTHER SPACE BODY", "version_id": "private"}]}]

    def get_conversation(self, actor, conversation_id):
        assert actor == "alice"
        self.checks += 1
        if not self.allowed:
            raise DomainError(409, "Space permissions changed")
        return {"id": conversation_id, "space_id": "space", "messages": self.messages}

    def get_version(self, actor, version_id):
        assert actor == "alice"
        self.version_checks.append(version_id)
        if not self.allowed:
            raise DomainError(403, "Version permission denied")
        return dict(self.versions[version_id])

    def search(self, actor, space_id, query, limit=5):
        assert (actor, space_id, limit) == ("alice", "space", 5)
        self.searches.append(query)
        return [{"version_id": version_id, "content": "UNTRUSTED SEARCH BODY"} for version_id in self.results]

    def append_message(self, actor, conversation_id, role, content, citations):
        if not self.allowed:
            raise DomainError(403, "Cannot append")
        self.messages.append({"role": role, "content": content, "citations": citations})
        return self.messages[-1]

    def list_tasks(self, actor):
        assert actor == "alice"
        return self.tasks


class FakeModel(ChatModelBase):
    def __init__(self, name, calls, *, content=None, fail=False, hook=None, tool=None):
        # Base agent only needs these configuration fields; never create a real client.
        self.name, self.calls = name, calls
        self.content, self.fail, self.hook, self.tool = content, fail, hook, tool
        self.credential = None
        self.formatter = OpenAIChatFormatter()
        self.model = "offline-model"
        self.parameters = self.Parameters()
        self.stream, self.max_retries, self.context_size = False, 0, 128000
        self.retry_delay = 0
        self.count = 0

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.count += 1
        self.calls.append({"name": self.name, "messages": messages, "tools": tools or []})
        if self.hook:
            self.hook(self)
        if self.fail:
            raise RuntimeError("secret provider exception must not be exposed")
        if self.tool and self.count == 1:
            name, arguments = self.tool
            return ChatResponse(content=[ToolCallBlock(id="call1", name=name, input=json.dumps(arguments))], is_last=True)
        if self.name == "ResearchCoordinator":
            content = self.content if self.content is not None else json.dumps({
                "claims": [{"text": "Observed result A; uncertainty remains.", "version_ids": ["v1"], "task_ids": []}],
                "limitations": ["One document only"],
            })
        else:
            content = "Draft from the current evidence only."
        return ChatResponse(content=[TextBlock(text=content)], is_last=True)


def model_factory(calls, **kwargs):
    return lambda name: FakeModel(name, calls, **kwargs)


@pytest.fixture(autouse=True)
def no_provider_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)


async def test_no_key_returns_explicit_real_local_excerpts():
    service = FakeService()
    result = await answer(service, "alice", "space", "conversation", "Observed")
    assert result["fallback"] is True
    assert "不是AI总结" in result["answer"]
    assert "Observed result A" in result["answer"]
    assert "UNTRUSTED SEARCH BODY" not in result["answer"]
    assert result["citations"][0]["version_id"] == "v1"
    assert result["conversation_id"] == "conversation"
    assert [message["role"] for message in service.messages] == ["user", "assistant"]
    assert len(service.version_checks) >= 3


async def test_no_search_results_do_not_invent_answer():
    service = FakeService()
    service.results = []
    result = await answer(service, "alice", "space", "conversation", "unknown")
    assert result["citations"] == []
    assert result['answer_kind'] == 'general'
    assert '未配置' in result['answer'] and result['fallback'] is True


async def test_initial_permission_failure_never_searches_or_calls_model():
    service, calls = FakeService(), []
    service.allowed = False
    with pytest.raises(DomainError, match="permissions changed"):
        await configured_assistant(service, model_factory=model_factory(calls)).answer(
            "alice", "space", "conversation", "question")
    assert not service.searches and not service.messages and not calls


async def test_conversation_space_mismatch_fails_before_persisting():
    service = FakeService()
    with pytest.raises(DomainError, match="outside"):
        await answer(service, "alice", "other-space", "conversation", "question")
    assert not service.messages


async def test_search_cannot_smuggle_other_space_content():
    service = FakeService()
    service.versions["v1"]["space_id"] = "private"
    with pytest.raises(DomainError, match="outside"):
        await answer(service, "alice", "space", "conversation", "question")
    assert not any(message["role"] == "assistant" for message in service.messages)


async def test_model_error_is_safe_explicit_fallback():
    service, calls = FakeService(), []
    result = await configured_assistant(service, model_factory=model_factory(calls, fail=True)).answer(
        "alice", "space", "conversation", "Observed")
    assert result["fallback"] is True
    assert "模型不可用" in result["answer"]
    assert "secret provider" not in str(result)
    assert result["citations"][0]["version_id"] == "v1"


async def test_actual_specialist_and_coordinator_model_calls():
    service, calls = FakeService(), []
    result = await configured_assistant(service, model_factory=model_factory(calls)).answer(
        "alice", "space", "conversation", "Observed")
    assert result["fallback"] is False
    assert [call["name"] for call in calls] == ["ReadOnlyRetrievalAgent", "ResearchCoordinator"]
    assert result["answer"].startswith("回答\n")
    assert "v1" in result["answer"]


async def test_toolkits_are_separate_read_only_and_no_write_methods():
    service, calls = FakeService(), []
    assistant = configured_assistant(service, model_factory=model_factory(calls))
    request = _Request(service, "alice", "space", "conversation")
    state = assistant.runtime.new_request_state()
    agents = {name: assistant.runtime.build_agent(description, check=request.check, state=state)
              for name, description in describe_agents(request).items()}
    expected = {
        "ReadOnlyRetrievalAgent": {"search_documents", "read_version"},
        "CollaborationAnalysisAgent": {"search_sources", "read_source_version"},
        "ProgressAgent": {"list_tasks"},
        "ResearchCoordinator": {"read_evidence"},
    }
    assert len({id(agent.toolkit) for agent in agents.values()}) == 4
    for name, agent in agents.items():
        names = {schema["function"]["name"] for schema in await agent.toolkit.get_tool_schemas()}
        assert names == expected[name]
        assert agent.react_config.max_iters == 3
        for tool_name in names:
            assert (await agent.toolkit.get_tool(tool_name)).is_read_only
        assert not any(word in tool for tool in names for word in ("approve", "delete", "publish", "update", "add"))


async def test_revocation_during_model_call_returns_no_content():
    service, calls = FakeService(), []
    def revoke(model):
        service.allowed = False
    with pytest.raises(DomainError, match="permissions changed"):
        await configured_assistant(service, model_factory=model_factory(calls, hook=revoke)).answer(
            "alice", "space", "conversation", "Observed")
    assert len(calls) == 1
    assert not any(message["role"] == "assistant" for message in service.messages)


async def test_revocation_after_coordinator_does_not_emit_output():
    service, calls = FakeService(), []
    def revoke(model):
        if model.name == "ResearchCoordinator":
            service.allowed = False
    with pytest.raises(DomainError):
        await configured_assistant(service, model_factory=model_factory(calls, hook=revoke)).answer(
            "alice", "space", "conversation", "Observed")
    assert not any(message["role"] == "assistant" for message in service.messages)


async def test_forged_citation_is_not_accepted():
    calls = []
    forged = json.dumps({"claims": [{"text": "FAKE CLAIM", "version_ids": ["fake-private-id"]}],
                         "citations": [{"version_id": "fake-private-id", "content": "SECRET"}]})
    result = await configured_assistant(FakeService(), model_factory=model_factory(calls, content=forged)).answer(
        "alice", "space", "conversation", "Observed")
    assert result["fallback"] is True
    assert "FAKE CLAIM" not in result["answer"]
    assert "fake-private-id" not in str(result)
    assert [item["version_id"] for item in result["citations"]] == ["v1"]


async def test_unsupported_claim_is_omitted_but_valid_claim_remains():
    content = json.dumps({"claims": [{"text": "real", "version_ids": ["v1"]},
                                      {"text": "UNSUPPORTED", "version_ids": []}]})
    result = await configured_assistant(FakeService(), model_factory=model_factory([], content=content)).answer(
        "alice", "space", "conversation", "Observed")
    assert not result["fallback"]
    assert "UNSUPPORTED" not in result["answer"]
    assert "缺少有效来源" in result["answer"]


async def test_align_routes_to_own_specialist_and_preserves_two_sources():
    service, calls = FakeService(), []
    service.results = ["v1", "v2"]
    result = await configured_assistant(service, model_factory=model_factory(calls)).answer(
        "alice", "space", "conversation", "Observed", "align")
    assert [call["name"] for call in calls] == ["CollaborationAnalysisAgent", "ResearchCoordinator"]
    assert {citation["version_id"] for citation in result["citations"]} == {"v1"}  # Only the source actually cited by the coordinator.
    assert "逐来源对照" in result["answer"]


async def test_align_local_mode_is_only_excerpts_not_fake_comparison():
    result = await answer(FakeService(), "alice", "space", "conversation", "Observed", "align")
    assert "未生成差异判断或共识" in result["answer"]
    assert result["fallback"]


async def test_progress_only_reads_task_metadata_and_does_not_search():
    service = FakeService()
    result = await answer(service, "alice", "space", "conversation", "progress", "progress")
    assert "pending_acceptance" in result["answer"]
    assert "不判定" in result["answer"]
    assert "PRIVATE" not in str(result)
    assert not service.searches and not service.version_checks
    assert result["citations"] == []
    assert service.tasks[0]["status"] == "pending_acceptance"


async def test_progress_model_pipeline_uses_only_progress_and_coordinator():
    service, calls = FakeService(), []
    content = json.dumps({"claims": [{"text": "记录显示待验收，不代表完成。", "version_ids": [], "task_ids": ["t1"]}]})
    result = await configured_assistant(service, model_factory=model_factory(calls, content=content)).answer(
        "alice", "space", "conversation", "status", "progress")
    assert not result["fallback"]
    assert [call["name"] for call in calls] == ["ProgressAgent", "ResearchCoordinator"]
    assert "PRIVATE" not in str(calls)


async def test_request_agents_and_histories_are_never_reused():
    service, calls, built = FakeService(), [], []
    service.messages.append({"role": "assistant", "content": "STALE SECRET HINT", "citations": []})
    def factory(name):
        model = FakeModel(name, calls)
        built.append(model)
        return model
    assistant = configured_assistant(service, model_factory=factory)
    await assistant.answer("alice", "space", "conversation", "Observed")
    await assistant.answer("alice", "space", "conversation", "Observed")
    assert len(built) == 4 and len({id(model) for model in built}) == 4  # only routed agents are constructed
    assert "STALE SECRET HINT" not in str(calls)


async def test_read_version_only_accepts_request_discovered_ids():
    request = _Request(FakeService(), "alice", "space", "conversation")
    with pytest.raises(ValueError, match="not found"):
        await request.read_version("v1")


@pytest.mark.parametrize("question,mode", [("", "qa"), ("x" * 8001, "qa"), ("q", "approve")])
async def test_invalid_inputs_are_rejected_without_side_effects(question, mode):
    service = FakeService()
    with pytest.raises(DomainError) as exc:
        await answer(service, "alice", "space", "conversation", question, mode)
    assert exc.value.status_code == 422
    assert not service.messages and not service.searches


async def test_sdk_react_tool_call_executes_only_bound_read_tool():
    service, calls = FakeService(), []
    def factory(name):
        return FakeModel(name, calls, tool=("search_documents", {"query": "Observed"})
                         if name == "ReadOnlyRetrievalAgent" else None)
    result = await configured_assistant(service, model_factory=factory).answer(
        "alice", "space", "conversation", "Observed")
    assert not result["fallback"]
    assert len(service.searches) == 2  # initial server search + actual SDK tool call
    assert [call["name"] for call in calls] == [
        "ReadOnlyRetrievalAgent", "ReadOnlyRetrievalAgent", "ResearchCoordinator"]


async def test_react_reauthorizes_before_next_model_iteration():
    service, calls = FakeService(), []
    def hook(model):
        if model.name == "ReadOnlyRetrievalAgent" and model.count == 1:
            service.allowed = False
    def factory(name):
        return FakeModel(name, calls, hook=hook,
                         tool=("search_documents", {"query": "Observed"})
                         if name == "ReadOnlyRetrievalAgent" else None)
    with pytest.raises(DomainError):
        await configured_assistant(service, model_factory=factory).answer(
            "alice", "space", "conversation", "Observed")
    assert len(calls) == 1
    assert not any(message["role"] == "assistant" for message in service.messages)


async def test_history_is_authorized_bounded_and_forwarded_as_untrusted_data():
    service, calls = FakeService(), []
    service.messages = [
        {"role": "user", "content": "Earlier research question", "citations": []},
        {"role": "assistant", "content": "CITED EARLIER ANSWER", "citations": [{"version_id": "v1"}]},
        {"role": "assistant", "content": "UNCITED OLD BODY", "citations": []},
    ]
    result = await configured_assistant(service, model_factory=model_factory(calls)).answer(
        "alice", "space", "conversation", "follow up")
    assert not result["fallback"]
    serialized = str(calls)
    assert "CITED EARLIER ANSWER" in serialized
    assert "Earlier research question" in serialized
    assert "UNCITED OLD BODY" not in serialized
    assert service.version_checks.count("v1") >= 3


async def test_history_citation_cross_space_is_never_forwarded():
    service, calls = FakeService(), []
    service.versions["v1"]["space_id"] = "private"
    service.messages = [{"role": "assistant", "content": "SECRET", "citations": [{"version_id": "v1"}]}]
    with pytest.raises(DomainError):
        await configured_assistant(service, model_factory=model_factory(calls)).answer(
            "alice", "space", "conversation", "follow up")
    assert not calls


async def test_late_document_hit_survives_excerpt_and_fingerprint_rechecks():
    class LongDocumentService(FakeService):
        def search(self, *args, **kwargs):
            super().search(*args, **kwargs)
            return [{"version_id": "v1", "content": "TARGET LATE HIT", "snippet": "TARGET LATE HIT"}]
    service = LongDocumentService()
    service.versions["v1"]["content"] = "prefix " * 1000 + "TARGET LATE HIT" + " suffix" * 1000
    result = await answer(service, "alice", "space", "conversation", "TARGET")
    assert "TARGET LATE HIT" in result["answer"]
    assert result["citations"][0]["excerpt"] == "TARGET LATE HIT"


async def test_changes_outside_excerpt_are_detected_before_output():
    service, calls = FakeService(), []
    service.versions["v1"]["content"] += " unchanged" * 1000
    def mutate(model):
        if model.name == "ResearchCoordinator":
            service.versions["v1"]["content"] += " changed tail"
    with pytest.raises(DomainError, match="Evidence changed"):
        await configured_assistant(service, model_factory=model_factory(calls, hook=mutate)).answer(
            "alice", "space", "conversation", "Observed")
    assert not any(message["role"] == "assistant" for message in service.messages)


async def test_pending_version_is_not_usable_evidence():
    service = FakeService()
    service.versions["v1"]["index_status"] = "pending"
    with pytest.raises(DomainError, match="not ready"):
        await answer(service, "alice", "space", "conversation", "Observed")


async def test_large_question_is_accepted_but_search_query_is_bounded():
    service = FakeService()
    result = await answer(service, "alice", "space", "conversation", "q" * 8000)
    assert result["fallback"]
    assert len(service.searches[0]) == 500


async def test_model_malformed_json_degrades_to_local_not_raw_output():
    result = await configured_assistant(FakeService(), model_factory=model_factory([], content="RAW SECRET TEXT")).answer(
        "alice", "space", "conversation", "Observed")
    assert result["fallback"]
    assert "RAW SECRET TEXT" not in str(result)


async def test_current_query_replaces_same_version_history_excerpt():
    class FragmentService(FakeService):
        def search(self, actor, space_id, query, limit=5):
            super().search(actor, space_id, query, limit=limit)
            return [{"version_id": "v1", "content": query}]
    service = FragmentService()
    service.versions["v1"]["content"] = "FIRST PARAGRAPH" + " filler" * 1000 + "SECOND PARAGRAPH"
    first = await answer(service, "alice", "space", "conversation", "FIRST PARAGRAPH")
    second = await answer(service, "alice", "space", "conversation", "SECOND PARAGRAPH")
    assert first["citations"][0]["excerpt"] == "FIRST PARAGRAPH"
    assert second["citations"][0]["excerpt"] == "SECOND PARAGRAPH"
    assert "SECOND PARAGRAPH" in second["answer"]
    assert "FIRST PARAGRAPH" not in second["answer"]
    request = _Request(service, "alice", "space", "conversation")
    await request.search("FIRST PARAGRAPH")
    assert (await request.search("SECOND PARAGRAPH"))[0]["content"] == "SECOND PARAGRAPH"


async def test_full_history_budget_cannot_crowd_out_current_search_or_roll_into_citations():
    service = FakeService()
    service.messages = []
    for index in range(12):
        version_id = f"history-{index}"
        service.versions[version_id] = dict(service.versions["v1"], document_id=f"old-doc-{index}")
        service.messages.append({"role": "assistant", "content": f"old answer {index}",
                                 "citations": [{"version_id": version_id}]})
    result = await answer(service, "alice", "space", "conversation", "Observed")
    assert [citation["version_id"] for citation in result["citations"]] == ["v1"]
    assistant_messages = [message for message in service.messages if message["role"] == "assistant"]
    assert [citation["version_id"] for citation in assistant_messages[-1]["citations"]] == ["v1"]
    assert len(set(service.version_checks) - {"v1"}) == 12
    # The next turn must still discover new evidence without carrying forward
    # every old source as if it supported the new answer.
    service.results = ["v2"]
    next_result = await answer(service, "alice", "space", "conversation", "different")
    assert [citation["version_id"] for citation in next_result["citations"]] == ["v2"]


async def test_synchronous_service_call_does_not_block_async_event_loop():
    from app.application.agents.evidence import _call
    entered, release = threading.Event(), threading.Event()
    class BlockingService:
        def read(self):
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError("Event loop failed to release the synchronous call")
            return "read result"
    task = asyncio.create_task(_call(BlockingService(), "read"))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()
        assert not task.done()
    finally:
        release.set()
    assert await task == "read result"


async def test_sync_adapter_returning_awaitable_is_supported():
    from app.application.agents.evidence import _call
    class AwaitableService:
        def read(self):
            async def result():
                return "awaited"
            return result()
    assert await _call(AwaitableService(), "read") == "awaited"


async def test_async_service_adapter_is_supported():
    class AsyncService(FakeService):
        async def get_conversation(self, *args):
            return super().get_conversation(*args)
        async def get_version(self, *args):
            return super().get_version(*args)
        async def search(self, *args, **kwargs):
            return super().search(*args, **kwargs)
        async def append_message(self, *args):
            return super().append_message(*args)
    result = await answer(AsyncService(), "alice", "space", "conversation", "Observed")
    assert result["fallback"]


async def test_history_compaction_is_bounded_and_disclosed_with_provenance():
    service, calls = FakeService(), []
    service.messages = [{"role": "assistant", "content": "CITED " + "x" * 4000,
                         "citations": [{"version_id": "v1"}]} for _ in range(15)]
    result = await configured_assistant(service, model_factory=model_factory(calls)).answer(
        "alice", "space", "conversation", "Observed")
    assert "12000" in result["answer"]
    assert any(event["type"] == "history.bounded" for event in result["events"])
    request = _Request(service, "alice", "space", "conversation")
    await request.load_history()
    assert len(request.history) <= 12
    assert sum(len(message["content"]) for message in request.history) <= 12000
    assert all(message["version_ids"] for message in request.history if message["role"] == "assistant")


async def test_epoch_change_mid_model_call_is_not_a_successful_fallback():
    class EpochService(FakeService):
        epoch = 1
        def get_conversation(self, *args):
            return {**super().get_conversation(*args), "space_epoch": self.epoch}
    service, calls = EpochService(), []
    def mutate(model):
        service.epoch = 2
    with pytest.raises(DomainError, match="epoch changed"):
        await configured_assistant(service, model_factory=model_factory(calls, hook=mutate)).answer(
            "alice", "space", "conversation", "Observed")
    assert len(calls) == 1
    assert not any(message["role"] == "assistant" for message in service.messages)


async def test_runtime_timeout_does_not_become_sdk_empty_success():
    class SlowModel(FakeModel):
        async def _call_api(self, *args, **kwargs):
            await asyncio.sleep(1)
            return await super()._call_api(*args, **kwargs)
    runtime = build_runtime(ResearchAISettings(llm_min_interval_seconds=0, llm_max_retries=0,
                            model_timeout_seconds=0.01, pipeline_timeout_seconds=0.1),
                            model_factory=lambda name: SlowModel(name, []))
    result = await asyncio.wait_for(ResearchAssistant(FakeService(), runtime).answer(
        "alice", "space", "conversation", "Observed"), timeout=1)
    assert result["fallback"]
    assert "不是AI总结" in result["answer"]
