"""Independent, provider-neutral goal extraction event contract (not chat)."""
from dataclasses import dataclass
from typing import Any, Mapping

EVENT_TYPES = frozenset({
    'goal.started', 'sources.validation.started', 'sources.validation.completed',
    'sources.read.started', 'sources.read.completed', 'generation.started', 'generation.completed',
    'evidence.validation.started', 'evidence.validation.completed',
    'draft.save.started', 'draft.save.completed', 'goal.completed', 'goal.failed', 'goal.cancelled',
})
TERMINAL_EVENTS = frozenset({'goal.completed', 'goal.failed', 'goal.cancelled'})


@dataclass(frozen=True)
class GoalGenerationEvent:
    run_id: str
    seq: int
    timestamp: str
    type: str
    actor_id: str
    space_id: str
    conversation_id: str
    request_id: str | None
    data: Mapping[str, Any]

    def as_dict(self):
        return {'run_id': self.run_id, 'request_id': self.request_id, 'seq': self.seq,
                'timestamp': self.timestamp, 'type': self.type,
                'version_ids': list(self.data.get('version_ids', [])), 'data': dict(self.data)}


FAILURES = {
    'no_extractable_goals': '所选原文未识别到可提取目标，请补充标题或编号目标后重试。',
    'invalid_evidence': '生成内容未通过原文证据校验，未保存草稿；请检查来源或重试。',
    'model_unavailable': '目标生成模型暂不可用，未保存草稿，请稍后重试。',
    'source_changed': '来源版本、权限或登录状态已变化，已停止生成；请重新选择有效课题知识。',
    'execution_deadline': '生成等待超时，已停止且未保存未完成草稿。',
    'backpressure': '事件消费者无法继续接收，已停止生成。',
    'run_unavailable': '目标生成暂不可用，未保存草稿，请稍后重试。',
    'cancelled': '已取消目标提取，未保存未完成草稿。',
    'consumer_timeout': '连接租约已到期，已停止目标提取。',
    'shutdown': '服务已停止本次目标提取。',
}
