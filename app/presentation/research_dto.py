from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    email: str = Field(max_length=254, description="账号邮箱")
    password: str = Field(min_length=1, max_length=1024, description="密码")


class Register(Login):
    invite_token: str = Field(min_length=10, max_length=512, description="负责人发出的邀请凭证")


class ChangePassword(Input):
    current_password: str = Field(min_length=1, max_length=1024, description="当前密码")
    new_password: str = Field(min_length=12, max_length=1024, description="新密码，不能与当前密码相同")


class SpaceInput(Input):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["collaboration"] = "collaboration"
    task_id: str | None = None
    goal_id: str | None = Field(default=None, description="关联课题目标，不改变知识库权限")


class InviteInput(Input):
    email: str = Field(max_length=254)


class AcceptInvite(Input):
    token: str = Field(min_length=10, max_length=512)


class Grant(Input):
    role: Literal["reader", "editor"]


class VersionInput(Input):
    content: str = Field(min_length=1, max_length=2_000_000)
    filename: str = Field(default="", max_length=255)
    source: str = Field(default="", max_length=2000)


class DocumentInput(VersionInput):
    title: str = Field(min_length=1, max_length=300)


class ShareInput(Input):
    target_space_id: str


class DeleteVersion(Input):
    replacement_version_id: str | None = Field(default=None, max_length=128, description="删除当前版本时必须明确选择保留版本")
    reason: str = Field(default="", max_length=2000, description="删除原因")


class WithdrawVersion(Input):
    reason: str = Field(default="", max_length=2000, description="撤回原因，原始版本保留审计")


class ReviewInput(Input):
    version_id: str
    task_id: str | None = None
    goal_id: str | None = Field(default=None, description="关联课题目标，不代表自动验收完成")


class GoalInput(Input):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=10000)
    parent_id: str | None = None
    acceptance_criteria: str = Field(default="", max_length=10000)
    deliverables: str = Field(default="", max_length=10000)
    constraints: str = Field(default="", max_length=10000)
    time_requirement: str = Field(default="", max_length=10000)
    due_date: str | None = None
    position: int = Field(default=0, ge=0, le=10000, strict=True)


class GoalPatch(Input):
    expected_revision: int = Field(ge=1, strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10000)
    parent_id: str | None = None
    acceptance_criteria: str | None = Field(default=None, max_length=10000)
    deliverables: str | None = Field(default=None, max_length=10000)
    constraints: str | None = Field(default=None, max_length=10000)
    time_requirement: str | None = Field(default=None, max_length=10000)
    due_date: str | None = None
    position: int | None = Field(default=None, ge=0, le=10000, strict=True)


class GoalStatus(Input):
    status: Literal["not_started", "in_progress", "pending_acceptance", "completed", "paused"]
    reason: str = Field(min_length=1, max_length=4000)
    expected_revision: int = Field(ge=1, strict=True)


class GoalArchive(Input):
    expected_revision: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=1, max_length=4000)


class GoalKnowledge(Input):
    version_id: str = Field(min_length=1, max_length=128)


class GoalUpdate(Input):
    content: str = Field(min_length=1, max_length=10000)
    version_id: str | None = None


class DecisionInput(Input):
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=4000)


class TaskInput(Input):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=10000)
    assignee_id: str
    due_date: str | None = None


class TaskPatch(Input):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10000)
    assignee_id: str | None = None
    due_date: str | None = None
    status: Literal["not_started", "in_progress", "pending_acceptance", "completed", "paused"] | None = None


class ProgressInput(Input):
    content: str = Field(min_length=1, max_length=10000)
    version_id: str | None = None


class ConversationInput(Input):
    space_id: str


class GoalDraftInput(Input):
    version_ids: list[str] = Field(min_length=1, max_length=5)
    method: Literal['rule', 'ai'] = Field(default='rule', description='仅rule可用：本地原文辅助提取，需人工核对；ai请求明确拒绝')

    def require_local_method(self):
        if self.method == 'ai':
            from app.domain.errors import DomainError
            raise DomainError(422, 'AI goal generation/regeneration is paused; use method=rule for local original-text assistance and verify manually')


class GoalDraftRegenerate(GoalDraftInput):
    expected_revision: int | None = Field(default=None, ge=1, strict=True)


class GoalDraftAdopt(Input):
    suggestion_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1, strict=True)
    expected_goal_revision: int | None = Field(default=None, ge=1, strict=True)
    confirmed: bool = Field(strict=True)


class GoalDraftReject(Input):
    suggestion_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1, strict=True)
    confirmed: bool = Field(strict=True)
    reason: str = Field(default='', max_length=4000)


class GoalDraftPatch(Input):
    expected_revision: int = Field(ge=1, strict=True)
    nodes: list[dict] = Field(max_length=30)


class GoalDraftPublish(Input):
    expected_revision: int = Field(ge=1, strict=True)
    confirmed: bool = Field(strict=True)


class ChatInput(ConversationInput):
    conversation_id: str
    question: str = Field(min_length=1, max_length=8000)
    mode: Literal["qa", "align", "progress"] = "qa"
    selected_version_id: str | None = Field(default=None, min_length=1, max_length=128, description="用户明确选择的材料版本；不指定时不隐式选择文档")


class ChatRunInput(ChatInput):
    request_id: str | None = Field(default=None, min_length=1, max_length=200)

