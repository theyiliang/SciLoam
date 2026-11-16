"""Real SDK serialization and provider completion checks using offline transports."""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from openai import AsyncClient

from app.domain.ai_failures import ModelTimeout
from app.domain.ai_ports import AgentDescription, ToolDescription
from app.domain.errors import DomainError
from app.infrastructure.ai.agentscope_runtime import AgentScopeRuntime, MAX_OUTPUT_TOKENS, ProviderStreamError
from app.infrastructure.ai.model_gateway import ResearchAISettings


async def checked():
    pass


def settings(**changes):
    return ResearchAISettings(llm_api_key="offline-placeholder", llm_base_url="https://fake.invalid/v1",
                              llm_min_interval_seconds=0, retry_delay_seconds=0, **changes)


def completion(content="{}"):
    return {"id": "offline", "object": "chat.completion", "created": 0, "model": "offline",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}]}


def mock_sdk(monkeypatch, handler):
    """Keep the actual SDK client/serialization, replacing only its transport."""
    clients = []

    def create_client(**kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AsyncClient(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("openai.AsyncClient", create_client)
    return clients


@pytest.mark.parametrize("name,json_mode", [("ReadOnlyRetrievalAgent", False), ("ResearchCoordinator", True)])
async def test_owned_sdk_wire_policy_and_cleanup(monkeypatch, name, json_mode):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    clients = mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(llm_max_retries=0))
    result = await runtime._run(AgentDescription(name, "Return JSON." if json_mode else "Return a draft.", ()),
                                {}, check=checked, state=runtime.new_request_state())
    assert result == "{}" and len(bodies) == 1
    body = bodies[0]
    assert body["enable_thinking"] is False
    assert body["max_completion_tokens"] == MAX_OUTPUT_TOKENS and body["stream"] is False
    assert body.get("response_format") == ({"type": "json_object"} if json_mode else None)
    assert clients[0].max_retries == 0
    assert httpx.Timeout(clients[0].timeout).read == runtime.settings.model_timeout_seconds
    assert clients[0].is_closed()


async def test_actual_sdk_gateway_retries_have_no_hidden_http_retries_and_close_fallback(monkeypatch):
    routes = []

    def handler(request):
        body = json.loads(request.content)
        routes.append(body["model"])
        if body["model"] == "primary":
            return httpx.Response(429, json={"error": {"message": "offline rate limit"}})
        return httpx.Response(200, json=completion())

    clients = mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(llm_model="primary", llm_fallback_model="fallback", llm_max_retries=2))
    await runtime._run(AgentDescription("ResearchCoordinator", "Return JSON.", ()), {},
                       check=checked, state=runtime.new_request_state())
    assert routes == ["primary"] * 3 + ["fallback"]
    assert len(clients) == 2 and all(client.is_closed() for client in clients)


