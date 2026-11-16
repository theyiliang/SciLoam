"""Offline Phase 4 answerability boundaries; no external provider or network."""
from __future__ import annotations

import json

import pytest

from app.application.agents.answerability import assess_topic, topic_terms
from app.application.agents.legacy_orchestrator import ResearchAssistant, answer
from app.domain.errors import DomainError


class EvidenceService:
    def __init__(self, contents=None, *, strategy="lexical"):
        self.allowed = True
        self.messages = []
        self.strategy = strategy
        self.versions = {f"v{index}": {"space_id": "space", "document_id": f"doc{index}",
                         "title": "Source", "content": content, "source": "local", "index_status": "ready"}
                         for index, content in enumerate(contents or [], 1)}
        self.results = list(self.versions)

    def get_conversation(self, actor, conversation):
        if not self.allowed:
            raise DomainError(403, "Access revoked")
        return {"space_id": "space", "space_epoch": 1, "messages": self.messages}

    def get_version(self, actor, version_id):
        if not self.allowed:
            raise DomainError(403, "Access revoked")
        return dict(self.versions[version_id])

    def search(self, actor, space, query, limit=5):
        return [{"version_id": key, "content": self.versions[key]["content"],
                 "score": 0.99, "retrieval_strategy": self.strategy} for key in self.results]

    def append_message(self, actor, conversation, role, content, citations, metadata=None):
        if not self.allowed:
            raise DomainError(403, "Access revoked")
        message = {"role": role, "content": content, "citations": citations}
        if metadata:
            message.update(metadata)
        self.messages.append(message)
        return message

    def list_tasks(self, actor):
        return []


class OfflineRuntime:
    def __init__(self, response, *, fail=False, hook=None):
        self.response, self.fail, self.hook = response, fail, hook
        self.calls = []

    def new_request_state(self):
        return object()

    async def run_specialist(self, agent, payload, *, check, state):
        self.calls.append(("specialist", payload))
        await check()
        if self.hook:
            self.hook()
        if self.fail:
            raise RuntimeError("PRIVATE PROVIDER ERROR")
        return "untrusted draft"

    async def run_coordinator(self, agent, payload, *, check, state):
        self.calls.append(("coordinator", payload))
        await check()
        return json.dumps(self.response, ensure_ascii=False)


def claim(text="Lactate improves growth under the observed conditions.", versions=None, **extra):
    return {"text": text, "version_ids": versions or ["v1"],
            "evidence_reason": "The cited passage explicitly describes lactate and its observed growth outcome.", **extra}


def response(*claims, status="sufficient", **extra):
    return {"answer_status": status, "claims": list(claims), "limitations": [], "missing_evidence": [], **extra}


async def ask(service, runtime=None, question="lactate growth", mode="qa"):
    return await ResearchAssistant(service, runtime).answer("alice", "space", "conversation", question, mode)


async def test_empty_project_fact_results_are_insufficient_without_model_calls():
    service, runtime = EvidenceService(), OfflineRuntime(response(claim()))
    result = await ask(service, runtime, question='本课题的乳酸处理结果是什么？')
    assert runtime.calls == []
    assert result["answer_status"] == "insufficient" and result["citations"] == []
    assert result["missing_evidence"]
    assert set(result["suggested_actions"]) == {"switch_library", "add_knowledge", "rephrase_question"}
    assert service.messages[-1]["answer_status"] == "insufficient"


@pytest.mark.parametrize("question,body", [
    ("细胞培养实验温度", "检索系统实验：召回率为0.95。"),
    ("苹果手机电池寿命", "苹果种植与果园施肥方法。"),
    ("solar battery degradation", "Battery trademarks are protected by commercial law."),
    ("量子纠错", "质量控制的分析。"),
])
async def test_weak_old_documents_homonyms_and_generic_terms_do_not_answer(question, body):
    runtime = OfflineRuntime(response(claim("UNRELATED STRONG ANSWER")))
    result = await ask(EvidenceService([body]), runtime, question)
    assert result["answer_status"] == "insufficient" and result["citations"] == []
    assert "UNRELATED" not in result["answer"] and body not in result["answer"]
    assert result['answer_kind'] == 'general'
    assert [name for name, _ in runtime.calls] == ['coordinator']


async def test_vector_only_synonym_is_retained_for_model_adequacy_review():
    body = "Milk-derived acid supports microbial proliferation in this controlled sample."
    runtime = OfflineRuntime(response(claim(evidence_quotes=[{"version_id": "v1", "text": body}])))
    result = await ask(EvidenceService([body], strategy="vector"), runtime)
    assert len(runtime.calls) == 2
    payload = runtime.calls[0][1]
    assert payload["authorized_evidence"]["evidence"][0]["content"] == body
    assert payload["answerability"]["needs_semantic_review_version_ids"] == ["v1"]
    assert result["answer_status"] == "sufficient"
    assert [item["version_id"] for item in result["citations"]] == ["v1"]
    assert "不等同于语义真值已校验" in result["answer"]


