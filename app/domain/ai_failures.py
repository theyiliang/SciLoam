"""Safe, provider-independent AI failure reasons for application boundaries."""
from __future__ import annotations


class ModelTimeout(TimeoutError):
    """A timeout whose public message is a fixed machine-readable reason only."""

    REASONS = frozenset({
        "model_first_response_timeout", "model_stream_timeout", "pipeline_timeout",
        "model_final_timeout", "model_request_timeout", "tool_timeout",
    })

    def __init__(self, reason: str):
        if reason not in self.REASONS:
            raise ValueError("Invalid model timeout reason")
        self.reason = reason
        super().__init__(reason)