@pytest.mark.parametrize("failure", ["timeout", "cancel"])
async def test_owned_clients_close_on_timeout_and_cancel(monkeypatch, failure):
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()

    clients = mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(llm_max_retries=0, model_timeout_seconds=2 if failure == 'cancel' else .5))
    task = asyncio.create_task(runtime._run(AgentDescription("ReadOnlyRetrievalAgent", "Draft.", ()), {},
                                            check=checked, state=runtime.new_request_state()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
    except TimeoutError:
        if task.done():
            await task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise AssertionError('SDK request did not reach the offline transport')
    if failure == "cancel":
        task.cancel()
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else TimeoutError):
        await task
    assert clients and all(client.is_closed() for client in clients)


async def test_injected_factory_client_lifecycle_remains_caller_owned(monkeypatch):
    clients = mock_sdk(monkeypatch, lambda request: httpx.Response(200, json=completion()))
    owner = AgentScopeRuntime(settings(llm_max_retries=0))
    injected = owner._model("ReadOnlyRetrievalAgent")
    runtime = AgentScopeRuntime(settings(llm_max_retries=0), model_factory=lambda name: injected)
    try:
        await runtime._run(AgentDescription("ReadOnlyRetrievalAgent", "Draft.", ()), {},
                           check=checked, state=runtime.new_request_state())
        assert not clients[0].is_closed()
    finally:
        await injected.client.close()


def sse(event):
    return "data: " + json.dumps(event) + "\n\n"


@pytest.mark.parametrize("finish,done,reason", [
    ("stop", True, None), ("length", True, "stream_output_limit"),
    ("content_filter", True, "stream_content_filter"), ("tool_calls", True, "stream_invalid_finish"),
    (None, True, "stream_missing_finish"), ("stop", False, "stream_missing_done"),
])
async def test_stream_wire_policy_and_terminal_reason(finish, done, reason):
    event = {"choices": [{"index": 0, "delta": {"content": "{}"}, "finish_reason": finish}]}
    body = sse(event) + sse({"choices": [], "usage": {}}) + ("data: [DONE]\n\n" if done else "")
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text=body)

    runtime = AgentScopeRuntime(settings(llm_max_retries=0), stream_transport=httpx.MockTransport(handler))
    iterator = runtime._http_stream([], model="offline", check=checked)
    if reason:
        with pytest.raises(ProviderStreamError) as error:
            _ = [part async for part in iterator]
        assert error.value.reason == reason and str(error.value) == reason
    else:
        assert [part async for part in iterator] == ["{}"]
    wire = requests[0]
    assert wire["enable_thinking"] is False and wire["max_completion_tokens"] == MAX_OUTPUT_TOKENS
    assert wire["response_format"] == {"type": "json_object"} and wire["stream"] is True
    assert wire['stream_options'] == {'include_usage': True}


MAIN_JSON = json.dumps({"claims": [{"text": "Observed result", "source_kind": "material",
    "evidence_quotes": [{"evidence_id": "e1", "version_id": "v1", "text": "Observed result"}]}]})


def tool_call(name="read_document", arguments=None, call_id="read-1"):
    return {"id": call_id, "type": "function", "function": {"name": name,
            "arguments": json.dumps(arguments if arguments is not None else {"version_id": "v1"})}}


def tool_completion(calls=None, finish="tool_calls"):
    body = completion(None)
    body["choices"][0]["message"]["tool_calls"] = calls or [tool_call()]
    body["choices"][0]["finish_reason"] = finish
    return body


def tool_sse(name="read_document", arguments=None, done=True, finish="tool_calls"):
    call = tool_call(name, arguments)
    raw = call["function"]["arguments"]
    return (sse({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
        "id": call["id"], "type": "function", "function": {"name": name, "arguments": raw[:8]}}]}}]}) +
        sse({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
            "function": {"arguments": raw[8:]}}]}, "finish_reason": finish}]}) +
        ("data: [DONE]\n\n" if done else ""))


def final_sse(text=MAIN_JSON):
    return sse({"choices": [{"index": 0, "delta": {"content": text},
                            "finish_reason": "stop"}]}) + "data: [DONE]\n\n"


def main_agent(invoke):
    return AgentDescription("ResearchCoordinator", "Read evidence then return claims JSON.", (
        ToolDescription("list_documents", "List request documents.", invoke),
        ToolDescription("read_document", "Read a document window.", invoke),
        ToolDescription("search_documents", "Search request documents.", invoke),
        ToolDescription("list_tasks", "List request tasks.", invoke),
    ))


async def collect(runtime, agent, *, check=checked, state=None):
    return "".join([text async for text in runtime.stream_coordinator(
        agent, {"question": "Observed?"}, check=check, state=state or runtime.new_request_state())])


