"""AgentScope adapter. All SDK types are confined to infrastructure."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import httpx

from agentscope.agent import Agent, InjectionConfig, ReActConfig
from agentscope.credential import OpenAICredential
from agentscope.message import (AssistantMsg, SystemMsg, TextBlock, ToolCallBlock,
                                ToolResultBlock, ToolResultState, UserMsg)

from app.domain.ai_failures import ModelTimeout
from app.domain.errors import DomainError
from agentscope.model import ChatModelBase, OpenAIChatModel
from agentscope.tool import FunctionTool, ToolChoice, ToolChunk, Toolkit

from app.domain.ai_ports import AgentDescription, ToolDescription
from .model_gateway import (InputBudgetExceeded, ModelGateway, RequestState,
                            ResearchAISettings, input_size)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def _pipeline(state: RequestState):
    """Convert only this scope's expiration, never caller cancellation."""
    remaining = state.deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise ModelTimeout("pipeline_timeout")
    timer = asyncio.timeout(remaining)
    try:
        async with timer:
            yield
    except TimeoutError:
        if timer.expired():
            first = (state.first_response_at - state.model_started_at
                     if state.first_response_at is not None and state.model_started_at is not None else -1.0)
            logger.warning("research.pipeline_timeout trace=%s round=%d stage=%s "
                           "queue_wait=%.6f first_response=%.6f model_elapsed=%.6f "
                           "total_elapsed=%.6f tool_elapsed=%.6f reason=pipeline_timeout",
                           state.trace_id, state.round_index, state.stage, state.queue_wait_seconds,
                           first, state.model_elapsed_seconds,
                           asyncio.get_running_loop().time() - state.started_at if state.started_at is not None else 0.0,
                           state.tool_elapsed_seconds)
            raise ModelTimeout("pipeline_timeout") from None
        raise


MAX_OUTPUT_TOKENS = 2000
MAX_PROVIDER_CHARS = 100000
# Minimum seconds between authorization revalidations inside an SSE stream.
STREAM_CHECK_SECONDS = 1.0
MAX_TOOL_CALLS = 16
TOOL_UNAVAILABLE = "Read-only tool unavailable; evidence must not be inferred."
FINAL_TURN_NOTICE = (
    "只读工具往返预算已用完，所有工具已关闭。现在必须返回最终答案JSON，不得再请求工具。"
    "仅依据本次已读取的授权证据；未读完的文档明确说明实际范围，不称全文总结。"
    "资料不足时说明具体缺失或请求澄清，不猜测、不补造事实。"
)
LAST_TOOL_TURN_NOTICE = (
    "这是最后一个可调用只读工具的轮次；如仍需取证，请在本轮完成必要读取。"
    "下一轮将关闭工具，只能基于已读资料生成最终回答并说明限制。"
)


class _CompletionModel(OpenAIChatModel):
    """The SDK otherwise discards the wire finish reason, including truncation."""

    def _parse_completion_response(self, start_datetime, response, *args, **kwargs):
        if not response.choices:
            raise ProviderStreamError("stream_missing_finish")
        choice = response.choices[0]
        finish = choice.finish_reason
        if finish not in {"stop", "tool_calls"}:
            raise ProviderStreamError({"length": "stream_output_limit",
                                       "content_filter": "stream_content_filter"}.get(
                                           finish, "stream_invalid_finish"))
        if bool(choice.message.tool_calls) != (finish == "tool_calls"):
            raise ProviderStreamError("stream_invalid_finish")
        return super()._parse_completion_response(start_datetime, response, *args, **kwargs)


