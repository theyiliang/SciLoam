"""Offline gateway and DDD-boundary regressions; no provider clients required."""
from __future__ import annotations

import asyncio
import ast
import logging
from pathlib import Path

import pytest

from app.domain.ai_failures import ModelTimeout
from app.domain.errors import DomainError
from app.infrastructure.ai.factory import build_assistant, build_runtime
from app.infrastructure.ai.model_gateway import (
    InputBudgetExceeded, ModelGateway, ResearchAISettings, input_size,
)


async def checked():
    pass


class Model:
    def __init__(self, error=None, delay=0):
        self.error, self.delay, self.calls = error, delay, 0

    async def __call__(self, messages, **kwargs):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return "ok"


def gateway(**kwargs):
    return ModelGateway(ResearchAISettings(llm_min_interval_seconds=0, retry_delay_seconds=0, **kwargs))


async def invoke(gate, model, *, state=None, fallback=None, check=checked, messages=None):
    return await gate.invoke(name="ReadOnlyRetrievalAgent", primary=model, fallback=fallback,
                             messages=messages or [{"content": "PRIVATE RESEARCH QUESTION"}],
                             tools=[], tool_choice=None, check=check,
                             state=state or gate.new_request_state())


async def test_transient_retry_is_finite_then_uses_fallback():
    gate = gateway(llm_max_retries=2)
    primary, fallback = Model(RuntimeError("429 PRIVATE PROVIDER BODY")), Model()
    checks = []

    async def check():
        checks.append(True)

    assert await invoke(gate, primary, fallback=lambda: fallback, check=check) == "ok"
    assert primary.calls == 3 and fallback.calls == 1
    assert len(checks) == 4


async def test_both_models_fail_with_a_finite_total_attempt_count():
    gate = gateway(llm_max_retries=1)
    primary, fallback = Model(RuntimeError("503 service unavailable")), Model(RuntimeError("rate limit"))
    with pytest.raises(RuntimeError):
        await invoke(gate, primary, fallback=lambda: fallback)
    assert primary.calls == fallback.calls == 2


async def test_nontransient_error_never_retries_or_switches_models():
    gate = gateway(llm_max_retries=3)
    primary, fallback = Model(ValueError("invalid request")), Model()
    with pytest.raises(ValueError):
        await invoke(gate, primary, fallback=lambda: fallback)
    assert primary.calls == 1 and fallback.calls == 0


async def test_revocation_before_retry_or_fallback_never_reaches_model():
    gate = gateway(llm_max_retries=0)
    primary, fallback = Model(RuntimeError("rate limit")), Model()
    count = 0

    async def check():
        nonlocal count
        count += 1
        if count > 1:
            raise DomainError(403, "revoked")

    with pytest.raises(DomainError, match="revoked"):
        await invoke(gate, primary, fallback=lambda: fallback, check=check)
    assert primary.calls == 1 and fallback.calls == 0


async def test_input_budget_is_cumulative_and_counts_retries():
    messages = [{"content": "private"}]
    size = input_size(messages, [])
    gate = gateway(total_input_chars=size * 2, llm_max_retries=3)
    state, model = gate.new_request_state(), Model(RuntimeError("rate limit"))
    with pytest.raises(InputBudgetExceeded):
        await invoke(gate, model, state=state, messages=messages)
    assert model.calls == 2
    assert state.budget.used == size * 2
    with pytest.raises(InputBudgetExceeded):
        await invoke(gate, Model(), state=state, messages=messages)


async def test_input_budget_refuses_oversized_first_call():
    gate, model = gateway(total_input_chars=1), Model()
    with pytest.raises(InputBudgetExceeded):
        await invoke(gate, model)
    assert model.calls == 0