@pytest.mark.parametrize("streaming", [False, True])
async def test_main_tool_loop_wire_schema_results_and_one_final(monkeypatch, streaming):
    bodies, invoked = [], []

    async def invoke(**arguments):
        invoked.append(arguments)
        return {"evidence_id": "e1", "version_id": "v1", "text": "Observed result"}

    def handler(request):
        bodies.append(json.loads(request.content))
        if streaming:
            return httpx.Response(200, text=tool_sse(arguments={"version_id": "v1", "start": 0, "length": 6000})
                                  if len(bodies) == 1 else final_sse())
        return httpx.Response(200, json=tool_completion(calls=[tool_call(arguments={
            "version_id": "v1", "start": 0, "length": 6000})]) if len(bodies) == 1 else completion(MAIN_JSON))

    clients = mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(llm_max_retries=0), stream_transport=httpx.MockTransport(handler))
    agent, state = main_agent(invoke), runtime.new_request_state()
    result = (await collect(runtime, agent, state=state) if streaming else
              await runtime.run_coordinator(agent, {}, check=checked, state=state))
    assert result == MAIN_JSON and len(bodies) == 2
    assert invoked == [{"version_id": "v1", "start": 0, "length": 6000}]
    functions = {tool["function"]["name"]: tool["function"] for tool in bodies[0]["tools"]}
    assert set(functions) == {"list_documents", "read_document", "search_documents", "list_tasks"}
    assert functions["list_documents"]["parameters"].get("properties", {}) == {}
    read = functions["read_document"]["parameters"]
    assert set(read["properties"]) == {"version_id", "start", "length"}
    assert read["required"] == ["version_id"]
    assert read["properties"]["length"]["default"] == 6000
    tool_result = next(message for message in bodies[1]["messages"] if message["role"] == "tool")
    assert tool_result["tool_call_id"] == "read-1" and "Observed result" in tool_result["content"]
    assert state.budget.used > len(json.dumps(bodies[0]["messages"]))
    assert all(client.is_closed() for client in clients)


@pytest.mark.parametrize("streaming", [False, True])
async def test_tool_budget_reserves_one_final_turn_with_tools_disabled(monkeypatch, streaming):
    bodies, calls = [], []

    async def invoke(**arguments):
        calls.append(arguments)
        return []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        final = len(bodies) == 3
        if final:
            assert not body.get('tools') and body['tool_choice'] == 'none'
            assert '只读工具往返预算已用完' in str(body['messages'][-1]['content'])
        elif len(bodies) == 2:
            assert any('最后一个可调用' in str(m.get('content')) for m in body['messages'])
        if streaming:
            return httpx.Response(200, text=final_sse() if final else tool_sse())
        return httpx.Response(200, json=completion(MAIN_JSON) if final else tool_completion())

    mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(llm_max_retries=0, max_tool_iterations=2),
                                stream_transport=httpx.MockTransport(handler))
    result = (await collect(runtime, main_agent(invoke)) if streaming else
              await runtime.run_coordinator(main_agent(invoke), {}, check=checked, state=runtime.new_request_state()))
    assert result == MAIN_JSON
    assert len(bodies) == 3 and len(calls) == 2


@pytest.mark.parametrize('streaming', [False, True])
async def test_final_turn_cannot_execute_more_tools(monkeypatch, streaming):
    bodies, calls = [], []
    async def invoke(**arguments):
        calls.append(arguments)
        return []
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text=tool_sse()) if streaming else httpx.Response(200, json=tool_completion())
    mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(llm_max_retries=0, max_tool_iterations=1),
                                stream_transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderStreamError):
        if streaming:
            await collect(runtime, main_agent(invoke))
        else:
            await runtime.run_coordinator(main_agent(invoke), {}, check=checked, state=runtime.new_request_state())
    assert len(bodies) == 2 and len(calls) == 1