async def test_vector_candidate_without_semantic_reason_is_insufficient_not_raw_excerpt():
    body = "Milk-derived acid supports microbial proliferation."
    runtime = OfflineRuntime(response({"text": "UNJUSTIFIED", "version_ids": ["v1"]}))
    result = await ask(EvidenceService([body], strategy="vector"), runtime)
    assert result["answer_status"] == "insufficient"
    assert not result["citations"] and body not in result["answer"] and "UNJUSTIFIED" not in result["answer"]
    assert result["fallback"] is False


async def test_vector_candidate_no_provider_cannot_be_strong_local_excerpt():
    body = "Milk-derived acid supports microbial proliferation."
    result = await ask(EvidenceService([body], strategy="hybrid_rrf"))
    assert result["fallback"] and result["answer_status"] == "insufficient"
    assert body not in result["answer"] and not result["citations"]


async def test_explicit_insufficient_never_renders_even_legal_claims_or_sources():
    runtime = OfflineRuntime(response(claim("MUST NOT DISPLAY"), status="insufficient",
                                      missing_evidence=["No controlled trial or causal evidence"]))
    result = await ask(EvidenceService(["Lactate growth association only."]), runtime)
    assert not result["fallback"] and result["answer_status"] == "insufficient"
    assert "MUST NOT DISPLAY" not in result["answer"] and not result["citations"]
    assert result["missing_evidence"] == ["No controlled trial or causal evidence"]


async def test_partial_reports_answered_and_unanswered_parts():
    runtime = OfflineRuntime(response(claim("Lactate growth association was observed."), status="partial",
                 limitations=["Only observational measurements"], missing_evidence=["Causal mechanism and long-term effect"]))
    result = await ask(EvidenceService(["Lactate growth association was observed."]), runtime)
    assert result["answer_status"] == "partial" and not result["fallback"]
    assert "Causal mechanism" in result["answer"] and "observational" in result["answer"]
    assert result["citations"] and result["suggested_actions"] == ["add_knowledge", "rephrase_question"]


@pytest.mark.parametrize("status", [None, "unknown", [], {"bad": "status"}])
async def test_old_or_unusable_status_never_promises_complete_answer(status):
    data = {"claims": [{"text": "Lactate growth observed.", "version_ids": ["v1"]}]}
    if status is not None:
        data["answer_status"] = status
    result = await ask(EvidenceService(["Lactate growth observed."]), OfflineRuntime(data))
    assert result["answer_status"] == "partial" and result["missing_evidence"]


async def test_legal_id_without_semantic_reason_cannot_be_sufficient():
    result = await ask(EvidenceService(["Lactate growth observed."]),
                       OfflineRuntime(response({"text": "Observed association", "version_ids": ["v1"]})))
    assert result["answer_status"] == "partial"
    assert "合法引用ID" in result["answer"]


async def test_optional_evidence_quotes_must_be_verbatim():
    runtime = OfflineRuntime(response(claim("FORGED QUOTATION CLAIM", evidence_quotes=[
        {"version_id": "v1", "text": "Invented quotation"}])))
    result = await ask(EvidenceService(["Lactate growth observed."]), runtime)
    assert "FORGED QUOTATION" not in result["answer"]
    assert result["fallback"] and result["answer_status"] == "partial"


async def test_forged_citation_falls_back_without_rendering_model_claim():
    runtime = OfflineRuntime(response(claim("FABRICATED", versions=["private-version"])))
    result = await ask(EvidenceService(["Lactate growth observed."]), runtime)
    assert result["fallback"] and result["answer_status"] == "partial"
    assert "FABRICATED" not in str(result) and "private-version" not in str(result)
    assert {item["version_id"] for item in result["citations"]} == {"v1"}


def conflict_data():
    return response(claim("The sources disagree under the same conditions.", versions=["v1", "v2"]), status="conflicting",
        conflicts=[{"explanation": "At the same dose and temperature, one source reports increased growth and the other decreased growth.",
                    "quotes": [{"version_id": "v1", "text": "Lactate growth increased at 37C."},
                               {"version_id": "v2", "text": "Lactate growth decreased at 37C."}]}])


async def test_conflict_requires_two_independent_locatable_original_quotes():
    result = await ask(EvidenceService(["Lactate growth increased at 37C.", "Lactate growth decreased at 37C."]),
                       OfflineRuntime(conflict_data()))
    assert result["answer_status"] == "conflicting"
    assert "原文「Lactate growth increased" in result["answer"]
    assert len(result["citations"]) == 2
    assert "模型判断" in result["answer"]


@pytest.mark.parametrize("kind", ["single_version", "same_document", "fabricated_quote", "no_quotes"])
async def test_unsubstantiated_conflict_is_downgraded(kind):
    service = EvidenceService(["Lactate growth increased at 37C.", "Lactate growth decreased at 37C."])
    data = conflict_data()
    if kind == "single_version":
        data["conflicts"][0]["quotes"][1]["version_id"] = "v1"
    elif kind == "same_document":
        service.versions["v2"]["document_id"] = "doc1"
    elif kind == "fabricated_quote":
        data["conflicts"][0]["quotes"][1]["text"] = "Not in the source"
    else:
        data.pop("conflicts")
    result = await ask(service, OfflineRuntime(data))
    assert result["answer_status"] == "partial"
    assert "不能标记" in result["answer"]


