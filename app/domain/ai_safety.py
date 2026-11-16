"""Provider-neutral presentation safety port, not an authorization boundary.

Implementations return model/display copies. Original requests and evidence ledgers
remain owned by the application and must never be destructively filtered.
"""
from __future__ import annotations

from typing import Any, Protocol


class AISafety(Protocol):
    def input_payload(self, payload: dict) -> dict:
        """Return JSON-compatible untrusted data with an explicit trust policy."""
        ...

    def tool_result(self, value: Any) -> Any:
        """Filter textual leaves without breaking structured tool references."""
        ...

    def output_text(self, text: str) -> str:
        """Redact recognized sensitive material from display prose."""
        ...

    def output_record(self, value: Any) -> Any:
        """Recursively audit display strings, preserving structural IDs/offsets."""
        ...