@pytest.mark.parametrize('streaming', [False, True])
async def test_long_document_three_tool_rounds_then_persisted_answer(monkeypatch, tmp_path, streaming):
    import secrets
    from app.application.agents.orchestrator import ResearchAssistant
    from app.infrastructure.persistence.factory import build_research_service
    service = build_research_service(tmp_path / 'research.db')
    actor = service.bootstrap('owner@example.org', secrets.token_urlsafe(24), 'Synthetic')['user']['id']
    space = next(row['id'] for row in service.list_spaces(actor) if row['kind'] == 'basis')
    conversation = service.create_conversation(actor, space)['id']
    body = '甲' * 6000 + '乙' * 179
    key = service.add_document(actor, space, '分段材料', body)['latest_version']['id']
    requests = []
    def handler(request):
        wire = json.loads(request.content)
        requests.append(wire)
        turn = len(requests)
        if turn <= 3:
            name = 'list_documents' if turn == 1 else 'read_document'
            args = {} if turn == 1 else {'version_id': key, 'start': 0 if turn == 2 else 6000}
            if streaming:
                return httpx.Response(200, text=tool_sse(name, args))
            return httpx.Response(200, json=tool_completion(calls=[tool_call(name, args)]))
        assert turn == 4 and not wire.get('tools') and wire['tool_choice'] == 'none'
        text = json.dumps({'answer_status': 'sufficient', 'claims': [{
            'source_kind': 'material', 'text': '已读取全文6179字符，末段为乙。', 'version_ids': [key],
            'evidence_quotes': [{'evidence_id': 'e2', 'version_id': key, 'text': '乙' * 179}]}]}, ensure_ascii=False)
        return httpx.Response(200, text=final_sse(text)) if streaming else httpx.Response(200, json=completion(text))
    mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(llm_max_retries=0), stream_transport=httpx.MockTransport(handler))
    events = []
    async def sink(event):
        events.append(event)
    result = await ResearchAssistant(service, runtime).answer(actor, space, conversation, '这个文档说了啥',
                                                            event_sink=sink if streaming else None)
    assert len(requests) == 4 and not result['fallback']
    assert '6179' in result['answer'] and '不能视为全文总结' not in result['answer']
    assert result['citations'][0]['chunk_start'] == 6000
    assert service.get_conversation(actor, conversation)['messages'][-1]['content'] == result['answer']
    if streaming:
        # Orchestrator no longer emits answer.delta; streaming still emits agent.started.
        assert any(event['type'] == 'agent.started' for event in events)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("name,arguments", [
    ("delete_document", {}), ("read_document", {"version_id": "v1", "actor": "other"}),
    ("list_documents", {"space": "other"}), ("read_document", {"version_id": "v1", "start": True}),
    ("read_document", {"version_id": "v1", "length": -1}),
])
async def test_main_rejects_unbound_or_scope_arguments(monkeypatch, streaming, name, arguments):
    calls = []

    async def invoke(**values):
        calls.append(values)

    def handler(request):
        return (httpx.Response(200, text=tool_sse(name, arguments)) if streaming else
                httpx.Response(200, json=tool_completion([tool_call(name, arguments)])))

    mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(llm_max_retries=0), stream_transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderStreamError, match="stream_invalid_tools"):
        if streaming:
            await collect(runtime, main_agent(invoke))
        else:
            await runtime.run_coordinator(main_agent(invoke), {}, check=checked, state=runtime.new_request_state())
    assert not calls


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("finish", ["length", "content_filter"])
async def test_incomplete_main_completion_never_final(monkeypatch, streaming, finish):
    async def invoke(**arguments):
        raise AssertionError("Incomplete tool call must never execute")

    def handler(request):
        if streaming:
            return httpx.Response(200, text=tool_sse(finish=finish))
        return httpx.Response(200, json=tool_completion(finish=finish))

    mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(llm_max_retries=0), stream_transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderStreamError):
        if streaming:
            await collect(runtime, main_agent(invoke))
        else:
            await runtime.run_coordinator(main_agent(invoke), {}, check=checked, state=runtime.new_request_state())


async def test_stream_tool_call_requires_done_before_execution():
    calls = []

    async def invoke(**arguments):
        calls.append(arguments)

    runtime = AgentScopeRuntime(settings(llm_max_retries=0), stream_transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=tool_sse(done=False))))
    with pytest.raises(ProviderStreamError, match="stream_missing_done"):
        await collect(runtime, main_agent(invoke))
    assert not calls


