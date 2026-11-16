"""Bounded answer presentation metadata; never an authorization capability.

Only server-issued event labels and aggregate counts survive, with authorized
read-event document titles/IDs. Model prose, exceptions and bodies are excluded.
"""
from .errors import DomainError

ANSWER_STATUSES = frozenset({"sufficient", "partial", "insufficient", "conflicting"})
SUGGESTED_ACTIONS = frozenset({"switch_library", "add_knowledge", "rephrase_question", "select_material"})
MODES = frozenset({"qa", "align", "progress"})
ANSWER_KINDS = frozenset({'general', 'grounded', 'mixed', 'clarification', 'failure'})
CLARIFICATION_KINDS = frozenset({'document', 'scope', 'comparison', 'other'})
MAX_MISSING_EVIDENCE = 10
MAX_MISSING_EVIDENCE_LENGTH = 1000
MAX_EVENTS = 100
MAX_EVENT_COUNT = 1_000_000

EVENT_TYPES = frozenset({
    "agent.route", "agent.started", "agent.completed", "model.fallback",
    "answer.local_excerpts", "answer.insufficient", "answer.completed",
    "answer.assessed", "answerability.assessed", "history.bounded",
    "retrieval.started", "retrieval.completed", "retrieval.skipped", "retrieval.degraded",
    "rerank.started", "rerank.completed", "rerank.skipped", "rerank.degraded", "document.read",
})
EVENT_REASONS = frozenset({"not_configured", "no_candidates", "progress_mode", "legacy_adapter",
                           "embedding_failed", "index_sync_failed", "vector_search_failed",
                           "vector_unavailable", "rerank_failed", "model_timeout",
                           "model_request_failed", "answer_validation_failed", 'general_question',
                           'model_first_response_timeout', 'model_stream_timeout', 'pipeline_timeout',
                           'model_final_timeout', 'model_request_timeout', 'tool_timeout', 'answer_language_failed'})
EVENT_AGENTS = frozenset({
    "ResearchCoordinator", "ReadOnlyRetrievalAgent", "CollaborationAnalysisAgent", "ProgressAgent",
})
EVENT_STRATEGIES = frozenset({"legacy_lexical", "lexical", "vector", "hybrid_rrf", "semantic"})
EVENT_COUNT_FIELDS = frozenset({
    "hit_count", "candidate_count", "evidence_count", "citation_count", "claim_count",
    "accepted_count", "dropped_count", "task_count", "count", "max_messages", "max_chars",
})


def _invalid():
    raise DomainError(422, "Invalid answer metadata")


def _member(value, choices):
    if not isinstance(value, str) or value not in choices:
        _invalid()
    return value


def _events(value):
    if not isinstance(value, list) or len(value) > MAX_EVENTS:
        _invalid()
    result = []
    for item in value:
        if not isinstance(item, dict):
            _invalid()
        kind = item.get("type")
        # Unknown names can themselves contain secrets, so do not preserve them.
        if not isinstance(kind, str) or kind not in EVENT_TYPES:
            continue
        event = {"type": kind}
        for key, choices in (("agent", EVENT_AGENTS), ("strategy", EVENT_STRATEGIES), ("reason", EVENT_REASONS)):
            label = item.get(key)
            if isinstance(label, str) and label in choices:
                event[key] = label
        if kind == 'document.read':
            for key, limit in (('document_title', 500), ('version_id', 200)):
                label = item.get(key)
                if isinstance(label, str) and label.strip() and len(label) <= limit:
                    event[key] = label
        strategies = item.get("strategies")
        if isinstance(strategies, list):
            event["strategies"] = sorted({label for label in strategies[:10]
                                          if isinstance(label, str) and label in EVENT_STRATEGIES})
        reasons = item.get("reasons")
        if isinstance(reasons, list):
            event["reasons"] = sorted({reason for reason in reasons[:10]
                                       if isinstance(reason, str) and reason in EVENT_REASONS})
        for key in ("degraded", "rerank_applied"):
            if type(item.get(key)) is bool:
                event[key] = item[key]
        for key in EVENT_COUNT_FIELDS:
            if key in item:
                count = item[key]
                if type(count) is not int or not 0 <= count <= MAX_EVENT_COUNT:
                    _invalid()
                event[key] = count
        result.append(event)
    return result


def normalize_answer_metadata(value=None):
    """Return detached, whitelisted metadata, without inventing legacy state."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        _invalid()
    result = {}
    if 'answer_kind' in value:
        result['answer_kind'] = _member(value['answer_kind'], ANSWER_KINDS)
    if 'clarification_kind' in value:
        result['clarification_kind'] = _member(value['clarification_kind'], CLARIFICATION_KINDS)
    if "answer_status" in value:
        result["answer_status"] = _member(value["answer_status"], ANSWER_STATUSES)
    if "suggested_actions" in value:
        actions = value["suggested_actions"]
        if not isinstance(actions, list) or len(actions) > len(SUGGESTED_ACTIONS):
            _invalid()
        result["suggested_actions"] = list(dict.fromkeys(_member(item, SUGGESTED_ACTIONS) for item in actions))
    if "missing_evidence" in value:
        missing = value["missing_evidence"]
        if not isinstance(missing, list) or len(missing) > MAX_MISSING_EVIDENCE:
            _invalid()
        if any(not isinstance(item, str) or not item.strip() or len(item) > MAX_MISSING_EVIDENCE_LENGTH
               for item in missing):
            _invalid()
        result["missing_evidence"] = list(missing)
    if "fallback" in value:
        if type(value["fallback"]) is not bool:
            _invalid()
        result["fallback"] = value["fallback"]
    if "mode" in value:
        result["mode"] = _member(value["mode"], MODES)
    if "events" in value:
        result["events"] = _events(value["events"])
    for key in ("message_id", "run_id", "request_id"):
        if key in value:
            identifier = value[key]
            if not isinstance(identifier, str) or not 1 <= len(identifier) <= 200:
                _invalid()
            result[key] = identifier
    return result
