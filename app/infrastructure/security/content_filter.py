"""Heuristic scanning of untrusted text, not prompt-injection prevention.

Only matching spans are replaced so normal scientific material stays useful.
JSON structure, role separation, authorization and exact evidence validation
must be enforced by callers independently of this deliberately limited scan.
No raw input or matches are logged.
"""
from __future__ import annotations

import re

FILTERED_PLACEHOLDER = "[内容已过滤：疑似注入]"

DANGEROUS_PATTERNS: list[str] = [
    r"(?i)ignore\s+(?:(?:all|any)\s+)?(?:previous|prior|above|system|developer)\s+instructions?",
    r"(?i)(?:disregard|override|forget)\s+(?:(?:all|any|the)\s+)?(?:previous|prior|above|system|developer)\s+(?:instructions?|rules?|messages?)",
    r"忽略.{0,10}(?:之前|以上|所有|系统).{0,10}(?:指令|指示|规则)",
    r"(?i)system\s*prompt",
    r"(?i)you\s+are\s+now",
    r"扮演.{0,10}角色",
    r"(?i)output\s+(?:all|every)\s+(?:user|system)",
    r"(?i)(?:reveal|print|expose|leak)\s+(?:your\s+|the\s+|all\s+)?(?:api[ _-]?keys?|secrets?|credentials?|session[ _-]?ids?)",
    r"(?:泄露|输出|打印|透露).{0,8}(?:密钥|凭据|系统提示词|会话标识)",
    # Override-mode claims and bulk-dump requests (OWASP LLM01). Wording stays
    # generic: an invisible injection phrase is replaced by a placeholder, so
    # the surviving sentence can read like an ordinary catalogue question.
    r"忽略.{0,12}(?:资料|数据|文档)?边界",
    r"(?:进入|切换|开启|切换到|现在是|当前是).{0,8}(?:调试|开发者|开发|维护|管理员|超级用户|测试)模式",
    r"(?i)\b(?:debug|developer|dev|maintenance|admin|god|test)\s+mode\b",
    r"(?:逐字|全文|原文|完整).{0,6}(?:打印|输出|导出|转储|列举)",
    r"(?:打印|输出|导出|转储|列举|列出).{0,12}(?:全部|所有|每一份|每个|整份).{0,8}(?:文档|资料|原文|全文|正文)",
    r"(?i)(?:print|dump|export|output|list|enumerate)\s+(?:all|every|each)\s+(?:documents?|files?|records?|contents?)",
    r"(?i)(?:print|dump|output|reveal|expose)\s+(?:the\s+)?(?:verbatim|full\s+text|entire\s+text)",
    # These are data, never actual message roles. Flag familiar role-spoofing
    # markers as a heuristic, but do not use XML tags as a trust boundary.
    r"(?i)<\|(?:im_start|im_end|system|assistant|developer)\|>",
    r"(?i)</?(?:system|developer|assistant)(?:\s[^>\r\n]*)?>",
]
_compiled = tuple(re.compile(pattern) for pattern in DANGEROUS_PATTERNS)


def injection_detected(text: str) -> bool:
    """Return whether untrusted text matches any heuristic injection pattern."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in _compiled)


def sanitize_tool_output(text: str) -> tuple[bool, str]:
    """Return ``(matched, display_text)`` for textual data, never serialized JSON."""
    if not text:
        return False, text
    cleaned = text
    for pattern in _compiled:
        cleaned = pattern.sub(FILTERED_PLACEHOLDER, cleaned)
    return cleaned != text, cleaned