@pytest.mark.parametrize("streaming", [False, True])
async def test_main_revocation_after_tool_blocks_next_model(monkeypatch, streaming):
    allowed, bodies = True, []

    async def check():
        if not allowed:
            raise DomainError(403, "revoked")

    async def invoke(**arguments):
        nonlocal allowed
        allowed = False
        return {"private": "must not be forwarded"}

    def handler(request):
        bodies.append(request)
        return httpx.Response(200, text=tool_sse()) if streaming else httpx.Response(200, json=tool_completion())

    mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(llm_max_retries=0), stream_transport=httpx.MockTransport(handler))
    with pytest.raises(DomainError, match="revoked"):
        if streaming:
            await collect(runtime, main_agent(invoke), check=check)
        else:
            await runtime.run_coordinator(main_agent(invoke), {}, check=check, state=runtime.new_request_state())
    assert len(bodies) == 1


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure", ["cancel", "deadline", "tool_timeout"])
async def test_main_tool_cancellation_and_deadlines(monkeypatch, streaming, failure):
    entered, bodies = asyncio.Event(), []

    async def invoke(**arguments):
        entered.set()
        await asyncio.Event().wait()

    def handler(request):
        bodies.append(json.loads(request.content))
        if streaming:
            return httpx.Response(200, text=tool_sse() if len(bodies) == 1 else final_sse())
        return httpx.Response(200, json=tool_completion() if len(bodies) == 1 else completion(MAIN_JSON))

    mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(llm_max_retries=0,
        tool_timeout_seconds=.02 if failure == "tool_timeout" else 5), stream_transport=httpx.MockTransport(handler))
    state = runtime.new_request_state()
    if failure == "deadline":
        state.deadline = asyncio.get_running_loop().time() + .5
    operation = (collect(runtime, main_agent(invoke), state=state) if streaming else
                 runtime.run_coordinator(main_agent(invoke), {}, check=checked, state=state))
    task = asyncio.create_task(operation)
    await asyncio.wait_for(entered.wait(), 3)
    if failure == "cancel":
        task.cancel()
    if failure == "tool_timeout":
        assert await task == MAIN_JSON
        tool_result = next(message for message in bodies[1]["messages"] if message["role"] == "tool")
        assert "unavailable" in tool_result["content"]
    else:
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else TimeoutError):
            await task
        assert len(bodies) == 1


@pytest.mark.parametrize("streaming", [False, True])
async def test_main_budget_includes_schemas_and_stops_before_second_request(monkeypatch, streaming):
    from app.infrastructure.ai.model_gateway import InputBudgetExceeded
    bodies = []

    async def invoke(**arguments):
        return {"text": "x" * 12000}

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text=tool_sse()) if streaming else httpx.Response(200, json=tool_completion())

    mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(llm_max_retries=0, total_input_chars=6000),
                                stream_transport=httpx.MockTransport(handler))
    state = runtime.new_request_state()
    with pytest.raises(InputBudgetExceeded):
        if streaming:
            await collect(runtime, main_agent(invoke), state=state)
        else:
            await runtime.run_coordinator(main_agent(invoke), {}, check=checked, state=state)
    assert len(bodies) == 1 and state.budget.used > 500


async def test_stream_reauthorizes_every_provider_line_and_closes_on_revocation():
    seen, calls = 0, []

    async def invoke(**arguments):
        calls.append(arguments)

    async def check():
        nonlocal seen
        seen += 1
        if seen == 5:
            raise DomainError(403, "revoked during stream")

    runtime = AgentScopeRuntime(settings(llm_max_retries=0), stream_transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=tool_sse())))
    with pytest.raises(DomainError, match="revoked during stream"):
        await collect(runtime, main_agent(invoke), check=check)
    assert seen == 5 and not calls


