"""Assembly helpers used by the main composition root, never by application code."""
from __future__ import annotations

from typing import Any

from app.application.agents.orchestrator import ResearchAssistant
from .agentscope_runtime import AgentScopeRuntime
from .model_gateway import ResearchAISettings
from app.infrastructure.security.ai_safety import SciLoamSafety


def build_runtime(settings: ResearchAISettings | None = None, *, model_factory=None,
                  fallback_model_factory=None) -> AgentScopeRuntime | None:
    settings = settings if settings is not None else ResearchAISettings.from_env()
    if model_factory is None and not settings.llm_api_key:
        return None
    return AgentScopeRuntime(settings, model_factory=model_factory,
                             fallback_model_factory=fallback_model_factory)


def build_assistant(service: Any, *, settings: ResearchAISettings | None = None,
                    runtime=None, model_factory=None,
                    security_recorder=None) -> ResearchAssistant:
    settings = settings if settings is not None else ResearchAISettings.from_env()
    if runtime is None:
        runtime = build_runtime(settings, model_factory=model_factory)
    safety = SciLoamSafety(api_key=settings.llm_api_key)
    # Authorized history reconstructs citation titles from live records, so its
    # display boundary must use the same guard as new assistant output.
    if callable(getattr(service, 'get_conversation', None)):
        service._answer_safety = safety
    return ResearchAssistant(service, runtime, safety=safety,
                             security_recorder=security_recorder)
