"""Deterministic display redaction; never a substitute for authorization.

Only recognized secrets/internal connection metadata are removed. Public document,
version and evidence IDs (including UUIDs) are not intrinsically confidential.
The caller must audit structured values leaf-by-leaf, not serialized JSON.
No raw matches are retained or logged.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

REDACTED = "[已脱敏]"

# Named assignments include JSON's quoted keys/values. Do not blanket-redact
# UUIDs or the word "epoch" in scientific prose (e.g. "training epoch 20").
_ASSIGNMENT_VALUE = r'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,，;；。}\]\)<>"']+)'''
_INTERNAL_HOST = r"(?:vllm|reranker|qdrant|redis|opensearch)"
SENSITIVE_PATTERNS: list[str] = [
    r'''(?i)(?<![\w])(?:["']?(?:session_id|epoch)["']?)\s*[:=]\s*''' + _ASSIGNMENT_VALUE,
    r"(?<![A-Za-z0-9_])sk-[a-zA-Z0-9_-]{20,}",
    # Any URI with userinfo can leak credentials, regardless of its host/scheme.
    r'''(?i)\b[a-z][a-z0-9+.-]*://[^\s/<>"'@]+:[^\s/<>"'@]+@[^\s<>"'，；。,;)\]}]+''',
    # Container hosts with/without a scheme; don't match public qdrant.io, etc.
    r'''(?i)(?<![\w./-])(?:[a-z][a-z0-9+.-]*://'''
    + _INTERNAL_HOST
    + r'''(?::\d+)?|'''
    + _INTERNAL_HOST
    + r''':\d+)(?![\w.-])(?:/[^\s<>"'，；。,;)\]}]*)?''',
]
_compiled = tuple(re.compile(pattern) for pattern in SENSITIVE_PATTERNS)


def audit_output(text: str, *, known_secrets: Iterable[str] = ()) -> tuple[bool, str]:
    """Return ``(unchanged, display_text)`` without storing raw sensitive hits.

    Exact configured keys cover nonstandard provider formats. Longer secrets go
    first so overlapping values cannot leave a sensitive suffix behind.
    """
    if not text:
        return True, text
    cleaned = text
    for secret in sorted({value for value in known_secrets if value}, key=len, reverse=True):
        cleaned = cleaned.replace(secret, REDACTED)
    for pattern in _compiled:
        cleaned = pattern.sub(REDACTED, cleaned)
    return cleaned == text, cleaned