@pytest.mark.parametrize('position', ['before', 'after', 'same_delta'])
@pytest.mark.parametrize('streaming', [False, True])
async def test_mixed_tool_text_is_private_and_final_answer_is_clean(monkeypatch, position, streaming):
    bodies, calls = [], []

    async def invoke(**arguments):
        calls.append(arguments)
        return []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) > 1:
            assert 'not the answer' not in json.dumps(bodies[-1]['messages'])
            return httpx.Response(200, text=final_sse()) if streaming else httpx.Response(200, json=completion(MAIN_JSON))
        if not streaming:
            body = tool_completion()
            body['choices'][0]['message']['content'] = 'not the answer'
            return httpx.Response(200, json=body)
        prose = sse({'choices': [{'delta': {'content': 'not the answer'}}]})
        if position == 'same_delta':
            body = sse({'choices': [{'delta': {'content': 'not the answer', 'tool_calls': [
                {'index': 0, **tool_call()}]}, 'finish_reason': 'tool_calls'}]}) + 'data: [DONE]\n\n'
        elif position == 'before':
            body = prose + tool_sse()
        else:
            call = tool_call()
            body = (sse({'choices': [{'delta': {'tool_calls': [{'index': 0, **call}]}}]}) +
                    prose + sse({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]}) + 'data: [DONE]\n\n')
        return httpx.Response(200, text=body)

    mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(llm_max_retries=3), stream_transport=httpx.MockTransport(handler))
    result = (await collect(runtime, main_agent(invoke)) if streaming else
              await runtime.run_coordinator(main_agent(invoke), {}, check=checked, state=runtime.new_request_state()))
    assert result == MAIN_JSON
    assert len(bodies) == 2 and len(calls) == 1


@pytest.mark.parametrize('done,finish,reason', [
    (False, 'tool_calls', 'stream_missing_done'),
    (True, None, 'stream_missing_finish'),
])
async def test_mixed_tool_text_requires_completion_without_leaking_prose(done, finish, reason):
    calls, fragments = [], []

    async def invoke(**kwargs):
        calls.append(kwargs)

    prose = sse({'choices': [{'delta': {'content': 'private tool-round prose'}}]})
    runtime = AgentScopeRuntime(settings(llm_max_retries=0), stream_transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=prose + tool_sse(done=done, finish=finish))))
    with pytest.raises(ProviderStreamError, match=reason):
        async for fragment in runtime.stream_coordinator(main_agent(invoke), {}, check=checked,
                                                         state=runtime.new_request_state()):
            fragments.append(fragment)
    assert not calls and not fragments


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize('forced_final', [False, True])
async def test_main_stream_buffers_tool_capable_turn_but_forced_final_streams_early(complete, forced_final):
    release, closed = asyncio.Event(), asyncio.Event()
    bodies = []
    claim_prefix = MAIN_JSON[:-1]

    class PausedMainSSE(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse({"choices": [{"index": 0, "delta": {"content": claim_prefix}}]}).encode()
            await release.wait()
            if complete:
                yield final_sse("}").encode()

        async def aclose(self):
            closed.set()

    async def invoke(**arguments):
        return {"evidence_id": "e1", "version_id": "v1", "text": "Observed result"}

    def handler(request):
        bodies.append(json.loads(request.content))
        return (httpx.Response(200, text=tool_sse()) if len(bodies) == 1 else
                httpx.Response(200, stream=PausedMainSSE()))

    runtime = AgentScopeRuntime(settings(llm_max_retries=0, max_tool_iterations=1 if forced_final else 3),
                                stream_transport=httpx.MockTransport(handler))
    iterator = runtime.stream_coordinator(main_agent(invoke), {}, check=checked, state=runtime.new_request_state())
    pending = asyncio.create_task(anext(iterator))
    try:
        if forced_final:
            assert await asyncio.wait_for(asyncio.shield(pending), 3) == claim_prefix
            assert not release.is_set() and not closed.is_set()
        else:
            # Until completion, the same prefix could still become tool prose.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), .03)
            assert not closed.is_set()
        release.set()
        if complete:
            if not forced_final:
                assert await pending == claim_prefix
            assert await anext(iterator) == "}"
            with pytest.raises(StopAsyncIteration):
                await anext(iterator)
        else:
            with pytest.raises(ProviderStreamError, match="stream_missing_done"):
                if forced_final:
                    await anext(iterator)
                else:
                    await pending
        assert len(bodies) == 2 and closed.is_set()
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await iterator.aclose()


@pytest.mark.parametrize("kind,reason", [
    ("no_content", "model_first_response_timeout"),
    ("role_only", "model_first_response_timeout"),
    ("tool_delta", "model_stream_timeout"),
    ("text", "model_stream_timeout"),
])
async def test_live_sse_timeout_distinguishes_real_content(kind, reason):
    bodies, closed = [], []

    class PausedSSE(httpx.AsyncByteStream):
        async def __aiter__(self):
            if kind == "role_only":
                yield sse({"choices": [{"delta": {"role": "assistant", "content": ""}}]}).encode()
            elif kind == "tool_delta":
                yield sse({"choices": [{"delta": {"tool_calls": [{"index": 0,
                    "id": "read-1", "function": {"name": "read_document", "arguments": "{"}}]}}]}).encode()
            elif kind == "text":
                yield sse({"choices": [{"delta": {"content": "PRIVATE FRAGMENT"}}]}).encode()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    def handler(request):
        bodies.append(request)
        return httpx.Response(200, stream=PausedSSE())

    runtime = AgentScopeRuntime(settings(model_timeout_seconds=.03, llm_max_retries=0),
                                stream_transport=httpx.MockTransport(handler))
    state = runtime.new_request_state()
    with pytest.raises(ModelTimeout) as error:
        await collect(runtime, main_agent(checked), state=state)
    assert error.value.reason == reason and len(bodies) == 1 and closed == [True]
    assert state.stream_started == (kind in {"tool_delta", "text"})


@pytest.mark.parametrize("streaming", [False, True])
async def test_final_generation_timeout_is_explicit(monkeypatch, streaming):
    bodies = []

    async def invoke(**kwargs):
        return []

    async def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(200, text=tool_sse()) if streaming else httpx.Response(200, json=tool_completion())
        await asyncio.Event().wait()

    mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(model_timeout_seconds=.1, llm_max_retries=0, max_tool_iterations=1),
                                stream_transport=httpx.MockTransport(handler))
    state = runtime.new_request_state()
    with pytest.raises(ModelTimeout) as error:
        if streaming:
            await collect(runtime, main_agent(invoke), state=state)
        else:
            await runtime.run_coordinator(main_agent(invoke), {}, check=checked, state=state)
    assert error.value.reason == "model_final_timeout"
    assert state.round_index == 1 and state.stage == "final" and len(bodies) == 2


