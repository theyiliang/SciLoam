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
            "给出同空间逐来源差异草稿，分别标明方法、结论、冲突与缺失；不足两份独立文档时说明不能比较。"),
        "ProgressAgent": (
            (ToolDescription("list_tasks", "Read task metadata only; status is recorded, never inferred completion.", request.list_tasks),),
            "只读展示任务记录状态、期限与负责人；列出缺失记录，不认定completed，不读取文档或进度正文。"),
        "ResearchCoordinator": (
            (ToolDescription("read_evidence", "Read only the already discovered and reauthorized evidence ledger.", request.read_evidence),),
            '仅汇总被路由专家的草稿并校核证据。返回纯JSON：'
            '{"claims":[{"text":"结论或逐来源差异草稿","version_ids":["真实版本ID"],'
            '"task_ids":["真实任务ID"]}],"limitations":["局限"]}。'
            '每项claim必须有本次真实版本或（仅progress模式）任务ID。不得输出其他来源字段。'),
    }
    return {name: AgentDescription(name, _COMMON + instructions, tools)
            for name, (tools, instructions) in specifications.items()}
