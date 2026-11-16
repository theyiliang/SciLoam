"""Business descriptions of read-only tools, independent of any agent SDK."""
from app.domain.ai_ports import AgentDescription, ToolDescription
from .evidence import _Request
from .policies import _COMMON


def describe_agents(request: _Request) -> dict[str, AgentDescription]:
    specifications = {
        "ReadOnlyRetrievalAgent": (
            (ToolDescription("search_documents", "Search authorized documents in the bound conversation space.", request.search),
             ToolDescription("read_version", "Read a version discovered by this request's authorized search.", request.read_version)),
            "按问题检索并逐来源摘录证据；不要补全证据以外的事实。"),
        "CollaborationAnalysisAgent": (
            (ToolDescription("search_sources", "Search same-space sources for a source-by-source comparison draft.", request.search),
             ToolDescription("read_source_version", "Read a discovered same-space source version.", request.read_version)),
            "给出同空间逐来源差异草稿，分别标明方法、结论、冲突与缺失；不足两份独立文档时说明不能比较。"
            "但可逐字引用单份来源明确记载的尚未确认状态，不将其当作跨源裁决。"
            "来源已明确沿用同批样本时如实陈述，不能弱化成可能共用，也不能重复计数。"),
        "ProgressAgent": (
            (ToolDescription("list_tasks", "Read public goal metadata only (legacy tool name); status is recorded, never inferred completion.", request.list_tasks),),
            "只读展示目标记录的路径、状态与期限；列出缺失记录，不认定completed，不读取私有文档、关联来源或更新正文，不派单。"),
        "ResearchCoordinator": (
            (ToolDescription("read_evidence", "Read only the already discovered and reauthorized evidence ledger.", request.read_evidence),),
            '仅依据当前授权证据回答，不按问题措辞或检索排名补全结论。返回纯JSON：'
            '{"answer_status":"sufficient|partial|insufficient|conflicting",'
            '"claims":[{"text":"来源支持的回答","version_ids":["真实版本ID"],'
            '"task_ids":["真实目标记录ID（兼容字段名）"],"evidence_reason":"具体说明来源内容如何回答当前问题",'
            '"evidence_quotes":[{"version_id":"本claim所用版本ID","text":"可选逐字原文"}]}],'
            '"limitations":["具体限制"],"missing_evidence":["问题中尚无依据的部分"],'
            '"conflicts":[{"explanation":"解释相同条件下哪些陈述互相矛盾，不能只说不同",'
            '"quotes":[{"version_id":"版本1","text":"原文逐字引用"},'
            '{"version_id":"另一独立文档版本","text":"矛盾原文逐字引用"}]}]}。'
            '不要输出Markdown代码围栏或JSON以外文字。evidence_quotes必须逐字复制连续原文，'
            '不得用省略号拼接；需要分开引用时给出多个quote。仅使用answerability中的候选版本，'
            'weak_lexical_match不得引用，即使正文可见也不代表可作证据。'
            '旧claims格式仍可处理但不承诺完整回答。insufficient时claims留空；'
            'partial只回答有依据部分并列出未知。missing_evidence列完成用户请求仍缺的具体数据，'
            '不要写“无缺失”等否定占位；限制、未知和已确认事实分开。'
            'sufficient必须逐项说明支持关系，并为每个所用版本给出evidence_quotes逐字原文。'
            'topic_relevance不是语义真值：weak_lexical_match不得当作问题证据；'
            'needs_semantic_review不能仅因无字面重合拒绝，也不能仅因存在ID接受，须具体判定adequacy并给evidence_reason。'
            'conflicting仅用于两个独立来源的直接矛盾引文，单版本、一般差异或元数据不能建立冲突。'
            'align不足两份独立文档/版本时不得作确定对比；但单份来源明确记载未确认、仍须核对时，'
            '先直接说明不能据此确认最终值或比例，逐字引用该边界，不自行裁定最终统一值。'
            '已由来源明示的事实不要弱化成可能：例如明确沿用同批摘要，就说明不是新增独立样本；'
            '只使用当前阶段授权证据，不引入后续纪要。定义题可简短补充唯一标识去重，不为补充增加工具调用。'
            '历史只供理解追问，不能作为本次证据。'
            '每项claim必须有本次真实版本或（仅progress模式）目标记录ID，兼容使用task_ids字段。不得输出其他来源字段或修改权限。'),
    }
    return {name: AgentDescription(name, _COMMON + instructions, tools)
            for name, (tools, instructions) in specifications.items()}