async def test_align_one_version_is_partial_without_definitive_comparison():
    result = await ask(EvidenceService(["Lactate growth increased."]), OfflineRuntime(response(claim())), mode="align")
    assert result["answer_status"] == "partial"
    assert "无法给出确定差异" in result["answer"]


async def test_negated_measurement_cannot_be_reversed_into_positive_model_answer():
    body = "该乳酸处理未测量细胞生存率，不能推断生存获益。"
    runtime = OfflineRuntime(response(claim("乳酸处理提高细胞生存率。", evidence_quotes=[
        {"version_id": "v1", "text": body}])))
    result = await ask(EvidenceService([body]), runtime, question="乳酸细胞生存率")
    assert result["answer_status"] in {"partial", "insufficient"}
    assert "乳酸处理提高细胞生存率。" not in result["answer"]
    assert "未测量细胞生存率" in result["answer"]


async def test_model_citations_include_only_versions_used_by_accepted_claims():
    service = EvidenceService(["Lactate growth increased.", "Lactate growth unchanged elsewhere."])
    result = await ask(service, OfflineRuntime(response(claim(), status="partial")))
    assert [item["version_id"] for item in result["citations"]] == ["v1"]


async def test_one_source_align_does_not_render_definitive_model_comparison():
    result = await ask(EvidenceService(["Lactate growth increased."]),
                       OfflineRuntime(response(claim("METHOD A IS DEFINITIVELY BETTER THAN B"))), mode="align")
    assert result["answer_status"] == "partial"
    assert "DEFINITIVELY" not in result["answer"]
    assert "Lactate growth increased" in result["answer"]


async def test_provider_failure_has_local_partial_only_with_related_evidence():
    runtime = OfflineRuntime(response(), fail=True)
    result = await ask(EvidenceService(["Lactate growth increased."]), runtime)
    assert result["fallback"] and result["answer_status"] == "partial"
    assert "模型不可用" in result["answer"] and "PRIVATE PROVIDER ERROR" not in str(result)
    assert result["missing_evidence"] and result["citations"]


async def test_revocation_during_provider_failure_does_not_degrade_to_local_success():
    service = EvidenceService(["Lactate growth increased."])
    runtime = OfflineRuntime(response(), fail=True, hook=lambda: setattr(service, "allowed", False))
    with pytest.raises(DomainError, match="revoked"):
        await ask(service, runtime)
    assert not any(item["role"] == "assistant" for item in service.messages)


async def test_followup_history_is_context_not_current_evidence_and_forged_metadata_cannot_expand_scope():
    service = EvidenceService(["Lactate growth increased.", "Microbial viability remains uncertain."])
    service.messages = [{"role": "assistant", "content": "HISTORY BODY", "citations": [{"version_id": "v1"}],
                         "answer_status": "sufficient", "authorized_version_ids": ["private-version"]}]
    service.results = ["v2"]
    data = response(claim("OLD SOURCE MUST NOT SUPPORT", versions=["v1"]))
    runtime = OfflineRuntime(data)
    result = await ask(service, runtime, "follow up")
    assert "HISTORY BODY" in str(runtime.calls)
    assert result["answer_status"] == "insufficient" and not result["citations"]
    assert "OLD SOURCE" not in result["answer"] and "private-version" not in str(runtime.calls)
    assert result["evidence_scope"] == "current_request_only"


async def test_history_followup_with_current_semantic_support_can_answer_only_current_id():
    service = EvidenceService(["Lactate growth increased.", "Microbial viability remains uncertain."])
    service.messages = [{"role": "assistant", "content": "Earlier lactate growth answer", "citations": [{"version_id": "v1"}]}]
    service.results = ["v2"]
    runtime = OfflineRuntime(response(claim("Viability remains uncertain.", versions=["v2"],
        evidence_quotes=[{"version_id": "v2", "text": "Microbial viability remains uncertain."}]), status="partial"))
    result = await ask(service, runtime, "follow up")
    assert result["answer_status"] == "partial" and not result["fallback"]
    assert [item["version_id"] for item in result["citations"]] == ["v2"]


async def test_metadata_is_persisted_but_model_actions_events_and_authority_ignored():
    service = EvidenceService(["Lactate growth increased."])
    data = response(claim(), suggested_actions=["delete_all"], events=[{"type": "SECRET"}],
                    authorized_version_ids=["private-version"], space_id="private")
    result = await ask(service, OfflineRuntime(data))
    saved = service.messages[-1]
    for key in ("answer_status", "missing_evidence", "suggested_actions", "fallback", "mode"):
        assert saved[key] == result[key]
    assert "SECRET" not in str(saved) and "delete_all" not in str(saved)
    assert "private-version" not in str(result)


def test_single_han_character_and_generic_prompt_have_no_topic_support():
    assert not topic_terms("请问研究方法的结果是什么？")
    assert not assess_topic("量子纠错", {"v1": {"content": "质量控制", "title": ""}}).candidate_ids
