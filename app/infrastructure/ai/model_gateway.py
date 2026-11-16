"""Research-only gateway controls; no library session context or body logging."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
from openai import APITimeoutError

from app.domain.ai_failures import ModelTimeout
from app.domain.errors import DomainError
from app.infrastructure.budget import TokenBudget
from app.infrastructure.throttle import GatewayThrottle
from app.infrastructure.transient import is_transient_error

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResearchAISettings:
    llm_api_key: str = ""
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen3-max"
    llm_fallback_model: str = ""
    llm_max_concurrency: int = 2
    llm_min_interval_seconds: float = 1.0
    llm_max_retries: int = 2
    model_timeout_seconds: float = 45.0
    tool_timeout_seconds: float = 15.0
    pipeline_timeout_seconds: float = 180.0
    max_tool_iterations: int = 3
    total_input_chars: int = 240000
    retry_delay_seconds: float = 0.25

    def __post_init__(self) -> None:
        if not 1 <= self.llm_max_concurrency <= 128:
            raise ValueError("llm_max_concurrency must be 1-128")
        if not 0 <= self.llm_max_retries <= 3:
            raise ValueError("llm_max_retries must be 0-3")
        if type(self.max_tool_iterations) is not int or not 1 <= self.max_tool_iterations <= 10:
            raise ValueError("max_tool_iterations must be 1-10 tool rounds, plus at most one final answer turn")
        timeouts = (self.model_timeout_seconds, self.tool_timeout_seconds, self.pipeline_timeout_seconds)
        if not all(math.isfinite(value) and 0 < value <= 3600 for value in timeouts):
            raise ValueError("timeouts must be finite and between 0 and 3600 seconds")
        if not 1 <= self.total_input_chars <= 10000000:
            raise ValueError("input budget must be 1-10000000 characters")
        if not all(math.isfinite(value) and 0 <= value <= 60 for value in
                   (self.llm_min_interval_seconds, self.retry_delay_seconds)):
            raise ValueError("intervals must be finite and between 0 and 60 seconds")

    @classmethod
    def from_env(cls) -> ResearchAISettings:
        """Only the infrastructure/composition boundary reads process settings."""
        return cls(
            llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
            llm_base_url=os.getenv("LLM_BASE_URL") or cls.llm_base_url,
            llm_model=os.getenv("LLM_MODEL") or cls.llm_model,
            llm_fallback_model=os.getenv("LLM_FALLBACK_MODEL", ""),
            llm_max_concurrency=int(os.getenv("LLM_MAX_CONCURRENCY", "2")),
            llm_min_interval_seconds=float(os.getenv("LLM_MIN_INTERVAL_SECONDS", "1")),
            llm_max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
            model_timeout_seconds=float(os.getenv("RESEARCH_MODEL_TIMEOUT_SECONDS", "45")),
            tool_timeout_seconds=float(os.getenv("RESEARCH_TOOL_TIMEOUT_SECONDS", "15")),
            pipeline_timeout_seconds=float(os.getenv("RESEARCH_PIPELINE_TIMEOUT_SECONDS", "180")),
            max_tool_iterations=int(os.getenv("RESEARCH_MAX_TOOL_ITERATIONS", "3")),
            total_input_chars=int(os.getenv("RESEARCH_TOTAL_INPUT_CHARS", "240000")),
        )


# Minimum seconds between authorization revalidations while consuming a stream.
STREAM_CHECK_SECONDS = 1.0


def _status_code(error: Exception) -> int:
    response = getattr(error, 'response', None)
    code = getattr(response, 'status_code', getattr(error, 'status_code', None))
    return code if type(code) is int and 100 <= code <= 599 else 0


class InputBudgetExceeded(RuntimeError):
    pass


@dataclass
class RequestState:
    budget: TokenBudget
    trace_id: str
    deadline: float
    round_index: int = 0
    stage: str = "model"
    started_at: float | None = None
    first_response_at: float | None = None
    model_started_at: float | None = None
    stream_started: bool = False
    queue_wait_seconds: float = 0.0
    model_elapsed_seconds: float = 0.0
    tool_elapsed_seconds: float = 0.0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def mark_response(self) -> None:
        """Record real model content, including private tool-call deltas."""
        if self.first_response_at is None:
            self.first_response_at = asyncio.get_running_loop().time()
        self.stream_started = True


def input_size(messages: Any, tools: Any) -> int:
    def encode(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump()
        if hasattr(value, "__dict__"):
            return vars(value)
        raise TypeError("Cannot account for model input")
    return len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, default=encode))


class ModelGateway:
    def __init__(self, settings: ResearchAISettings):
        self.settings = settings
        self.throttle = GatewayThrottle(settings.llm_max_concurrency, settings.llm_min_interval_seconds)

    def new_request_state(self) -> RequestState:
        now = asyncio.get_running_loop().time()
        return RequestState(TokenBudget(self.settings.total_input_chars), uuid.uuid4().hex,
                            now + self.settings.pipeline_timeout_seconds, started_at=now)

    def _remaining(self, state: RequestState, *, retry: bool = False) -> float:
        remaining = state.deadline - asyncio.get_running_loop().time()
        # Reserve a complete configured attempt for retries/fallbacks. A tiny
        # scheduling tolerance preserves deliberately tiny test configurations.
        minimum = min(self.settings.model_timeout_seconds, self.settings.pipeline_timeout_seconds)
        tolerance = min(0.001, minimum * 0.01)
        if remaining <= 0 or (retry and remaining + tolerance < minimum):
            raise ModelTimeout("pipeline_timeout")
        return remaining

    def _timeout(self, state: RequestState, *, streaming: bool) -> ModelTimeout:
        if asyncio.get_running_loop().time() >= state.deadline:
            return ModelTimeout("pipeline_timeout")
        if state.stage == "final":
            return ModelTimeout("model_final_timeout")
        if streaming:
            return ModelTimeout("model_stream_timeout" if state.stream_started
                                else "model_first_response_timeout")
        return ModelTimeout("model_request_timeout")

    def _log(self, event: str, state: RequestState, *, name: str, route: int,
             attempt: int, size: int, error: Exception | None = None) -> None:
        now = asyncio.get_running_loop().time()
        first = (state.first_response_at - state.model_started_at
                 if state.first_response_at is not None and state.model_started_at is not None else -1.0)
        # Only bounded metadata; never provider text, bodies, URLs or keys.
        logger.log(logging.WARNING if error else logging.INFO,
                   "research.%s trace=%s agent=%s round=%d stage=%s route=%d attempt=%d "
                   "input_chars=%d queue_wait=%.6f first_response=%.6f model_elapsed=%.6f "
                   "total_elapsed=%.6f tool_elapsed=%.6f reason=%s error_type=%s status_code=%d",
                   event, state.trace_id, name, state.round_index, state.stage, route, attempt,
                   size, state.queue_wait_seconds, first, state.model_elapsed_seconds,
                   now - state.started_at if state.started_at is not None else 0.0,
                   state.tool_elapsed_seconds, error.reason if isinstance(error, ModelTimeout) else "none",
                   type(error).__name__ if error else "none", _status_code(error) if error else 0)

    @asynccontextmanager
    async def _attempt(self, *, name: str, state: RequestState, check: Callable,
                       size: int, route: int, attempt: int, streaming: bool):
        remaining = self._remaining(state, retry=bool(route or attempt))
        loop = asyncio.get_running_loop()
        queued = loop.time()
        if state.started_at is None:
            state.started_at = queued
        state.first_response_at = state.model_started_at = None
        state.stream_started = False
        state.queue_wait_seconds = state.model_elapsed_seconds = 0.0
        pipeline_timer = asyncio.timeout(remaining)
        try:
            async with pipeline_timer:
                async with self.throttle.slot():
                    state.queue_wait_seconds = loop.time() - queued
                    # Reauthorize after queueing and before every retry/fallback.
                    await check()
                    self._remaining(state, retry=bool(route or attempt))
                    if size > state.budget.remaining:
                        raise InputBudgetExceeded("Research total input budget exceeded")
                    state.budget.charge(name, size)
                    state.model_started_at = loop.time()
                    self._log("model", state, name=name, route=route, attempt=attempt, size=size)
                    async with asyncio.timeout(self.settings.model_timeout_seconds):
                        yield
        except (TimeoutError, httpx.TimeoutException, APITimeoutError) as exc:
            if isinstance(exc, ModelTimeout):
                raise
            reason = ModelTimeout("pipeline_timeout") if pipeline_timer.expired() else self._timeout(state, streaming=streaming)
            raise reason from None
        finally:
            now = loop.time()
            if state.model_started_at is not None:
                state.model_elapsed_seconds = now - state.model_started_at
            else:
                state.queue_wait_seconds = now - queued

    async def _retry_delay(self, state: RequestState, attempt: int) -> None:
        self._remaining(state, retry=True)
        delay = self.settings.retry_delay_seconds * (2 ** attempt)
        minimum = min(self.settings.model_timeout_seconds, self.settings.pipeline_timeout_seconds)
        if state.deadline - asyncio.get_running_loop().time() < delay + minimum:
            raise ModelTimeout("pipeline_timeout")
        if delay:
            await asyncio.sleep(delay)
        self._remaining(state, retry=True)

    async def invoke(self, *, name: str, primary: Any, fallback: Callable[[], Any] | None,
                     messages: list, tools: Any, tool_choice: Any,
                     check: Callable[[], Awaitable[None]], state: RequestState,
                     **kwargs: Any) -> Any:
        size = input_size(messages, tools)
        models = [lambda: primary] + ([fallback] if fallback is not None else [])
        for model_index, factory in enumerate(models):
            self._remaining(state, retry=bool(model_index))
            model = factory()
            for attempt in range(self.settings.llm_max_retries + 1):
                try:
                    async with self._attempt(name=name, state=state, check=check, size=size,
                                             route=model_index, attempt=attempt, streaming=False):
                        response = await model(messages, tools=tools, tool_choice=tool_choice, **kwargs)
                        self._remaining(state)
                        state.mark_response()
                    self._log("model_complete", state, name=name, route=model_index, attempt=attempt, size=size)
                    return response
                except (DomainError, PermissionError, InputBudgetExceeded):
                    raise
                except Exception as exc:
                    self._log("model_failed", state, name=name, route=model_index, attempt=attempt, size=size, error=exc)
                    if isinstance(exc, ModelTimeout) and exc.reason == "pipeline_timeout":
                        raise
                    if not (isinstance(exc, TimeoutError) or is_transient_error(exc)):
                        raise
                    if attempt < self.settings.llm_max_retries:
                        await self._retry_delay(state, attempt)
                        continue
                    if model_index + 1 == len(models):
                        raise
        raise RuntimeError("No model available")

    async def stream(self, *, name: str, primary: Any, fallback: Callable[[], Any] | None,
                     messages: list, check: Callable[[], Awaitable[None]], state: RequestState) -> AsyncIterator[str]:
        """Own the provider iterator for its entire lifetime, including cancellation.

        Retries/fallback are allowed only before any text has escaped upstream.
        Once streaming begins, the application emits a canonical replacement on
        failure instead of concatenating output from a second model.
        """
        size = input_size(messages, None)
        models = [lambda: primary] + ([fallback] if fallback is not None else [])
        emitted = False
        for model_index, factory in enumerate(models):
            self._remaining(state, retry=bool(model_index))
            provider = factory()
            for attempt in range(self.settings.llm_max_retries + 1):
                try:
                    async with self._attempt(name=name, state=state, check=check, size=size,
                                             route=model_index, attempt=attempt, streaming=True):
                        iterator = provider(messages).__aiter__()
                        try:
                            # Per-fragment revalidation is prohibitively expensive
                            # (measured ~0.2s per check; hundreds of fragments per
                            # answer). Keep a bounded cadence; attempt start and
                            # stream completion still check strictly.
                            last_fragment_check = 0.0
                            async for fragment in iterator:
                                now = asyncio.get_running_loop().time()
                                if now - last_fragment_check >= STREAM_CHECK_SECONDS:
                                    last_fragment_check = now
                                    await check()
                                self._remaining(state)
                                if not isinstance(fragment, str):
                                    raise ValueError("Invalid provider fragment")
                                if fragment:
                                    state.mark_response()
                                    emitted = True
                                    yield fragment
                        finally:
                            close = getattr(iterator, "aclose", None)
                            if close is not None:
                                await close()
                        await check()
                    self._log("stream_complete", state, name=name, route=model_index, attempt=attempt, size=size)
                    return
                except (DomainError, PermissionError, InputBudgetExceeded):
                    raise
                except Exception as exc:
                    self._log("stream_failed", state, name=name, route=model_index, attempt=attempt, size=size, error=exc)
                    if isinstance(exc, ModelTimeout) and exc.reason == "pipeline_timeout":
                        raise
                    if emitted or state.stream_started or not (isinstance(exc, TimeoutError) or is_transient_error(exc)):
                        raise
                    if attempt < self.settings.llm_max_retries:
                        await self._retry_delay(state, attempt)
                        continue
                    if model_index + 1 == len(models):
                        raise
        raise RuntimeError("No model available")
