"""Conservative answerability policy, not a semantic truth or confidence oracle.

Retrieval ranks/RRF scores are deliberately never used as answer confidence.
Lexical overlap is a topic guard only; vector candidates need model adequacy review.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Mapping

_EN_STOP = set("a an the is are was were be been do does did how what why when where which who can could would should please tell me about explain describe compare comparison difference differences and or of in on to for with from this that these those it its result results study research paper document evidence answer question work works up follow more detail details method methods experiment experiments metric metrics analysis system technology".split())
_ZH_STOP = ("请问", "请帮我", "帮我", "告诉我", "请", "一下", "什么", "为什么", "怎么样", "怎么", "如何", "是否", "有哪些", "哪些", "多少", "哪里", "为何", "能否", "可以", "根据", "关于", "有关", "文献", "研究", "论文", "文件", "文档", "资料", "证据", "内容", "问题", "答案", "结果", "结论", "进行", "分析", "比较", "对比", "差异", "方法", "指标", "实验", "试验", "系统", "技术", "效果", "影响", "相关", "情况", "介绍", "总结", "的", "了", "吗", "呢", "是", "和", "与", "及", "在", "对", "有")
_FOLLOWUP = re.compile(r"^(?:follow[ -]?up|more details?|continue|what about (?:it|that)|继续|详细一点|展开(?:说说)?|那呢|然后呢|这(?:个|些)(?:呢|怎么样)?)[?？。!！\s]*$", re.I)


def topic_terms(text: str) -> set[str]:
    text = text.casefold()
    english = {term for term in re.findall(r"[a-z0-9][a-z0-9_+-]*", text)
               if len(term) > 1 and term not in _EN_STOP and not term.isdigit()}
    for stop in sorted(_ZH_STOP, key=len, reverse=True):
        text = text.replace(stop, " ")
    # Never count a single shared Han character as topic support.
    chinese = {span[i:i + 2] for span in re.findall(r"[㐀-鿿]+", text)
               for i in range(len(span) - 1)}
    return english | chinese


@dataclass
class TopicGate:
    lexical_ids: set[str] = field(default_factory=set)
    semantic_ids: set[str] = field(default_factory=set)
    weak_ids: set[str] = field(default_factory=set)

    @property
    def candidate_ids(self) -> set[str]:
        return self.lexical_ids | self.semantic_ids

    def as_dict(self) -> dict:
        return {"lexical_version_ids": sorted(self.lexical_ids),
                "needs_semantic_review_version_ids": sorted(self.semantic_ids),
                "weak_version_ids": sorted(self.weak_ids),
                "notice": "主题词匹配仅作候选筛选；检索排名/RRF不是置信度，合法ID也不证明语义支持。"}


def _reviewable_lexical_candidate(question: str, body: str, overlap: set[str]) -> bool:
    # A continuous subject phrase is less brittle than the ratio of all query
    # bigrams. It grants model review only, never extractive answer authority.
    discourse = ('会议纪要', '纪要', '确认', '最终', '等于', '分钟', '小时', '记录', '观察窗口')
    if not overlap:
        return False
    subject = question.casefold()
    source = body.casefold()
    for word in sorted(_ZH_STOP, key=len, reverse=True):
        subject = subject.replace(word, ' ')
    for word in discourse:
        subject = subject.replace(word, ' ')
    phrases = {span[i:i + 4] for span in re.findall(r'[㐀-鿿]+', subject)
               for i in range(len(span) - 3)}
    if any(phrase in source for phrase in phrases):
        return True
    # Numeric comparison needs both real quantities and a topic/reference signal;
    # coincident numbers or units alone cannot admit unrelated questions.
    quantities = set(re.findall(r'\d+(?:\.\d+)?', question))
    shared_numbers = quantities & set(re.findall(r'\d+(?:\.\d+)?', body))
    units = set(re.findall(r'\d+(?:\.\d+)?\s*(?:分钟|小时|天|个月|%|％)', question))
    shared_unit = any(re.sub(r'\s+', '', unit) in re.sub(r'\s+', '', body) for unit in units)
    substantive = overlap - {'分钟', '小时', '最终', '确认', '等于', '实验', '记录', '两人'}
    reference = '纪要' in question and '纪要' in body
    return ((len(shared_numbers) >= 2 or shared_unit)
            and bool(substantive - {'纪要', '要加'} or reference))


def is_document_overview(question: str) -> bool:
    if re.fullmatch(r'\s*(?:请)?(?:总结|概括|解读|介绍)(?:一下)?\s*《[^》\n]{1,300}》\s*[？?。！!]*\s*', question):
        return True
    if re.fullmatch(r'.{1,300}(?:这份文档|这个文档)(?:主要)?(?:说了什么|讲了什么)[？?。！!]*', question):
        return True
    return bool(re.fullmatch(
        r'\s*(?:请|请帮我|帮我)?\s*(?:(?:概括|总结|介绍|解读)(?:一下)?\s*(?:这(?:个|份|篇)?|该|当前)?(?:文档|文件|材料|文章|论文)|'
        r'(?:这(?:个|份|篇)?|该|当前)(?:文档|文件|材料|文章|论文)(?:主要)?(?:说了什么|讲了什么|讲什么|内容是什么|是什么内容))\s*[？?。！!]*\s*',
        question))


# Whole-space catalog/overview questions. fullmatch keeps fact questions that
# merely reference the library (e.g. "根据知识库里的实验记录，本课题指标是什么")
# on the normal retrieval path; only pure catalog intents load a space overview.
_SPACE_OVERVIEW_ASK = re.compile(
    r'\s*(?:请|请问|请帮我|帮我|麻烦)?\s*(?:告诉我|介绍一下|介绍|说明一下|说一下)?\s*'
    r'(?:当前|这个|本|该|我的)?\s*知识库\s*(?:里面|里|中|内)?\s*(?:都)?\s*'
    r'(?:有|包含|收录了?|涵盖|涉及)\s*(?:什么|哪些|啥)\s*(?:内容|资料|文档|文件|材料|信息|知识|数据)?\s*[？?。！!]*\s*')
_SPACE_OVERVIEW_META = re.compile(
    r'\s*(?:请|请问|请帮我|帮我|麻烦)?\s*(?:'
    r'(?:介绍|总结|概括|说明)(?:一下)?\s*(?:当前|这个|本|该|我的)?\s*知识库|'
    r'(?:当前|这个|本|该|我的)?\s*知识库\s*(?:主要)?\s*(?:讲了什么|是什么|是干什么的|是关于什么的|介绍了什么|内容是什么|是什么内容)|'
    r'(?:列出|列一下)\s*(?:当前|这个|本|该|我的)?\s*知识库\s*(?:的)?\s*(?:目录|清单|列表|文档)?|'
    r'(?:当前|这个|本|该|我的)?\s*知识库\s*(?:的)?\s*(?:目录|概览|概况|索引|清单|文档列表))\s*[？?。！!]*\s*')
_SPACE_OVERVIEW_EN = re.compile(
    r"\s*(?:please\s+)?(?:tell me\s+)?what(?:'s| is| are)?\s+(?:in|inside)\s+(?:the|this|my|current)\s+knowledge\s*base\s*[?.\s]*|"
    r"\s*(?:please\s+)?list\s+(?:the\s+)?(?:documents|files|contents)\s+in\s+(?:the|this|my|current)\s+knowledge\s*base\s*[?.\s]*",
    re.I)


def is_space_overview(question: str) -> bool:
    return bool(_SPACE_OVERVIEW_ASK.fullmatch(question)
                or _SPACE_OVERVIEW_META.fullmatch(question)
                or _SPACE_OVERVIEW_EN.fullmatch(question))


def assess_topic(question: str, evidence: Mapping[str, dict]) -> TopicGate:
    terms = topic_terms(question)
    gate = TopicGate()
    followup = bool(_FOLLOWUP.fullmatch(question.strip()))
    for version_id, item in evidence.items():
        source_terms = topic_terms(str(item.get("title", "")) + " " + str(item.get("content", "")))
        overlap = terms & source_terms
        # One narrow keyword is allowed; multi-topic questions must not pass on
        # a single homonym. This is intentionally a heuristic, not truth scoring.
        lexical = bool(terms and overlap and len(overlap) / len(terms) >= 0.5
                       and (len(terms) == 1 or len(overlap) >= 2))
        strategy = str(item.get("retrieval_strategy", "")).casefold()
        if item.get('retrieval_strategy') == 'document_overview' and is_document_overview(question):
            gate.lexical_ids.add(version_id)
        elif lexical:
            gate.lexical_ids.add(version_id)
        elif ("vector" in strategy or "hybrid" in strategy or "semantic" in strategy or followup
              or _reviewable_lexical_candidate(question, str(item.get('content', '')), overlap)):
            gate.semantic_ids.add(version_id)
        else:
            gate.weak_ids.add(version_id)
    return gate


def strings(value: object) -> list[str]:
    return [item.strip()[:1000] for item in value[:10]
            if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


@dataclass
class AnswerOutcome:
    answer: str
    status: str
    citation_ids: set[str] = field(default_factory=set)
    limitations: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    suggested_actions: list[str] = field(default_factory=list)


def insufficient(reason: str, *, local: bool = False) -> AnswerOutcome:
    return AnswerOutcome(
        ("本地处理（未生成模型总结）。\n" if local else "") + "证据不足，无法据此回答。\n" + reason,
        "insufficient", limitations=[reason], missing_evidence=["能够直接支持当前问题的主题相关证据"],
        suggested_actions=["switch_library", "add_knowledge", "rephrase_question"])


def _independent(ids: set[str], evidence: Mapping[str, dict]) -> bool:
    return len(ids) >= 2 and len({evidence[key].get("document_id") or key for key in ids}) >= 2


def render_coordinator(text: str, evidence: Mapping[str, dict], tasks: list[dict],
                       gate: TopicGate, mode: str) -> AnswerOutcome:
    # Tolerate one complete formatting wrapper, never extract a JSON fragment
    # from prose or repair source quotes/claims to make invalid output pass.
    wrapper = re.fullmatch(r'\s*```(?:json)?\s*\n(.*?)\n```\s*', text, re.S)
    data = json.loads(wrapper.group(1) if wrapper else text)
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        raise ValueError("Invalid coordinator response")
    limitations = strings(data.get("limitations"))
    missing = strings(data.get("missing_evidence"))
    requested = data.get("answer_status")
    valid_status = isinstance(requested, str) and requested in {"sufficient", "partial", "insufficient", "conflicting"}
    status = requested if valid_status else "partial"
    if not valid_status:
        limitations.append("模型未提供有效的可回答性状态，仅保留局部来源陈述，不承诺完整回答。")
    if status == "insufficient":
        result = insufficient("现有候选尚不足以支持当前问题；不展示候选摘录或模型断言。")
        result.limitations += limitations
        result.missing_evidence = missing or result.missing_evidence
        return result
    known_tasks = {task["id"] for task in tasks}
    lines, accepted = [], set()
    dropped = False
    semantic_unresolved = False
    semantic_rejected = False
    source_boundaries: dict[str, list[str]] = {}
    for claim in data["claims"][:20]:
        if not isinstance(claim, dict) or not isinstance(claim.get("text"), str) or not claim["text"].strip():
            dropped = True
            continue
        versions, task_ids = claim.get("version_ids", []), claim.get("task_ids", [])
        if (not isinstance(versions, list) or not isinstance(task_ids, list)
                or not all(isinstance(key, str) and key in gate.candidate_ids for key in versions)
                or not all(isinstance(key, str) and key in known_tasks for key in task_ids)
                or (task_ids and mode != "progress") or (versions and mode == "progress")
                or not (versions or (mode == "progress" and task_ids))):
            dropped = True
            continue
        semantic = set(versions) & gate.semantic_ids
        reason = claim.get("evidence_reason")
        quotes = claim.get("evidence_quotes")
        if quotes is not None and (not isinstance(quotes, list) or not 0 < len(quotes) <= 10 or any(
                not isinstance(quote, dict) or quote.get("version_id") not in versions
                or not isinstance(quote.get("text"), str) or not quote["text"].strip()
                or quote["text"] not in evidence[quote["version_id"]]["content"] for quote in quotes[:10])):
            dropped = True
            continue
        if semantic and (not isinstance(reason, str) or len(reason.strip()) < 12
                         or not isinstance(quotes, list)
                         or not set(versions).issubset({quote['version_id'] for quote in quotes})):
            dropped = True
            semantic_rejected = True
            continue
        # A small explicit-negation guard catches obvious measurement reversals;
        # it is not a general entailment validator. Ambiguous cases stay partial.
        negative = re.compile(r"未(?:测量|评估|检测|证实|观察到)|没有(?:测量|评估|证据|提高)|不支持|无法(?:确定|证实)|not (?:measured|assessed|evaluated|observed)|no evidence|did not|does not", re.I)
        grounded_quotes = (isinstance(quotes, list) and quotes
                           and set(versions).issubset({quote['version_id'] for quote in quotes}))
        passages = ([quote['text'] for quote in quotes] if grounded_quotes
                    else [evidence[key]['content'] for key in versions])
        if (any(negative.search(passage) for passage in passages)
                and not negative.search(claim["text"])):
            dropped = True
            continue
        if (not isinstance(reason, str) or len(reason.strip()) < 12
                or not isinstance(quotes, list) or not quotes
                or not set(versions).issubset({quote["version_id"] for quote in quotes[:10]})):
            semantic_unresolved = True
        if (mode == 'align' and requested != 'conflicting' and grounded_quotes
                and isinstance(reason, str) and len(reason.strip()) >= 12):
            # Source-attributed boundary, not permission to render an arbitrary
            # one-source model conclusion. Only literal nonconfirmation survives.
            for quote in quotes:
                if (re.search(r'不是[^。；\n]{0,12}(?:最终值|审定[^。；\n]{0,6}值)|'
                              r'最终(?:值|比例|结果|结论)[^。；\n]{0,8}(?:尚未|未经|未)(?:确认|审定)', quote['text'])
                        and re.search(r'仍须核对|仍需核对|尚未确定|未完成|未确认', quote['text'])):
                    source_boundaries.setdefault(quote['version_id'], [])
                    if quote['text'] not in source_boundaries[quote['version_id']]:
                        source_boundaries[quote['version_id']].append(quote['text'])
        accepted.update(versions)
        labels = [f"版本 {key}" for key in versions] + [f"任务记录 {key}" for key in task_ids]
        lines.append(f"- {claim['text'][:3000]}（{'；'.join(labels)}）")
        if isinstance(reason, str) and reason.strip():
            lines.append("  来源支持说明（模型判断）：" + reason.strip()[:1000])
    if not lines:
        if semantic_rejected and not gate.lexical_ids:
            return insufficient("候选来源尚无具体语义支持说明；仅有合法引用ID不足以支持当前问题。")
        if dropped:
            raise ValueError("No evidence-supported claims")
        return insufficient("模型未给出可引用的局部回答。")
    if dropped:
        status = "partial"
        limitations.append("部分模型断言缺少有效来源或语义支持说明，已省略。")
    if status == "conflicting":
        conflicts = data.get("conflicts")
        valid_conflict = False
        if isinstance(conflicts, list):
            for conflict in conflicts[:10]:
                if not isinstance(conflict, dict) or not isinstance(conflict.get("explanation"), str) or len(conflict["explanation"].strip()) < 12:
                    continue
                quotes = conflict.get("quotes")
                ids, quote_texts = set(), set()
                if not isinstance(quotes, list):
                    continue
                for quote in quotes[:10]:
                    if not isinstance(quote, dict):
                        continue
                    key, body = quote.get("version_id"), quote.get("text")
                    if (isinstance(key, str) and key in accepted and isinstance(body, str)
                            and len(body.strip()) >= 4 and body in evidence[key]["content"]):
                        ids.add(key)
                        quote_texts.add(body)
                if _independent(ids, evidence) and len(quote_texts) >= 2:
                    valid_conflict = True
                    lines.append("来源矛盾（模型判断，未作事实裁决）：" + conflict["explanation"][:1000])
                    for quote in quotes[:10]:
                        if (isinstance(quote, dict) and isinstance(quote.get("version_id"), str)
                                and isinstance(quote.get("text"), str) and quote["version_id"] in ids
                                and quote["text"] in quote_texts
                                and quote["text"] in evidence[quote["version_id"]]["content"]):
                            lines.append(f"- 原文「{quote['text']}」（版本 {quote['version_id']}）")
        if not valid_conflict:
            status = "partial"
            limitations.append("未提供两个独立来源版本的可定位矛盾引文，不能标记为已发现来源冲突。")
            missing.append("两份独立来源的矛盾原文及矛盾所在条件")
    if mode == "align" and not _independent(accepted, evidence):
        status = "partial"
        if source_boundaries:
            limitations.append('仅说明单份来源明确记载的未确认状态，不代表已完成跨来源核对、差异裁决或最终值审定。')
            missing.append('完成跨来源核对所需的独立来源及核对记录')
            lines = ['当前来源明确记载结果尚未审定、仍须核对，不能据此确认最终值或最终比例。']
            for key, quotes in sorted(source_boundaries.items()):
                lines.extend(f'- 来源明确记载（版本 {key}）：「{quote}」' for quote in quotes)
            accepted = set(source_boundaries)
        else:
            limitations.append("不足两份独立文档/版本支持对比，无法给出确定差异或共识；以下仅为单来源原文。")
            missing.append("至少两份独立且与比较对象相关的来源版本")
            # A disclaimer cannot make a one-source comparison safe.
            lines = [f"- 来源原文（版本 {key}）：{evidence[key]['content']}" for key in sorted(accepted)]
    if semantic_unresolved:
        limitations.append("模型未逐项给出语义支持说明及可定位原文引句，不能仅凭合法引用ID承诺完整回答；尚未核验断言是否由原文推出。")
    if status == "sufficient" and (missing or limitations or semantic_unresolved):
        status = "partial"
    # Legal IDs are provenance checks, not entailment or factual verification.
    limitations.append("来源ID仅核对本次授权证据范围；主题匹配与模型支持说明不等同于语义真值已校验。")
    limitations.append("证据范围仅限本次可访问的检索结果，不代表完整文献或全部事实。")
    if status == "partial" and not missing:
        missing.append("现有来源未覆盖的问题部分、适用条件及完整性尚不能确认")
    heading = {"qa": "回答", "align": "逐来源对照", "progress": "任务记录整理（不代表验收或完成认定）"}[mode]
    actions = ["add_knowledge", "rephrase_question"] if status == "partial" else []
    return AnswerOutcome(heading + "\n" + "\n".join(lines), status,
                         accepted if mode != "progress" else set(),
                         limitations, missing, actions)


def add_disclosures(outcome: AnswerOutcome) -> str:
    parts = [outcome.answer]
    limitations = [item for item in outcome.limitations if item not in outcome.answer]
    if limitations:
        parts.append("限制：\n" + "\n".join("- " + item for item in limitations))
    if outcome.missing_evidence:
        parts.append("尚缺证据：\n" + "\n".join("- " + item for item in outcome.missing_evidence))
    return "\n".join(parts)