class ProviderStreamError(RuntimeError):
    """Safe machine-readable stream failures, never provider body text."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _GuardedModel(ChatModelBase):
    def __init__(self, model: ChatModelBase, *, gateway: ModelGateway, name: str,
                 check: Callable, state: RequestState, fallback: Callable | None):
        super().__init__(credential=model.credential, model=model.model,
                         parameters=model.parameters, stream=False, max_retries=0,
                         context_size=model.context_size)
        self.inner, self.gateway, self.name = model, gateway, name
        self.check, self.state, self.fallback = check, state, fallback
        self.formatter = model.formatter

    async def __call__(self, messages: list, tools=None, tool_choice=None, **kwargs):
        # SDK base __call__ converts CancelledError into an empty response. The
        # gateway owns retries/timeouts, so preserve cancellation at this boundary.
        return await self._call_api(self.model, messages, tools=tools, tool_choice=tool_choice, **kwargs)

    async def _call_api(self, model_name: str, messages: list, tools=None, tool_choice=None, **kwargs):
        def provider_call(model):
            async def call(messages, **options):
                return await model._call_api(model.model, messages=messages, **options)
            return call
        fallback = (lambda: provider_call(self.fallback())) if self.fallback else None
        if self.state.stage == "tool":
            self.state.round_index += 1
            self.state.stage = "model"
        return await self.gateway.invoke(name=self.name, primary=provider_call(self.inner), fallback=fallback,
                                         messages=messages, tools=tools, tool_choice=tool_choice,
                                         check=self.check, state=self.state, **kwargs)


def _adapt_tool(description: ToolDescription, timeout: float, state: RequestState | None = None) -> FunctionTool:
    async def invoke(**kwargs: Any) -> ToolChunk:
        started = asyncio.get_running_loop().time()
        if state is not None:
            state.stage = "tool"
            if started >= state.deadline:
                raise ModelTimeout("pipeline_timeout")
        try:
            async with asyncio.timeout(min(timeout, state.deadline - started) if state else timeout):
                value = await description.invoke(**kwargs)
            if state is not None and asyncio.get_running_loop().time() >= state.deadline:
                raise ModelTimeout("pipeline_timeout")
            return ToolChunk(content=[TextBlock(text=json.dumps(value, ensure_ascii=False))],
                             state=ToolResultState.SUCCESS)
        except (DomainError, PermissionError, InputBudgetExceeded, ModelTimeout):
            raise
        except Exception:
            if state is not None and asyncio.get_running_loop().time() >= state.deadline:
                raise ModelTimeout("pipeline_timeout") from None
            # Tool-local errors remain recoverable, without private exception text.
            return ToolChunk(content=[TextBlock(text=TOOL_UNAVAILABLE)], state=ToolResultState.ERROR)
        finally:
            if state is not None:
                elapsed = asyncio.get_running_loop().time() - started
                state.tool_elapsed_seconds += elapsed
                logger.info("research.tool trace=%s round=%d stage=tool tool_elapsed=%.6f total_elapsed=%.6f",
                            state.trace_id, state.round_index, elapsed,
                            asyncio.get_running_loop().time() - state.started_at if state.started_at is not None else 0.0)

    # Explicit signatures generate strict, minimal SDK tool schemas.
    if description.name in {"search_documents", "search_sources"}:
        async def function(query: str) -> ToolChunk:
            return await invoke(query=query)
    elif description.name == "read_document":
        async def function(version_id: str, start: int = 0, length: int = 6000) -> ToolChunk:
            return await invoke(version_id=version_id, start=start, length=length)
    elif description.name in {"read_version", "read_source_version"}:
        async def function(version_id: str) -> ToolChunk:
            return await invoke(version_id=version_id)
    else:
        async def function() -> ToolChunk:
            return await invoke()
    function.__name__ = description.name
    function.__doc__ = description.description
    return FunctionTool(function, is_read_only=True)


class AgentScopeRuntime:
    def __init__(self, settings: ResearchAISettings, *, model_factory: Callable[[str], ChatModelBase] | None = None,
                 fallback_model_factory: Callable[[str], ChatModelBase] | None = None,
                 stream_factory: Callable | None = None, stream_transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.model_factory, self.fallback_model_factory = model_factory, fallback_model_factory
        self.stream_factory, self.stream_transport = stream_factory, stream_transport
        self.gateway = ModelGateway(settings)

    def new_request_state(self) -> RequestState:
        return self.gateway.new_request_state()

    def _model(self, name: str, *, fallback: bool = False) -> ChatModelBase:
        factory = self.fallback_model_factory if fallback else self.model_factory
        if factory is not None:
            return factory(name)
        credential = OpenAICredential(api_key=self.settings.llm_api_key, base_url=self.settings.llm_base_url)
        body: dict[str, Any] = {"enable_thinking": False}
        if name == "ResearchCoordinator":
            body["response_format"] = {"type": "json_object"}
        return _CompletionModel(credential=credential,
                               model=self.settings.llm_fallback_model if fallback else self.settings.llm_model,
                               parameters=OpenAIChatModel.Parameters(max_tokens=MAX_OUTPUT_TOKENS),
                               extra_body=body, stream=False, max_retries=0,
                               client_kwargs={"max_retries": 0,
                                              "timeout": self.settings.model_timeout_seconds})

    def build_agent(self, description: AgentDescription, *, check: Callable, state: RequestState) -> Agent:
        owned_models = []

        def create_model(*, fallback=False):
            model = self._model(description.name, fallback=fallback)
            factory = self.fallback_model_factory if fallback else self.model_factory
            if factory is None:
                owned_models.append(model)
            return model

        fallback = (lambda: create_model(fallback=True)) if (
            self.fallback_model_factory is not None or
            (self.model_factory is None and self.settings.llm_fallback_model and
             self.settings.llm_fallback_model != self.settings.llm_model)) else None
        guarded = _GuardedModel(create_model(), gateway=self.gateway,
                                name=description.name, check=check, state=state, fallback=fallback)
        guarded.owned_models = owned_models
        return Agent(name=description.name, system_prompt=description.instructions,
                     model=guarded,
                     toolkit=Toolkit(tools=[_adapt_tool(tool, self.settings.tool_timeout_seconds, state)
                                            for tool in description.tools]),
                     react_config=ReActConfig(max_iters=self.settings.max_tool_iterations,
                                              interruption_raise_cancelled_error=True),
                     injection_config=InjectionConfig(inject_runtime_state=False))

    async def _run(self, description: AgentDescription, payload: Mapping[str, Any], *,
                   check: Callable, state: RequestState) -> str:
        await check()
        agent = self.build_agent(description, check=check, state=state)
        try:
            state.round_index, state.stage = 0, "model"
            async with _pipeline(state):
                result = await agent.reply(UserMsg("research_input", json.dumps(payload, ensure_ascii=False)))
            await check()
            return result.get_text_content() or ""
        finally:
            # Factory-injected models belong to their caller. Only close clients
            # constructed by this runtime, including any lazy fallback clients.
            for model in agent.model.owned_models:
                await model.client.close()

    async def run_specialist(self, agent: AgentDescription, payload: Mapping[str, Any], *,
                             check: Callable, state: RequestState) -> str:
        state.round_index, state.stage = 0, "model"
        async with _pipeline(state):
            return await self._run(agent, payload, check=check, state=state)

    async def run_coordinator(self, agent: AgentDescription, payload: Mapping[str, Any], *,
                              check: Callable, state: RequestState) -> str:
        state.round_index, state.stage = 0, "model"
        async with _pipeline(state):
            return await self._run_coordinator(agent, payload, check=check, state=state)

    def _schemas(self, agent: AgentDescription) -> list[dict]:
        names = [tool.name for tool in agent.tools]
        allowed = {"list_documents", "read_document", "search_documents", "list_tasks",
                   "read_version", "read_source_version", "search_sources", "read_evidence"}
        if len(set(names)) != len(names) or not set(names) <= allowed:
            raise ValueError("Coordinator tools must be unique read-only tools")
        return [{"type": "function", "function": {"name": tool.name,
                 "description": tool.description, "parameters": _adapt_tool(
                     tool, self.settings.tool_timeout_seconds).input_schema}}
                for tool in agent.tools]

    async def _execute_calls(self, agent, calls, *, check, state):
        if not calls or len(calls) > MAX_TOOL_CALLS:
            raise ProviderStreamError("stream_invalid_tools")
        bound = {tool.name: tool for tool in agent.tools}
        results, seen = [], set()
        for call in calls:
            await check()
            remaining = state.deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ModelTimeout("pipeline_timeout")
            try:
                call_id, function = call["id"], call["function"]
                name = function["name"]
                if not isinstance(call_id, str) or not call_id or call_id in seen or name not in bound:
                    raise ValueError
                seen.add(call_id)
                arguments = json.loads(function["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError
                required = ({"query"} if name in {"search_documents", "search_sources"} else
                            {"version_id"} if name in {"read_document", "read_version", "read_source_version"} else set())
                optional = {"start", "length"} if name == "read_document" else set()
                if not required <= arguments.keys() or arguments.keys() - required - optional:
                    raise ValueError
                if any(not isinstance(arguments[key], str) for key in required):
                    raise ValueError
                if any(type(arguments[key]) is not int or arguments[key] < 0 for key in optional & arguments.keys()):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise ProviderStreamError("stream_invalid_tools") from None
            state.stage = "tool"
            started = asyncio.get_running_loop().time()
            try:
                async with asyncio.timeout(min(remaining, self.settings.tool_timeout_seconds)):
                    value = await bound[name].invoke(**arguments)
                    output = json.dumps(value, ensure_ascii=False)
            except (DomainError, PermissionError, InputBudgetExceeded, ModelTimeout):
                raise
            except Exception:
                if asyncio.get_running_loop().time() >= state.deadline:
                    raise ModelTimeout("pipeline_timeout") from None
                output = TOOL_UNAVAILABLE
            finally:
                elapsed = asyncio.get_running_loop().time() - started
                state.tool_elapsed_seconds += elapsed
                logger.info("research.tool trace=%s round=%d stage=tool tool_elapsed=%.6f total_elapsed=%.6f",
                            state.trace_id, state.round_index, elapsed,
                            asyncio.get_running_loop().time() - state.started_at if state.started_at is not None else 0.0)
            await check()
            if asyncio.get_running_loop().time() >= state.deadline:
                raise ModelTimeout("pipeline_timeout")
            results.append((call_id, name, output))
        return results

    def _final_turn_needed(self, messages, schemas, *, iteration, state, sdk=False):
        """Reserve one final request before spending another tool-capable turn.

        Input accounting is conservative for the history already available; an
        unexpectedly large new tool result still hits the gateway's hard cap.
        """
        if iteration == self.settings.max_tool_iterations:
            return True
        if not iteration or not schemas:
            return False
        scheduling = self.settings.llm_min_interval_seconds + 0.1
        if state.deadline - asyncio.get_running_loop().time() <= self.settings.model_timeout_seconds + scheduling:
            return True
        notice = (SystemMsg('tool_budget', FINAL_TURN_NOTICE) if sdk else
                  {'role': 'system', 'content': FINAL_TURN_NOTICE})
        final_size = input_size([*messages, notice], None)
        return state.budget.remaining < input_size(messages, schemas) + final_size

    @staticmethod
    def _reuse_tool_outputs(results, originals):
        """Keep the original authorized evidence in history, reference repeats.

        References use the original tool-call ID, whose full result remains in
        this request's context. Never replace the first copy or evidence IDs.
        """
        compact = []
        for call_id, name, output in results:
            original = originals.get(output)
            if original is not None:
                reference = json.dumps({'tool_output_ref': original,
                    'reuse': 'Identical result; reuse the full earlier tool output and its evidence IDs.'},
                    ensure_ascii=False)
                if len(reference) < len(output):
                    output = reference
            else:
                originals[output] = call_id
            compact.append((call_id, name, output))
        return compact

    async def _run_coordinator(self, agent, payload, *, check, state):
        await check()
        schemas = self._schemas(agent)
        messages = [SystemMsg("system", agent.instructions),
                    UserMsg("research_input", json.dumps(payload, ensure_ascii=False))]
        owned = []
        originals = {}

        def create(fallback=False):
            model = self._model(agent.name, fallback=fallback)
            factory = self.fallback_model_factory if fallback else self.model_factory
            if factory is None:
                owned.append(model)
            return model

        primary = create()
        fallback = (lambda: create(True)) if (self.fallback_model_factory is not None or
            (self.model_factory is None and self.settings.llm_fallback_model and
             self.settings.llm_fallback_model != self.settings.llm_model)) else None
        guarded = _GuardedModel(primary, gateway=self.gateway, name=agent.name,
                                check=check, state=state, fallback=fallback)
        try:
            async with _pipeline(state):
                for iteration in range(self.settings.max_tool_iterations + 1):
                    final_turn = self._final_turn_needed(messages, schemas, iteration=iteration,
                                                         state=state, sdk=True)
                    state.round_index, state.stage = iteration, "final" if final_turn else "model"
                    if final_turn:
                        messages.append(SystemMsg('tool_budget', FINAL_TURN_NOTICE))
                    elif schemas and iteration == self.settings.max_tool_iterations - 1:
                        messages.append(SystemMsg('tool_budget', LAST_TOOL_TURN_NOTICE))
                    response = await guarded(messages, tools=None if final_turn else schemas or None,
                                             **({'tool_choice': ToolChoice(mode='none')} if final_turn and schemas else {}))
                    await check()
                    finish = getattr(response, "finished_reason", None)
                    if not response.is_last or (finish is not None and
                            getattr(finish, "value", finish) != "completed"):
                        raise ProviderStreamError("stream_invalid_finish")
                    calls = [{"id": block.id, "type": "function", "function": {
                        "name": block.name, "arguments": block.input}}
                        for block in response.content if isinstance(block, ToolCallBlock)]
                    if not calls:
                        text = "".join(block.text for block in response.content if isinstance(block, TextBlock))
                        if not text:
                            raise ProviderStreamError("stream_missing_finish")
                        return text
                    if final_turn:
                        raise ProviderStreamError("stream_tool_limit")
                    messages.append(AssistantMsg(agent.name, [block for block in response.content
                                                             if isinstance(block, ToolCallBlock)]))
                    results = await self._execute_calls(agent, calls, check=check, state=state)
                    results = self._reuse_tool_outputs(results, originals)
                    messages.append(AssistantMsg(agent.name, [ToolResultBlock(
                        id=call_id, name=name, output=output, state=ToolResultState.SUCCESS)
                        for call_id, name, output in results]))
        finally:
            for model in owned:
                await model.client.close()

    async def _http_stream(self, messages: list, *, model: str, check: Callable,
                           tools: list | None = None, result: dict | None = None,
                           final_turn: bool = False, state: RequestState | None = None) -> AsyncIterator[str]:
        """OpenAI SSE, with bounded indexed tool deltas and explicit completion.

        Tool-capable turns buffer text through finish and [DONE], discarding
        prose when calls are present. This delays first text for those turns;
        tools-disabled final turns still stream immediately. Redirects are never
        followed with credentials.
        """
        if not self.settings.llm_api_key:
            raise RuntimeError("Streaming provider is not configured")
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        body = {"model": model, "messages": messages, "stream": True,
                "enable_thinking": False, "max_completion_tokens": MAX_OUTPUT_TOKENS,
                "response_format": {"type": "json_object"},
                "stream_options": {"include_usage": True}}
        if tools:
            body.update(tools=tools, parallel_tool_calls=False)
        if final_turn:
            body['tool_choice'] = 'none'
        calls: dict[int, dict] = {}
        total_chars = 0
        pending_text = []
        async with httpx.AsyncClient(transport=self.stream_transport, follow_redirects=False,
                                     timeout=self.settings.model_timeout_seconds) as client:
            async with client.stream("POST", url,
                    headers={"Authorization": "Bearer " + self.settings.llm_api_key},
                    json=body) as response:
                response.raise_for_status()
                buffer = []
                buffered_chars = 0
                stopped = False
                # Per-line authorization checks would run hundreds of expensive
                # revalidations per stream (measured ~0.2s each: 936 checks ate
                # 178s of a 182s attempt). Revalidate at a bounded cadence
                # instead; attempt start and completion still check strictly.
                last_line_check = 0.0
                async for line in response.aiter_lines():
                    now = asyncio.get_running_loop().time()
                    if now - last_line_check >= STREAM_CHECK_SECONDS:
                        last_line_check = now
                        await check()
                    if len(line) > 100000:
                        raise ValueError("Provider SSE line exceeds output limit")
                    if line:
                        if line.startswith("data:"):
                            value = line[5:].lstrip(" ")
                            buffered_chars += len(value)
                            if buffered_chars > 100000:
                                raise ValueError("Provider SSE event exceeds output limit")
                            buffer.append(value)
                        continue
                    if not buffer:
                        continue
                    data, buffer, buffered_chars = "\n".join(buffer), [], 0
                    if data.strip() == "[DONE]":
                        if not stopped:
                            raise ProviderStreamError("stream_missing_finish")
                        if result is not None:
                            result["calls"] = [calls[index] for index in sorted(calls)]
                        if not calls:
                            for fragment in pending_text:
                                yield fragment
                        return
                    event = json.loads(data)
                    usage = event.get('usage')
                    if state is not None and isinstance(usage, dict):
                        counts = {name: usage.get(name, 0) for name in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
                        if all(type(value) is int and value >= 0 for value in counts.values()):
                            logger.info('research.usage trace=%s round=%d prompt_tokens=%d completion_tokens=%d total_tokens=%d',
                                        getattr(state, 'trace_id', '?'), getattr(state, 'round_index', 0),
                                        counts['prompt_tokens'], counts['completion_tokens'], counts['total_tokens'])
                            if hasattr(state, 'prompt_tokens'):
                                state.prompt_tokens += counts['prompt_tokens']
                            if hasattr(state, 'completion_tokens'):
                                state.completion_tokens += counts['completion_tokens']
                            if hasattr(state, 'total_tokens'):
                                state.total_tokens += counts['total_tokens']
                    if "error" in event:
                        raise ProviderStreamError("stream_provider_error")
                    for choice in event.get("choices", []):
                        if choice.get("index", 0) != 0:
                            continue
                        delta = choice.get("delta", {})
                        if delta.get("tool_calls"):
                            if final_turn:
                                raise ProviderStreamError("stream_tool_limit")
                            if not tools or result is None:
                                raise ProviderStreamError("stream_unexpected_tools")
                            if stopped:
                                raise ProviderStreamError("stream_content_after_finish")
                            for part in delta["tool_calls"]:
                                # A role-only/empty keepalive is not a response;
                                # actual tool content starts the model stream.
                                if state is not None and (part.get("id") or
                                        part.get("function", {}).get("name") or
                                        part.get("function", {}).get("arguments")):
                                    state.mark_response()
                                index = part.get("index")
                                if type(index) is not int or not 0 <= index < MAX_TOOL_CALLS:
                                    raise ProviderStreamError("stream_invalid_tools")
                                call = calls.setdefault(index, {"id": "", "type": "function",
                                    "function": {"name": "", "arguments": ""}})
                                if part.get("type", "function") != "function":
                                    raise ProviderStreamError("stream_invalid_tools")
                                for target, key, value in [(call, "id", part.get("id")),
                                    (call["function"], "name", part.get("function", {}).get("name")),
                                    (call["function"], "arguments", part.get("function", {}).get("arguments"))]:
                                    if value is not None:
                                        if not isinstance(value, str):
                                            raise ProviderStreamError("stream_invalid_tools")
                                        total_chars += len(value)
                                        target[key] += value
                        finish = choice.get("finish_reason")
                        if finish is not None:
                            if finish not in {"stop", "tool_calls"} or (finish == "tool_calls" and not calls):
                                reason = {"length": "stream_output_limit", "content_filter": "stream_content_filter"}.get(
                                    finish, "stream_invalid_finish")
                                raise ProviderStreamError(reason)
                            if bool(calls) != (finish == "tool_calls") or stopped:
                                raise ProviderStreamError("stream_invalid_finish")
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            if stopped:
                                raise ProviderStreamError("stream_content_after_finish")
                            total_chars += len(content)
                            if total_chars > MAX_PROVIDER_CHARS:
                                raise ProviderStreamError("stream_output_limit")
                            if state is not None:
                                state.mark_response()
                            if tools and not final_turn:
                                pending_text.append(content)
                            else:
                                yield content
                        if total_chars > MAX_PROVIDER_CHARS:
                            raise ProviderStreamError("stream_output_limit")
                        if finish is not None:
                            stopped = True
                # Both a successful choice finish and the SSE completion marker
                # are required. A marker alone does not prove a complete answer.
                raise ProviderStreamError("stream_missing_done")

    async def stream_coordinator(self, agent: AgentDescription, payload: Mapping[str, Any], *,
                                 check: Callable, state: RequestState) -> AsyncIterator[str]:
        state.round_index, state.stage = 0, "model"
        async with _pipeline(state):
            iterator = self._stream_coordinator(agent, payload, check=check, state=state)
            try:
                async for fragment in iterator:
                    yield fragment
            finally:
                await iterator.aclose()

    async def _stream_coordinator(self, agent: AgentDescription, payload: Mapping[str, Any], *,
                                  check: Callable, state: RequestState) -> AsyncIterator[str]:
        await check()
        messages = [{"role": "system", "content": agent.instructions},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        schemas = self._schemas(agent)
        # Legacy orchestration supplies evidence out of band; preserve its
        # no-tool live-stream contract without weakening the main tool loop.
        if {tool.name for tool in agent.tools} == {"read_evidence"}:
            schemas = []
        if self.model_factory is not None and self.stream_factory is None:
            raise RuntimeError("Injected model does not support provider streaming")
        originals = {}
        for iteration in range(self.settings.max_tool_iterations + 1):
            final_turn = self._final_turn_needed(messages, schemas, iteration=iteration, state=state)
            state.round_index, state.stage = iteration, "final" if final_turn else "model"
            turn_schemas = [] if final_turn else schemas
            if final_turn:
                messages.append({'role': 'system', 'content': FINAL_TURN_NOTICE})
            elif schemas and iteration == self.settings.max_tool_iterations - 1:
                messages.append({'role': 'system', 'content': LAST_TOOL_TURN_NOTICE})
            result = {}

            def provider(model):
                async def stream(value):
                    # Gateway accounts messages; charge the schema delta on every
                    # retry/fallback as well, without changing its text-only API.
                    extra = input_size(value, turn_schemas) - input_size(value, None) if turn_schemas else 0
                    if extra > state.budget.remaining:
                        raise InputBudgetExceeded("Research total input budget exceeded")
                    if extra:
                        state.budget.charge(agent.name, extra)
                    result.clear()
                    source = (self.stream_factory(value, tools=turn_schemas, result=result) if schemas else
                              self.stream_factory(value)) if self.stream_factory else self._http_stream(
                                  value, model=model, check=check, tools=turn_schemas, result=result,
                                  final_turn=final_turn, state=state)
                    iterator = source.__aiter__()
                    pending = []
                    pending_chars = 0
                    try:
                        # No per-fragment check here: the gateway wraps this whole
                        # provider with start/completion checks plus its own bounded
                        # fragment cadence, and _http_stream revalidates per SSE line
                        # at the same bounded cadence underneath.
                        async for fragment in iterator:
                            if turn_schemas:
                                if not isinstance(fragment, str):
                                    raise ValueError("Invalid provider fragment")
                                pending_chars += len(fragment)
                                if pending_chars > MAX_PROVIDER_CHARS:
                                    raise ProviderStreamError("stream_output_limit")
                                if fragment:
                                    state.mark_response()
                                    pending.append(fragment)
                            else:
                                yield fragment
                        if turn_schemas:
                            if "calls" not in result:
                                raise ProviderStreamError("stream_missing_finish")
                            if not result["calls"]:
                                for fragment in pending:
                                    yield fragment
                    finally:
                        close = getattr(iterator, "aclose", None)
                        if close is not None:
                            await close()
                return stream

            primary = provider(self.settings.llm_model)
            fallback = (lambda: provider(self.settings.llm_fallback_model)) if (
                self.stream_factory is None and self.settings.llm_fallback_model and
                self.settings.llm_fallback_model != self.settings.llm_model) else None
            iterator = self.gateway.stream(name=agent.name, primary=primary, fallback=fallback,
                                           messages=messages, check=check, state=state)
            try:
                async for fragment in iterator:
                    yield fragment
            finally:
                await iterator.aclose()
            if schemas and "calls" not in result:
                raise ProviderStreamError("stream_missing_finish")
            calls = result.get("calls", [])
            if not calls:
                return
            if final_turn:
                raise ProviderStreamError("stream_tool_limit")
            messages.append({"role": "assistant", "content": None, "tool_calls": calls})
            results = await self._execute_calls(agent, calls, check=check, state=state)
            results = self._reuse_tool_outputs(results, originals)
            messages.extend({"role": "tool", "tool_call_id": call_id, "name": name,
                             "content": output} for call_id, name, output in results)