@pytest.mark.parametrize("streaming", [False, True])
async def test_outer_pipeline_timeout_during_tool_is_not_tool_unavailable(monkeypatch, streaming, caplog):
    caplog.set_level(logging.INFO)
    bodies = []

    async def invoke(**kwargs):
        await asyncio.Event().wait()

    def handler(request):
        bodies.append(request)
        return httpx.Response(200, text=tool_sse()) if streaming else httpx.Response(200, json=tool_completion())

    mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(pipeline_timeout_seconds=.3, model_timeout_seconds=1,
                                tool_timeout_seconds=1, llm_max_retries=0),
                                stream_transport=httpx.MockTransport(handler))
    state = runtime.new_request_state()
    with pytest.raises(ModelTimeout) as error:
        if streaming:
            await collect(runtime, main_agent(invoke), state=state)
        else:
            await runtime.run_coordinator(main_agent(invoke), {}, check=checked, state=state)
    assert error.value.reason == "pipeline_timeout" and len(bodies) == 1
    assert state.stage == "tool" and state.tool_elapsed_seconds > 0
    assert "stage=tool" in caplog.text and "reason=pipeline_timeout" in caplog.text
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("streaming", [False, True])
async def test_outer_pipeline_timeout_during_initial_authorization(streaming):
    runtime = AgentScopeRuntime(settings(pipeline_timeout_seconds=.02, llm_max_retries=0),
                                stream_factory=lambda messages: None)

    async def blocked_check():
        await asyncio.Event().wait()

    with pytest.raises(ModelTimeout, match="pipeline_timeout"):
        if streaming:
            await collect(runtime, main_agent(checked), check=blocked_check)
        else:
            await runtime.run_coordinator(main_agent(checked), {}, check=blocked_check,
                                          state=runtime.new_request_state())


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('reserve', ['time', 'input'])
async def test_remaining_budget_transitions_to_final_before_another_tool_round(monkeypatch, streaming, reserve):
    bodies, calls = [], []
    state = None

    async def invoke(**kwargs):
        calls.append(kwargs)
        if reserve == 'time':
            state.deadline = asyncio.get_running_loop().time() + .5
        else:
            # Enough for a final request, not another schema-bearing round plus final.
            state.budget.total_limit = state.budget.used + (1500 if streaming else 3000)
        return {'text': 'Authorized evidence'}

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(200, text=tool_sse()) if streaming else httpx.Response(200, json=tool_completion())
        assert not body.get('tools') and body['tool_choice'] == 'none'
        return httpx.Response(200, text=final_sse()) if streaming else httpx.Response(200, json=completion(MAIN_JSON))

    mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(llm_max_retries=0, model_timeout_seconds=1),
                                stream_transport=httpx.MockTransport(handler))
    state = runtime.new_request_state()
    result = (await collect(runtime, main_agent(invoke), state=state) if streaming else
              await runtime.run_coordinator(main_agent(invoke), {}, check=checked, state=state))
    assert result == MAIN_JSON and len(calls) == 1 and len(bodies) == 2
    assert state.stage == 'final' and state.round_index == 1
    assert state.budget.used <= state.budget.total_limit