async def test_model_timeout_is_bounded_and_cancellation_propagates():
    gate, model = gateway(model_timeout_seconds=0.01, llm_max_retries=0), Model(delay=1)
    with pytest.raises(TimeoutError):
        await invoke(gate, model)
    task = asyncio.create_task(invoke(gateway(), Model(delay=1)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_pipeline_deadline_covers_queue_and_retry_budget():
    gate, model = gateway(pipeline_timeout_seconds=0.02, model_timeout_seconds=1, llm_max_retries=3), Model(delay=1)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(invoke(gate, model), timeout=0.2)
    assert 1 <= model.calls <= 4  # platform timers may fire slightly before the deadline


async def test_shared_runtime_gateway_limits_concurrency():
    gate = gateway(llm_max_concurrency=2)
    active = peak = 0

    class ConcurrentModel:
        async def __call__(self, messages, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                return "ok"
            finally:
                active -= 1

    await asyncio.gather(*(invoke(gate, ConcurrentModel()) for _ in range(8)))
    assert peak == 2


async def test_logs_have_trace_metadata_without_bodies_or_provider_error(caplog):
    caplog.set_level(logging.INFO, logger="app.infrastructure.ai.model_gateway")
    with pytest.raises(RuntimeError):
        await invoke(gateway(llm_max_retries=0), Model(RuntimeError("429 PRIVATE PROVIDER BODY")))
    assert "trace=" in caplog.text and "input_chars=" in caplog.text
    assert "PRIVATE" not in caplog.text


async def test_http_failure_logs_status_without_response_body(caplog):
    import httpx
    request = httpx.Request('POST', 'https://example.invalid/chat/completions')
    response = httpx.Response(400, request=request, text='PRIVATE_PROVIDER_BODY')
    error = httpx.HTTPStatusError('PRIVATE_ERROR_TEXT', request=request, response=response)
    with pytest.raises(httpx.HTTPStatusError):
        await invoke(gateway(llm_max_retries=0), Model(error))
    assert 'status_code=400' in caplog.text
    assert 'PRIVATE' not in caplog.text


def test_timeout_interface_is_safe_and_backwards_compatible():
    for reason in ModelTimeout.REASONS:
        error = ModelTimeout(reason)
        assert isinstance(error, TimeoutError) and error.reason == str(error) == reason
    with pytest.raises(ValueError, match="Invalid model timeout reason"):
        ModelTimeout("PRIVATE PROVIDER BODY")
    assert ResearchAISettings().model_timeout_seconds == 45


@pytest.mark.parametrize("stage,reason", [("model", "model_request_timeout"), ("final", "model_final_timeout")])
async def test_sync_model_timeout_has_safe_phase_reason(stage, reason):
    gate = gateway(model_timeout_seconds=.02, llm_max_retries=0)
    state = gate.new_request_state()
    state.stage, state.round_index = stage, 2
    with pytest.raises(ModelTimeout) as error:
        await invoke(gate, Model(delay=1), state=state)
    assert error.value.reason == reason
    assert state.model_elapsed_seconds > 0 and state.first_response_at is None


@pytest.mark.parametrize("content,stage,reason", [
    (None, "model", "model_first_response_timeout"),
    ("", "model", "model_first_response_timeout"),
    ("PRIVATE FRAGMENT", "model", "model_stream_timeout"),
    (None, "final", "model_final_timeout"),
    ("PRIVATE FRAGMENT", "final", "model_final_timeout"),
])
async def test_stream_timeout_phase_and_first_content(content, stage, reason):
    gate = gateway(model_timeout_seconds=.02, llm_max_retries=0)
    state, closed = gate.new_request_state(), []
    state.stage = stage

    async def provider(messages):
        try:
            if content is not None:
                yield content
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    parts = []
    with pytest.raises(ModelTimeout) as error:
        async for part in gate.stream(name="ResearchCoordinator", primary=provider, fallback=None,
                                      messages=[], check=checked, state=state):
            parts.append(part)
    assert error.value.reason == reason
    assert parts == ([content] if content else []) and closed == [True]
    assert (state.first_response_at is not None) == bool(content)


@pytest.mark.parametrize("streaming", [False, True])
async def test_pipeline_timeout_does_not_retry_or_construct_fallback(streaming):
    gate = gateway(model_timeout_seconds=1, pipeline_timeout_seconds=.02, llm_max_retries=3)
    state, factories, calls = gate.new_request_state(), [], []

    def fallback():
        factories.append(True)
        return Model()

    async def provider(messages):
        calls.append(True)
        await asyncio.Event().wait()
        yield "never"

    with pytest.raises(ModelTimeout) as error:
        if streaming:
            _ = [part async for part in gate.stream(name="ResearchCoordinator", primary=provider,
                    fallback=fallback, messages=[], check=checked, state=state)]
        else:
            await invoke(gate, Model(delay=1), state=state, fallback=fallback)
    assert error.value.reason == "pipeline_timeout" and not factories
    if streaming:
        assert len(calls) == 1


@pytest.mark.parametrize("retries", [0, 3])
async def test_retry_or_fallback_needs_full_attempt_remaining(retries):
    gate = gateway(model_timeout_seconds=.05, pipeline_timeout_seconds=.08, llm_max_retries=retries)
    primary, fallback, factories = Model(delay=1), Model(), []

    def make_fallback():
        factories.append(True)
        return fallback

    with pytest.raises(ModelTimeout) as error:
        await invoke(gate, primary, fallback=make_fallback)
    assert error.value.reason == "pipeline_timeout"
    assert primary.calls == 1 and fallback.calls == 0 and not factories


async def test_expired_request_never_constructs_model_or_charges_budget():
    gate, model, factories = gateway(), Model(), []
    state = gate.new_request_state()
    state.deadline = asyncio.get_running_loop().time() - 1
    with pytest.raises(ModelTimeout, match="pipeline_timeout"):
        await invoke(gate, model, state=state, fallback=lambda: factories.append(True))
    assert model.calls == 0 and not factories and state.budget.used == 0


async def test_queue_timeout_is_pipeline_phase_without_model_or_budget_charge(caplog):
    caplog.set_level(logging.INFO, logger="app.infrastructure.ai.model_gateway")
    gate = gateway(llm_max_concurrency=1, pipeline_timeout_seconds=.02, llm_max_retries=0)
    state, model = gate.new_request_state(), Model()
    async with gate.throttle.slot():
        with pytest.raises(ModelTimeout, match="pipeline_timeout"):
            await invoke(gate, model, state=state)
    assert state.queue_wait_seconds > 0 and state.model_started_at is None
    assert state.budget.used == model.calls == 0
    assert "reason=pipeline_timeout" in caplog.text and "queue_wait=" in caplog.text


@pytest.mark.parametrize("kind", ["httpx", "sdk"])
async def test_provider_timeout_types_convert_without_private_error_text(kind, caplog):
    import httpx
    from openai import APITimeoutError
    request = httpx.Request("POST", "https://example.invalid/PRIVATE_URL")
    error = (httpx.ReadTimeout("PRIVATE BODY KEY", request=request) if kind == "httpx"
             else APITimeoutError(request=request))
    with pytest.raises(ModelTimeout) as raised:
        await invoke(gateway(llm_max_retries=0), Model(error))
    assert raised.value.reason == "model_request_timeout" and "PRIVATE" not in caplog.text


async def test_retry_delay_does_not_spend_reserved_attempt_budget():
    gate = ModelGateway(ResearchAISettings(llm_min_interval_seconds=0, retry_delay_seconds=.1,
                        model_timeout_seconds=.05, pipeline_timeout_seconds=.08, llm_max_retries=3))
    model = Model(RuntimeError("429 PRIVATE PROVIDER BODY"))
    with pytest.raises(ModelTimeout, match="pipeline_timeout"):
        await invoke(gate, model)
    assert model.calls == 1


async def test_timeout_logs_phase_and_timings_without_private_content(caplog):
    caplog.set_level(logging.INFO, logger="app.infrastructure.ai.model_gateway")
    gate = gateway(llm_api_key="PRIVATE_API_KEY", model_timeout_seconds=.02, llm_max_retries=0)
    state = gate.new_request_state()
    state.round_index, state.stage = 3, "final"
    with pytest.raises(ModelTimeout):
        await invoke(gate, Model(delay=1), state=state)
    for field in ("trace=", "round=3", "stage=final", "queue_wait=", "first_response=",
                  "model_elapsed=", "total_elapsed=", "tool_elapsed=", "reason=model_final_timeout"):
        assert field in caplog.text
    assert "PRIVATE" not in caplog.text


def test_no_credentials_builds_explicit_local_assistant():
    settings = ResearchAISettings()
    assert build_runtime(settings) is None
    assert build_assistant(object(), settings=settings).runtime is None


@pytest.mark.parametrize("kwargs", [
    {"llm_max_retries": 4}, {"max_tool_iterations": 11}, {"max_tool_iterations": 0},
    {"max_tool_iterations": True}, {"max_tool_iterations": 2.5}, {"total_input_chars": 0},
    {"llm_max_concurrency": 0}, {"model_timeout_seconds": 0},
    {"model_timeout_seconds": float("inf")}, {"retry_delay_seconds": float("nan")},
    {"total_input_chars": 10000001},
])
def test_invalid_unbounded_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        ResearchAISettings(**kwargs)


@pytest.mark.parametrize('rounds', [1, 3, 4, 10])
def test_bounded_tool_round_configuration(rounds, monkeypatch):
    monkeypatch.setenv('RESEARCH_MAX_TOOL_ITERATIONS', str(rounds))
    assert ResearchAISettings.from_env().max_tool_iterations == rounds


def test_application_research_has_no_sdk_network_environment_or_infrastructure_imports():
    root = Path(__file__).parents[1] / "app" / "application" / "agents" / "research"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                       else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for module in modules:
                assert not module.startswith(("agentscope", "httpx", "os", "app.infrastructure", "app.research")), (path, module)
