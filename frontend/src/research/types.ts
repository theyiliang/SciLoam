export type User = { id: string; email: string; role: "owner" | "member"; name?: string };
export type Space = { id: string; name: string; kind: "basis" | "private" | "collaboration" | "results"; owner_id: string; epoch?: number; role: "owner" | "editor" | "reader"; task_id?: string; goal_id?: string };
export type Session = { user: User | null; project?: { id: string; name?: string }; csrf_token?: string };
export type Version = { id: string; number?: number; content?: string; filename?: string; source?: string; space_id?: string; index_status?: string; created_at?: string | number; document_id?: string; title?: string; lifecycle_status?: "active" | "deleted" | "withdrawn"; protected?: boolean; is_current?: boolean; updated_at?: string | number };
export type Document = { id: string; title: string; space_id?: string; source?: string; filename?: string; created_by?: string; latest_version?: Version; versions?: Version[]; created_at?: string | number; active_version?: Version | null; updated_at?: string | number };
export type Task = { id: string; title: string; description?: string; assignee_id?: string; creator_id?: string; created_by?: string; due_date?: string; status: "not_started" | "in_progress" | "pending_acceptance" | "completed" | "paused" };
export type GoalStatus = Task["status"];
export type Goal = { id: string; parent_id: string | null; title: string; description: string; acceptance_criteria: string; deliverables?: string; constraints?: string; time_requirement?: string; due_date: string | null; status: GoalStatus; position: number; revision: number; can_manage: boolean; can_contribute: boolean; path_titles: string[]; path: string; archived_at: string | number | null; archived_reason?: string; legacy_task_id?: string | null };
export type GoalDraftSource = { version_id: string; document_id: string; space_id: string; number: number; content_hash?: string; version_hash?: string };
export type GoalFieldSource = { version_id: string; quote: string; start: number; end: number };
export type GoalDraftField = "title" | "description" | "acceptance_criteria" | "due_date" | "deliverables" | "constraints" | "time_requirement";
export type GoalDraftNode = { id: string; parent_id: string | null; title: string; description: string; acceptance_criteria: string; due_date: string | null; deliverables: string; constraints: string; time_requirement: string; position: number; origin: "explicit" | "suggested"; generation_method: "rule" | "ai" | "manual"; field_sources: Partial<Record<GoalDraftField, GoalFieldSource[]>>; source: GoalFieldSource | null };
export type GoalDraftSuggestion = { before: Goal | null; id: string; operation: "add" | "modify" | "move" | "retire"; node: GoalDraftNode; target_goal_id: string | null; expected_goal_revision: number | null; status: "pending" | "adopted" | "rejected" };
export type GoalDraft = { id: string; created_by: string; status: "draft" | "published" | "abandoned"; revision: number; sources: GoalDraftSource[]; sources_hash: string; base_tree_hash: string; nodes: GoalDraftNode[]; method: "extractive" | "ai"; limitations: string[]; publication_readiness: { publishable: boolean; reasons: string[] }; generation: { method: "rule" | "ai"; provider?: string; model?: string }; suggestions: GoalDraftSuggestion[]; regenerates_id?: string | null; created_at?: string | number; updated_at?: string | number; published_at?: string | number };
export type GoalDraftReceipt = { draft_id: string; goal_ids: string[]; node_id_mapping?: Record<string, string>; published_at?: string | number };
export type ImportReport = { origin_filename?: string; format?: string; paragraph_count?: number; warnings: string[]; page_support?: boolean; positions?: { paragraph: number; start: number; end: number }[] };
export type KnowledgeImport = (Document & { import_report?: ImportReport }) | { document: Document; report?: ImportReport };
export type GoalCitation = { version_id: string; document_id: string; space_id: string; title: string; number: number; source?: string };
export type GoalKnowledge = GoalCitation & { id: string; goal_id: string; linked_by: string; created_at?: string | number };
export type GoalUpdate = { id: string; goal_id: string; author_id: string; content: string; version_id: string | null; created_at?: string | number; citation?: GoalCitation };
export type GoalStatusHistory = { id: string; goal_id: string; actor_id: string; from_status: GoalStatus; to_status: GoalStatus; reason: string; created_at?: string | number };
export type GoalGenerationSource = { current_revision?: number; current_differs_from_confirmation?: boolean; source_details_redacted?: boolean; id: string; goal_id: string; draft_id: string; node_id: string; node: GoalDraftNode; generation: GoalDraft["generation"]; confirmation: { actor_id: string; confirmed: true; draft_revision: number; goal_revision?: number; confirmed_at: string | number }; operation: GoalDraftSuggestion["operation"]; created_at?: string | number };
export type GoalDetail = Goal & { knowledge: GoalKnowledge[]; updates: GoalUpdate[]; status_history: GoalStatusHistory[]; generation_sources?: GoalGenerationSource[] };
export type Progress = { id: string; content: string; version_id?: string; user_id?: string; created_at?: string | number };
export type Review = { id: string; version_id: string; task_id?: string; goal_id?: string | null; goal_title?: string | null; goal_path?: string | null; goal_archived?: boolean | null; submitted_by?: string; submitter_id?: string; user_id?: string; created_by?: string; status?: string; decision?: string; comment?: string; created_at?: string | number; document_title?: string; title?: string; number?: number; source_space_kind?: Space["kind"]; mode?: "snapshot"; published_version_id?: string | null; target_visibility?: "results"; can_review?: boolean; can_decide?: boolean; submitted_at?: string | number };
export type ReviewSnapshot = Review & { content?: string; source?: string; filename?: string };
export type Member = { id?: string; user_id?: string; email?: string; name?: string; role?: string; disabled_at?: string | number | null; active?: boolean; user?: User & { disabled_at?: string | number | null; active?: boolean } };
export type AnswerStatus = "sufficient" | "partial" | "insufficient" | "conflicting";
export type AnswerKind = "general" | "grounded" | "mixed" | "clarification" | "failure";
export type ClarificationKind = "document" | "scope" | "comparison" | "other";
export type SuggestedAction = string | { action?: string; type?: string; label?: string };
export type Citation = { id?: string; title?: string; document_title?: string; version_id?: string; content?: string; excerpt?: string; source?: string; url?: string; filename?: string; page?: number; snippet?: string; number?: number; version_number?: number; chunk_start?: number; chunk_end?: number; char_start?: number; char_end?: number; document_id?: string };
export type Message = { id?: string; role: string; content?: string; text?: string; answer?: string; citations?: Citation[]; events?: unknown[]; fallback?: boolean | string; answer_kind?: AnswerKind; clarification_kind?: ClarificationKind; answer_status?: AnswerStatus; missing_evidence?: string[] | string; suggested_actions?: SuggestedAction[] };
export type Conversation = { id: string; title?: string; space_id?: string; created_at?: string | number; messages?: Message[] };
export type ChatReply = { message_id: string; answer: string; citations?: Citation[]; events?: unknown[]; conversation_id: string; fallback?: boolean | string; answer_kind?: AnswerKind; clarification_kind?: ClarificationKind; answer_status?: AnswerStatus; missing_evidence?: string[] | string; suggested_actions?: SuggestedAction[] };
export type ChatAnswerInput = { space_id: string; conversation_id: string; question: string; mode: string; selected_version_id?: string };
export type ChatRunIdentity = { space_id: string; conversation_id: string; request_id: string };
export type ChatRunInput = ChatAnswerInput & { request_id: string };
export type MaterialOption = { version_id: string; document_id: string; title: string; number?: number; current: boolean };
// Only explicitly selectable, active versions; never infer a document from list order.
export function materialOptions(documents: Document[]): MaterialOption[] {
  return documents.flatMap(doc => {
    const current = doc.active_version !== undefined ? doc.active_version : doc.latest_version;
    const versions = doc.versions?.length ? doc.versions : current ? [current] : [];
    return versions.filter(version => (!version.lifecycle_status || version.lifecycle_status === "active") && !!version.id).map(version => ({ version_id: version.id, document_id: doc.id, title: doc.title, number: version.number, current: version.id === current?.id }));
  });
}
export type ChatRun = ChatRunIdentity & { run_id: string; message_id: string; status: "queued" | "running" | "completed" | "cancelled" | "failed" };
export type ChatRunEvent = { run_id: string; seq: number; timestamp: string; type: "document.read" | "run.started" | "retrieval.started" | "retrieval.completed" | "retrieval.skipped" | "retrieval.degraded" | "rerank.started" | "rerank.completed" | "rerank.skipped" | "rerank.degraded" | "agent.started" | "agent.completed" | "answer.delta" | "answer.final" | "run.failed" | "run.cancelled" | "run.completed"; data: Record<string, unknown> };
export const kinds: Record<Space["kind"], string> = { basis: "项目知识库", private: "个人知识库", collaboration: "协作知识库", results: "成果知识库" };
// Presentation only: never rename a saved space or replace a user's custom name.
export const spaceName = (space: Space) => ({ basis: "Basis", private: "Private", results: "Results" } as Partial<Record<Space["kind"], string>>)[space.kind] === space.name ? kinds[space.kind] : space.name;
// Show the kind separately only when it adds information to the displayed name.
export const spaceKindLabel = (space: Space) => spaceName(space) === kinds[space.kind] ? "" : kinds[space.kind];
export const spaceRoles: Record<string, string> = { owner: "空间所有者", editor: "可编辑", reader: "只读" };
// Content-only summaries for fixed libraries; permission wording stays out of the sidebar.
export const spaceSummary = (space: Space) => ({ basis: "项目立项依据与正式资料", private: "个人笔记与资料", results: "审核通过的成果副本" } as Partial<Record<Space["kind"], string>>)[space.kind] ?? "";
export const canManageSpace = (space: Space, user: User) => (space.kind === "private" || space.kind === "collaboration") && space.owner_id === user.id && space.role === "owner";
export const activeMember = (member: Member) => member.disabled_at == null && member.user?.disabled_at == null && member.active !== false && member.user?.active !== false;
export const passwordError = (password: string, confirmation: string) => password.length < 12 ? "密码至少需要12个字符。" : password !== confirmation ? "两次密码不一致，请检查。" : "";
export type PasswordChange = { password_changed: boolean; reauthenticate: boolean };
export const statuses: Record<Task["status"], string> = { not_started: "未开始", in_progress: "进行中", pending_acceptance: "待验收", completed: "已完成", paused: "已暂停" };
export const roles: Record<string, string> = { owner: "项目负责人", editor: "可编辑", reader: "只读", member: "项目成员" };
export const memberId = (m: Member) => m.user_id ?? m.user?.id ?? m.id ?? "";
export const memberName = (m: Member) => m.email ?? m.user?.email ?? m.name ?? memberId(m);
export const writable = (space: Space, user: User) => space.role === "owner" || space.role === "editor" || (space.kind === "private" && space.owner_id === user.id);
export const dateText = (value?: string | number) => value ? new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value).toLocaleString("zh-CN") : "—";