@pytest.mark.parametrize('streaming', [False, True])
async def test_duplicate_tool_outputs_reference_original_evidence_without_repeating_body(monkeypatch, streaming):
    bodies = []
    evidence = {'evidence_id': 'e1', 'version_id': 'v1', 'text': 'original evidence ' * 100}

    async def invoke(**kwargs):
        return evidence

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) < 3:
            if streaming:
                return httpx.Response(200, text=tool_sse())
            return httpx.Response(200, json=tool_completion())
        results = [message['content'] for message in body['messages'] if message['role'] == 'tool']
        assert len(results) == 2
        assert json.loads(results[0]) == evidence
        reference = json.loads(results[1])
        assert reference['tool_output_ref'] == 'read-1'
        assert len(results[1]) < len(results[0])
        return httpx.Response(200, text=final_sse()) if streaming else httpx.Response(200, json=completion(MAIN_JSON))

    mock_sdk(monkeypatch, handler)
    runtime = AgentScopeRuntime(settings(llm_max_retries=0, max_tool_iterations=2),
                                stream_transport=httpx.MockTransport(handler))
    result = (await collect(runtime, main_agent(invoke)) if streaming else
              await runtime.run_coordinator(main_agent(invoke), {}, check=checked, state=runtime.new_request_state()))
    assert result == MAIN_JSON and len(bodies) == 3


def test_composition_run_deadline_uses_same_pipeline_setting(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app import composition
    configured = settings(pipeline_timeout_seconds=271)
    monkeypatch.setattr(composition.ResearchAISettings, "from_env", lambda: configured)
    monkeypatch.setattr(composition, "load_retrieval_settings", lambda: SimpleNamespace(max_candidates=5))
    monkeypatch.setattr(composition, "build_retrieval", lambda value: (None, None, None))
    monkeypatch.setattr(composition, "build_research_service", lambda *args, **kwargs: object())
    monkeypatch.setattr(composition, "SQLiteResearchEngineering", lambda path: object())
    seen = []

    def assistant(service, *, settings, security_recorder=None):
        seen.append(settings)
        return object()

    monkeypatch.setattr(composition, "build_assistant", assistant)
    container = composition.build_container(tmp_path / "unused.sqlite")
    assert seen == [configured]
    assert container.runs.execution_timeout_seconds == 301
    assert not (tmp_path / "unused.sqlite").exists()
