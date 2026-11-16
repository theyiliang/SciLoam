import { useEffect, useRef, useState } from "react";
import { api, ApiError, errorText, idPath, listOf } from "./api";
import type { Document, Goal, GoalDetail, GoalStatus, Space, User, Version } from "./types";
import { dateText, kinds, spaceName, statuses } from "./types";
import { Empty, Feedback, PanelHeading, SourceLink } from "./ui";

type Props = { user: User };
type GoalForm = { id?: string; revision?: number; title: string; description: string; acceptance_criteria: string; deliverables: string; constraints: string; time_requirement: string; parent_id: string; due_date: string; position: string };
const nextStatuses: Record<GoalStatus, GoalStatus[]> = { not_started: ["in_progress", "paused"], in_progress: ["pending_acceptance", "paused"], pending_acceptance: ["completed", "in_progress"], paused: ["in_progress"], completed: ["in_progress"] };
const blankForm = (parent_id = ""): GoalForm => ({ title: "", description: "", acceptance_criteria: "", deliverables: "", constraints: "", time_requirement: "", parent_id, due_date: "", position: "0" });
const versionsOf = (doc: Document): Version[] => (doc.versions?.length ? doc.versions : (doc.active_version ?? doc.latest_version) ? [doc.active_version ?? doc.latest_version!] : []).filter(version => !version.lifecycle_status || version.lifecycle_status === "active");
export const goalPath = (goal: Goal) => goal.path || goal.path_titles?.join(" / ") || goal.title;
export function goalDescendants(goals: Goal[], id: string): Set<string> {
  const descendants = new Set([id]);
  let changed = true;
  while (changed) { changed = false; for (const goal of goals) if (goal.parent_id && descendants.has(goal.parent_id) && !descendants.has(goal.id)) { descendants.add(goal.id); changed = true; } }
  return descendants;
}
export function visibleGoals(goals: Goal[], search: string, collapsed: Set<string>): Goal[] {
  const query = search.trim().toLocaleLowerCase();
  const lookup = new Map(goals.map(goal => [goal.id, goal]));
  const included = new Set<string>();
  if (query) for (const goal of goals) if (`${goalPath(goal)} ${goal.description} ${goal.acceptance_criteria}`.toLocaleLowerCase().includes(query)) {
    let current: Goal | undefined = goal;
    const seen = new Set<string>();
    while (current && !seen.has(current.id)) { seen.add(current.id); included.add(current.id); current = current.parent_id ? lookup.get(current.parent_id) : undefined; }
  }
  return goals.filter(goal => {
    if (query) return included.has(goal.id);
    let current = goal.parent_id ? lookup.get(goal.parent_id) : undefined;
    const seen = new Set<string>();
    while (current && !seen.has(current.id)) { if (collapsed.has(current.id)) return false; seen.add(current.id); current = current.parent_id ? lookup.get(current.parent_id) : undefined; }
    return true;
  });
}

export default function Goals({ user }: Props) {
  const [goals, setGoals] = useState<Goal[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<GoalDetail | null>(null);
  const [search, setSearch] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [form, setForm] = useState<GoalForm | null>(null);
  const [nextStatus, setNextStatus] = useState<GoalStatus>("in_progress");
  const [reason, setReason] = useState("");
  const [archiveReason, setArchiveReason] = useState("");
  const [content, setContent] = useState("");
  const [picker, setPicker] = useState<"knowledge" | "update" | null>(null);
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [spaceId, setSpaceId] = useState("");
  const [documents, setDocuments] = useState<Document[]>([]);
  const [documentId, setDocumentId] = useState("");
  const [versionId, setVersionId] = useState("");
  const [loading, setLoading] = useState(true);
  const [reading, setReading] = useState(false);
  const [pickerLoading, setPickerLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const scope = `${user.id}:${user.role}`;
  const scopeRef = useRef(scope), generation = useRef(0), selectionSeq = useRef(0), listSeq = useRef(0), pickerSeq = useRef(0);
  const selectedRef = useRef("");
  const lock = useRef<object | null>(null);
  const [loadedScope, setLoadedScope] = useState(scope);
  if (scopeRef.current !== scope) { scopeRef.current = scope; generation.current++; selectionSeq.current++; listSeq.current++; pickerSeq.current++; selectedRef.current = ""; }
  const ready = loadedScope === scope;
  const current = ready && detail?.id === selectedId ? detail : null;
  const owner = user.role === "owner";
  const canManage = owner && !!current?.can_manage && !current.archived_at;
  const canContribute = !!current?.can_contribute && current.status !== "completed" && !current.archived_at;
  const valid = (epoch: number) => scopeRef.current === scope && generation.current === epoch;
  const selectionValid = (epoch: number, request: number, id: string) => valid(epoch) && selectionSeq.current === request && selectedRef.current === id;
  const resetPicker = () => { pickerSeq.current++; setPicker(null); setSpaces([]); setSpaceId(""); setDocuments([]); setDocumentId(""); setVersionId(""); setPickerLoading(false); };
  const resetEditors = () => { setForm(null); setReason(""); setArchiveReason(""); setContent(""); resetPicker(); };
  const fetchList = async (epoch: number) => {
    const request = ++listSeq.current;
    const items = listOf<Goal>(await api("/goals"), "goals");
    if (!valid(epoch) || request !== listSeq.current) return null;
    setGoals(items); return items;
  };
  const fetchDetail = async (id: string, epoch: number, request: number) => {
    const value = await api<GoalDetail>(`/goals/${idPath(id)}`);
    if (!selectionValid(epoch, request, id)) return;
    if (value.id !== id) throw new Error("目标详情不一致，请刷新后重试。");
    setDetail(value); setNextStatus(value.status);
  };
  useEffect(() => {
    const epoch = ++generation.current;
    selectedRef.current = ""; selectionSeq.current++; pickerSeq.current++; lock.current = null;
    setLoadedScope(scope); setGoals([]); setSelectedId(""); setDetail(null); resetEditors(); setConflict(false); setError(""); setNotice(""); setBusy(false); setLoading(true); setReading(false); setSearch(""); setCollapsed(new Set());
    void fetchList(epoch).catch(e => { if (valid(epoch)) setError(errorText(e)); }).finally(() => { if (valid(epoch)) setLoading(false); });
    return () => { generation.current++; selectionSeq.current++; listSeq.current++; pickerSeq.current++; lock.current = null; };
  }, [scope]);
  const open = async (goal: Goal) => {
    if (busy) return;
    const epoch = generation.current, request = ++selectionSeq.current;
    selectedRef.current = goal.id; setSelectedId(goal.id); setDetail(null); setReading(true); resetEditors(); setConflict(false); setError(""); setNotice("");
    try { await fetchDetail(goal.id, epoch, request); }
    catch (e) { if (selectionValid(epoch, request, goal.id)) setError(errorText(e)); }
    finally { if (selectionValid(epoch, request, goal.id)) setReading(false); }
  };
  const run = async (operation: (epoch: number, request: number, id: string) => Promise<void>, success: string) => {
    if (lock.current || !ready || conflict) return;
    const token = {}; lock.current = token;
    const epoch = generation.current, request = selectionSeq.current, id = selectedRef.current;
    setBusy(true); setError(""); setNotice("");
    try { await operation(epoch, request, id); if (selectionValid(epoch, request, id)) setNotice(success); }
    catch (e) { if (selectionValid(epoch, request, id)) { if (e instanceof ApiError && e.status === 409) { setConflict(true); setError(`${errorText(e)} 请手动刷新目标，核对最新状态与版本；不会自动覆盖你的填写内容。子目标未全部完成时不能确认父目标完成，有未归档子目标时不能归档。`); } else setError(errorText(e)); } }
    finally { if (lock.current === token) { lock.current = null; if (valid(epoch)) setBusy(false); } }
  };
  const refresh = async () => {
    if (busy || (form && !window.confirm("刷新将放弃当前目标表单的未保存修改，读取最新目标。确认刷新？"))) return;
    const epoch = generation.current, request = ++selectionSeq.current, id = selectedRef.current;
    resetEditors(); setConflict(false); setError(""); setNotice(""); setLoading(true); setReading(!!id); setDetail(null);
    try { await fetchList(epoch); if (id && selectionValid(epoch, request, id)) await fetchDetail(id, epoch, request); }
    catch (e) { if (selectionValid(epoch, request, id)) setError(errorText(e)); }
    finally { if (valid(epoch)) setLoading(false); if (selectionValid(epoch, request, id)) setReading(false); }
  };
  const afterMutation = async (epoch: number, request: number, id: string) => {
    if (!selectionValid(epoch, request, id)) return;
    await fetchList(epoch);
    if (selectionValid(epoch, request, id)) await fetchDetail(id, epoch, request);
  };
  const beginForm = (value?: GoalDetail, parent = "") => {
    resetEditors(); setError(""); setNotice("");
    setForm(value ? { id: value.id, revision: value.revision, title: value.title, description: value.description, acceptance_criteria: value.acceptance_criteria, deliverables: value.deliverables ?? "", constraints: value.constraints ?? "", time_requirement: value.time_requirement ?? "", parent_id: value.parent_id ?? "", due_date: value.due_date?.slice(0, 10) ?? "", position: String(value.position) } : blankForm(parent));
  };
  const saveGoal = () => {
    if (!owner || !form || !form.title.trim() || !Number.isSafeInteger(Number(form.position)) || Number(form.position) < 0 || Number(form.position) > 10000 || (form.id && !canManage)) return;
    if (!window.confirm("目标标题、描述与验收标准对项目成员可读。确认保存？请勿写入私有知识正文。")) return;
    const submitted = form;
    void run(async (epoch, request, id) => {
      const body = { title: submitted.title.trim(), description: submitted.description.trim(), acceptance_criteria: submitted.acceptance_criteria.trim(), deliverables: submitted.deliverables.trim(), constraints: submitted.constraints.trim(), time_requirement: submitted.time_requirement.trim(), parent_id: submitted.parent_id || null, due_date: submitted.due_date || null, position: Number(submitted.position), ...(submitted.id ? { expected_revision: submitted.revision } : {}) };
      const saved = await api<GoalDetail>(submitted.id ? `/goals/${idPath(submitted.id)}` : "/goals", submitted.id ? "PATCH" : "POST", body);
      if (!selectionValid(epoch, request, id)) return;
      await fetchList(epoch);
      if (!selectionValid(epoch, request, id)) return;
      setForm(null); selectedRef.current = saved.id; setSelectedId(saved.id); setDetail(saved); setNextStatus(saved.status); setNotice("目标已保存。目标不派单，完成情况由负责人依据验收标准确认。");
    }, "");
  };
  const confirmStatus = () => {
    if (!current || !canManage || !reason.trim() || !nextStatuses[current.status].includes(nextStatus)) return;
    if (!window.confirm(`确认人工将「${current.title}」标记为「${statuses[nextStatus]}」？\n请依据验收标准与实际证据判断，不以关联知识数量计算完成度。审核通过不会自动完成目标。所有未归档子目标须先完成，才能完成父目标。`)) return;
    const revision = current.revision;
    void run(async (epoch, request, id) => { await api(`/goals/${idPath(id)}/status`, "POST", { status: nextStatus, reason: reason.trim(), expected_revision: revision }); if (!selectionValid(epoch, request, id)) return; setReason(""); await afterMutation(epoch, request, id); }, "阶段状态已由负责人手动确认，依据已保留在状态历史中。");
  };
  const archive = () => {
    if (!current || !canManage || !archiveReason.trim()) return;
    if (!window.confirm(`确认归档「${current.title}」？\n目标将退出活跃目标树，保留历史，不代表完成；必须先处理所有未归档子目标。`)) return;
    const revision = current.revision;
    void run(async (epoch, request, id) => { await api(`/goals/${idPath(id)}/archive`, "POST", { expected_revision: revision, reason: archiveReason.trim() }); if (!selectionValid(epoch, request, id)) return; setArchiveReason(""); resetPicker(); await afterMutation(epoch, request, id); }, "目标已归档，历史仍保留；归档不代表完成。");
  };
  const openPicker = async (mode: "knowledge" | "update") => {
    if (!canContribute || busy) return;
    resetPicker(); setPicker(mode); setError("");
    const epoch = generation.current, selected = selectionSeq.current, id = selectedRef.current, request = ++pickerSeq.current;
    setPickerLoading(true);
    try { const items = listOf<Space>(await api("/spaces"), "spaces"); if (selectionValid(epoch, selected, id) && request === pickerSeq.current) setSpaces(items); }
    catch (e) { if (selectionValid(epoch, selected, id) && request === pickerSeq.current) setError(errorText(e)); }
    finally { if (selectionValid(epoch, selected, id) && request === pickerSeq.current) setPickerLoading(false); }
  };
  const chooseSpace = async (value: string) => {
    const epoch = generation.current, selected = selectionSeq.current, id = selectedRef.current, request = ++pickerSeq.current;
    setSpaceId(value); setDocuments([]); setDocumentId(""); setVersionId(""); setPickerLoading(!!value); setError("");
    if (!value) return;
    try { const items = listOf<Document>(await api(`/spaces/${idPath(value)}/documents`), "documents"); if (selectionValid(epoch, selected, id) && request === pickerSeq.current) setDocuments(items); }
    catch (e) { if (selectionValid(epoch, selected, id) && request === pickerSeq.current) setError(errorText(e)); }
    finally { if (selectionValid(epoch, selected, id) && request === pickerSeq.current) setPickerLoading(false); }
  };
  const chosenDocument = documents.find(doc => doc.id === documentId);
  const chosenVersion = chosenDocument && versionsOf(chosenDocument).find(version => version.id === versionId);
  const chosenSpace = spaces.find(space => space.id === spaceId);
  const contribute = (mode: "knowledge" | "update") => {
    if (!canContribute || (mode === "knowledge" && !chosenVersion) || (mode === "update" && !content.trim())) return;
    if (mode === "update" && picker === "update" && !chosenVersion) return;
    const version_id = (mode === "knowledge" || picker === "update") ? chosenVersion?.id : undefined;
    if (!window.confirm(mode === "knowledge" ? `将「${chosenDocument?.title} · v${chosenVersion?.number ?? "—"}」关联到目标？\n${chosenSpace?.kind === "private" ? "这是个人知识库版本。" : ""}关联不授予其他成员源知识访问权限，也不代表目标完成。` : "确认记录进展？不附版本的进展正文对全体项目成员可读；附版本的进展按源版本授权显示。请勿将私有内容写入公开描述或进展正文。关联版本不授予其他成员访问权限。")) return;
    void run(async (epoch, request, id) => {
      await api(`/goals/${idPath(id)}/${mode === "knowledge" ? "knowledge" : "updates"}`, "POST", mode === "knowledge" ? { version_id } : { content: content.trim(), ...(version_id ? { version_id } : {}) });
      if (!selectionValid(epoch, request, id)) return;
      if (mode === "update") setContent(""); resetPicker(); await afterMutation(epoch, request, id);
    }, mode === "knowledge" ? "已关联指定版本；未更改源知识权限或目标状态。" : "进展已记录；目标状态未自动改变。");
  };
  const unlink = (linkId: string) => {
    const link = current?.knowledge.find(item => item.id === linkId);
    if (!canContribute || !link || (!owner && link.linked_by !== user.id) || !window.confirm("确认取消此知识关联？不会删除知识原文，也不会改变目标状态。")) return;
    void run(async (epoch, request, id) => { await api(`/goals/${idPath(id)}/knowledge/${idPath(linkId)}`, "DELETE"); await afterMutation(epoch, request, id); }, "关联已取消，源知识与版本不受影响。");
  };
  const blocked = busy || conflict || reading || loading || !ready;
  const excludedParents = form?.id ? goalDescendants(goals, form.id) : new Set<string>();
  const tree = visibleGoals(ready ? goals : [], search, collapsed);
  const hasChildren = (id: string) => goals.some(goal => goal.parent_id === id);
  const versionPicker = picker && <div className="rw-phase6-picker"><h4>{picker === "knowledge" ? "选择要关联的知识版本" : "选择进展参考版本（可选）"}</h4><p className="rw-phase6-note">仅列出你当前有权访问的知识。关联私有版本不会授权给其他成员。</p><label>知识库<select aria-label="目标知识库" value={spaceId} disabled={busy || pickerLoading} onChange={e => void chooseSpace(e.target.value)}><option value="">请选择知识库</option>{spaces.map(space => <option key={space.id} value={space.id}>{spaceName(space)} · {kinds[space.kind]}</option>)}</select></label><label>知识<select aria-label="目标知识" value={documentId} disabled={busy || pickerLoading || !spaceId} onChange={e => { setDocumentId(e.target.value); setVersionId(""); }}><option value="">请选择知识</option>{documents.filter(doc => versionsOf(doc).length).map(doc => <option key={doc.id} value={doc.id}>{doc.title}</option>)}</select></label><label>具体版本<select aria-label="目标知识版本" value={versionId} disabled={busy || pickerLoading || !chosenDocument} onChange={e => setVersionId(e.target.value)}><option value="">请选择 vN 版本</option>{chosenDocument && versionsOf(chosenDocument).map(version => <option key={version.id} value={version.id}>v{version.number ?? "—"}{version.is_current ? " · 当前" : ""} · {dateText(version.created_at)}</option>)}</select></label>{pickerLoading && <p role="status">正在读取已授权知识…</p>}<div className="rw-actions">{picker === "knowledge" && <button type="button" className="rw-primary" disabled={blocked || !chosenVersion} onClick={() => contribute("knowledge")}>确认关联所选版本</button>}<button type="button" disabled={busy} onClick={resetPicker}>{picker === "update" ? "不附版本" : "取消关联选择"}</button></div></div>;
  if (!ready) return <section className="rw-phase6"><p role="status">正在加载项目目标…</p></section>;
  return <section className="rw-phase6" aria-busy={busy}>
    <PanelHeading title="任务管理" subtitle={owner ? "围绕目标查看知识证据与阻碍，不需要给成员逐项派单。" : "按研究目标关联知识、自主提交进展，不是负责人派发的任务。"}><button disabled={busy || loading} onClick={() => void refresh()}>刷新目标</button>{owner && <button className="rw-primary" disabled={blocked} onClick={() => beginForm()}>＋ 添加目标</button>}</PanelHeading>
    <Feedback error={error} />
    {notice && <p className="rw-goal-status" role="status">{notice}</p>}
    {conflict && <div className="rw-alert rw-warning">未自动重试或覆盖，请使用“刷新目标”核对后再操作。</div>}
    <div className="rw-phase6-layout">
      <aside className="rw-phase6-tree" aria-label="项目目标导航"><div className="rw-phase6-tree-head"><h3>目标导航</h3><p>自上而下理解研究目标与验收要求。</p><label>搜索目标<input aria-label="搜索目标" value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索目标、描述或验收标准" /></label><div className="rw-actions"><button onClick={() => setCollapsed(new Set())}>全部展开</button><button onClick={() => setCollapsed(new Set(goals.filter(goal => hasChildren(goal.id)).map(goal => goal.id)))}>全部折叠</button></div></div>
        {loading ? <p role="status">正在加载目标树…</p> : tree.length ? <ul className="rw-phase6-nodes">{tree.map(goal => <li key={goal.id} className={selectedId === goal.id ? "selected" : ""} style={{ paddingLeft: `${Math.max(0, (goal.path_titles?.length ?? 1) - 1) * 18}px` }}><div className="rw-phase6-node-row">{hasChildren(goal.id) ? <button className="rw-phase6-toggle" aria-label={`${collapsed.has(goal.id) ? "展开" : "折叠"}${goal.title}`} aria-expanded={!!search.trim() || !collapsed.has(goal.id)} onClick={() => setCollapsed(previous => { const next = new Set(previous); if (next.has(goal.id)) next.delete(goal.id); else next.add(goal.id); return next; })}>{collapsed.has(goal.id) && !search.trim() ? "＋" : "−"}</button> : <span className="rw-phase6-leaf" aria-hidden="true">·</span>}<button className="rw-phase6-node" aria-current={selectedId === goal.id ? "true" : undefined} disabled={busy} onClick={() => void open(goal)}><strong>{goal.title}</strong><span><span className="rw-badge">{statuses[goal.status]}</span>{goal.due_date && <small>截止 {goal.due_date.slice(0, 10)}</small>}</span></button></div></li>)}</ul> : <Empty>{goals.length ? "没有匹配的目标。" : "还没有项目目标，由负责人添加研究目标与验收要求。"}</Empty>}
      </aside>
      <article className="rw-phase6-detail" aria-label="目标详情">{form ? <><p className="rw-eyebrow">{form.id ? "编辑目标" : "添加目标"}</p><h3>从清晰的验收目标开始</h3><form className="rw-form" onSubmit={e => { e.preventDefault(); saveGoal(); }}><label>目标标题<input aria-label="目标标题" value={form.title} required maxLength={500} disabled={blocked} onChange={e => setForm({ ...form, title: e.target.value })} /></label><label>目标描述<textarea aria-label="目标描述" rows={4} maxLength={20000} value={form.description} disabled={blocked} onChange={e => setForm({ ...form, description: e.target.value })} /></label><label>验收标准<textarea aria-label="验收标准" rows={4} maxLength={20000} value={form.acceptance_criteria} disabled={blocked} onChange={e => setForm({ ...form, acceptance_criteria: e.target.value })} placeholder="明确交付要求、验证方法与判断依据，不以文档数量替代验收。" /></label><label>时间要求（原文）<textarea aria-label="目标时间要求原文" rows={3} maxLength={20000} value={form.time_requirement} disabled={blocked} onChange={e => setForm({ ...form, time_requirement: e.target.value })} /><small>保留原文相对时间要求，不推算成截止日期。</small></label><label>交付物<textarea aria-label="目标交付物" rows={3} maxLength={20000} value={form.deliverables} disabled={blocked} onChange={e => setForm({ ...form, deliverables: e.target.value })} /></label><label>约束<textarea aria-label="目标约束" rows={3} maxLength={20000} value={form.constraints} disabled={blocked} onChange={e => setForm({ ...form, constraints: e.target.value })} /></label><p className="rw-phase6-note">目标标题、描述与验收标准对项目成员可读，请勿填写私有内容。目标不要求指定执行人。</p><label>上级目标<select aria-label="上级目标" value={form.parent_id} disabled={blocked} onChange={e => setForm({ ...form, parent_id: e.target.value })}><option value="">顶层目标</option>{goals.filter(goal => !excludedParents.has(goal.id)).map(goal => <option key={goal.id} value={goal.id}>{goalPath(goal)}</option>)}</select></label><div className="rw-form-grid"><label>截止日期（可选）<input aria-label="目标截止日期" type="date" value={form.due_date} disabled={blocked} onChange={e => setForm({ ...form, due_date: e.target.value })} /></label><label>同级排序位置<input aria-label="目标排序位置" type="number" min="0" max="10000" step="1" value={form.position} disabled={blocked} required onChange={e => setForm({ ...form, position: e.target.value })} /><small>数字越小越靠前，同位置由服务端稳定排序。</small></label></div><div className="rw-actions"><button className="rw-primary" disabled={blocked || !form.title.trim()}>{form.id ? "保存目标" : "创建目标"}</button><button type="button" disabled={busy} onClick={() => setForm(null)}>取消编辑</button></div></form></> : reading ? <p role="status">正在读取目标详情…</p> : current ? <>
        {current.archived_at && <div className="rw-alert"><strong>目标已归档 · {dateText(current.archived_at)}</strong><p className="rw-prewrap">归档原因：{current.archived_reason || "未提供"}</p><p>归档是人工管理操作，不代表目标完成。</p></div>}<p className="rw-phase6-path">{goalPath(current)}</p><div className="rw-spread"><h3>{current.title}</h3><span className="rw-badge">{current.archived_at ? "已归档" : statuses[current.status]}</span></div><div className="rw-phase6-meta"><span>截止：{current.due_date?.slice(0, 10) || "未设置"}</span><span>状态由负责人手动确认</span></div><h4>目标描述</h4><p className="rw-prewrap rw-phase6-description">{current.description || "尚未填写目标描述。"}</p><div className="rw-phase6-acceptance"><h4>验收目标</h4><p className="rw-prewrap">{current.acceptance_criteria || "尚未填写验收标准，请由负责人明确。"}</p></div>
        <section className="rw-phase6-section rw-goal-requirements"><div><h4>交付物</h4><p className="rw-prewrap">{current.deliverables || "尚未填写交付物。"}</p></div><div><h4>时间要求（原文）</h4><p className="rw-prewrap">{current.time_requirement || "尚未填写时间要求原文。"}</p></div><div><h4>约束</h4><p className="rw-prewrap">{current.constraints || "尚未填写约束。"}</p></div></section>
        {!!current.generation_sources?.length && <details className="rw-phase6-section rw-detail-fold" aria-label="目标来源与确认详情"><summary>正式目标来源与确认详情</summary><p>以下为发布或采纳时的不可变快照，不代表之后人工编辑的内容仍与原文一致。</p>{current.generation_sources.map(record => <details key={record.id}><summary>{({ add: "新增", modify: "修改", move: "移动", retire: "退役归档" })[record.operation]} · {record.node.title ?? "来源详情已撤回"} · {record.source_details_redacted ? "来源不可展示" : record.node.origin === "explicit" ? "原文明确要求" : "建议（非原文要求）"}</summary><p>生成方式：{record.node.generation_method === "manual" ? "人工改动" : record.generation.method === "ai" ? "AI 生成建议（历史记录）" : "规则提取（本地原文辅助，需核对）"}</p><p>确认人：{record.confirmation.actor_id} · {dateText(record.confirmation.confirmed_at)} · 草稿 r{record.confirmation.draft_revision}{record.confirmation.goal_revision != null && ` · 目标 r${record.confirmation.goal_revision}`}</p><p>草稿 {record.draft_id} · 节点 {record.node_id}</p>{record.current_differs_from_confirmation && <p className="rw-warning">当前要求已不同于当次确认（当前目标 r{record.current_revision ?? current.revision}）。以下原文与确认仅对应当次快照，请重新核对当前要求。</p>}{record.source_details_redacted ? <p className="rw-warning">来源已撤回或权限变化，原文与生成节点不可展示；以下人工确认记录仍保留。</p> : ([ ["title", "研究目标"], ["description", "描述"], ["acceptance_criteria", "验收标准"], ["deliverables", "交付物"], ["time_requirement", "时间要求（原文）"], ["due_date", "明确截止日期"], ["constraints", "约束"] ] as const).map(([field, label]) => <div key={field}><h5>{label}</h5><p className="rw-prewrap">{record.node[field] || "未设置"}</p>{record.node.field_sources?.[field]?.length ? record.node.field_sources[field]!.map((source, index) => <blockquote className="rw-prewrap" key={index}>版本 {source.version_id} · 字符 [{source.start}, {source.end})（非页码）<br />{source.quote}</blockquote>) : <p className="rw-muted">未提供字段原文依据。</p>}</div>)}</details>)}</details>}
        {canManage && <div className="rw-actions"><button disabled={blocked} onClick={() => beginForm(current)}>编辑目标</button><button disabled={blocked} onClick={() => beginForm(undefined, current.id)}>添加子目标</button></div>}
        <section className="rw-phase6-section"><div className="rw-spread"><h4>关联知识</h4>{canContribute && <button disabled={blocked} onClick={() => void openPicker("knowledge")}>关联我的知识</button>}</div><p className="rw-phase6-note">关联不等于授予权限或完成目标，仅显示你当前有权读取的版本。</p>{current.knowledge.length ? <ul className="rw-phase6-records">{current.knowledge.map(link => <li key={link.id}><div className="rw-spread"><strong>{link.title} · v{link.number}</strong>{canContribute && (owner || link.linked_by === user.id) && <button disabled={blocked} onClick={() => unlink(link.id)}>取消关联</button>}</div><SourceLink source={link.source} /><small>关联于 {dateText(link.created_at)}</small></li>)}</ul> : <p className="rw-muted">暂无可见的关联知识。</p>}{picker === "knowledge" && versionPicker}</section>
        <details className="rw-phase6-section rw-detail-fold"><summary>自主贡献与进展</summary>{current.updates.length ? <ol className="rw-phase6-records">{current.updates.map(update => <li key={update.id}><p className="rw-prewrap">{update.content}</p>{update.citation && <p>参考版本：{update.citation.title} · v{update.citation.number} <SourceLink source={update.citation.source} /></p>}<small>{dateText(update.created_at)}</small></li>)}</ol> : <p className="rw-muted">暂无可见的进展记录。</p>}{canContribute ? <form className="rw-form" onSubmit={e => { e.preventDefault(); contribute("update"); }}><label>新增进展<textarea aria-label="新增目标进展" rows={4} required maxLength={20000} value={content} disabled={blocked} onChange={e => setContent(e.target.value)} placeholder="记录研究进展、阻碍与下一步，不要写入私有知识正文。" /></label><p className="rw-phase6-note">不附版本的进展正文对全体项目成员可读；附版本的记录仅对该版本当前授权者显示。请勿将私有内容写入公开目标描述或进展正文。</p>{picker === "update" ? versionPicker : <button type="button" disabled={blocked} onClick={() => void openPicker("update")}>选择参考知识版本（可选）</button>}<button className="rw-primary" disabled={blocked || !content.trim() || (picker === "update" && !chosenVersion)}>记录进展</button></form> : <p className="rw-phase6-note">{current.archived_at ? "已归档目标只读，历史记录保留。" : "已完成目标停止新增贡献；如需继续研究，请由负责人核对并调整状态。"}</p>}</details>
        {canManage && <section className="rw-phase6-section"><h4>人工确认阶段状态</h4><p className="rw-phase6-note">依据验收标准与证据确认，不以知识数量计算完成度；审核通过不会自动完成目标。父目标完成前，未归档子目标必须全部完成。</p><label>目标阶段状态<select aria-label="目标阶段状态" value={nextStatus} disabled={blocked} onChange={e => setNextStatus(e.target.value as GoalStatus)}>{Object.entries(statuses).map(([value, label]) => <option key={value} value={value} disabled={value !== current.status && !nextStatuses[current.status].includes(value as GoalStatus)}>{label}</option>)}</select></label><label>确认依据<textarea aria-label="状态确认依据" rows={3} value={reason} disabled={blocked} maxLength={10000} onChange={e => setReason(e.target.value)} /></label><button className="rw-primary" disabled={blocked || !reason.trim() || !nextStatuses[current.status].includes(nextStatus)} onClick={confirmStatus}>确认阶段状态</button></section>}
        <section className="rw-phase6-section"><h4>人工状态确认历史</h4>{current.status_history.length ? <ol className="rw-phase6-records">{current.status_history.map(entry => <li key={entry.id}><strong>{statuses[entry.from_status]} → {statuses[entry.to_status]}</strong><p className="rw-prewrap">{entry.reason}</p><small>{entry.reason.startsWith("legacy_import") ? "历史任务状态导入（非本次验收）" : "负责人手动确认"} · {dateText(entry.created_at)}</small></li>)}</ol> : <p className="rw-muted">尚无人工状态变更记录。</p>}</section>
        {canManage && <details className="rw-phase6-section"><summary>归档目标</summary><p className="rw-phase6-note">归档退出活跃目标树，保留历史，不表示完成；有未归档子目标时不可归档。</p><label>归档原因<textarea aria-label="归档原因" rows={2} maxLength={10000} value={archiveReason} disabled={blocked} onChange={e => setArchiveReason(e.target.value)} /></label><button className="rw-danger" disabled={blocked || !archiveReason.trim()} onClick={archive}>确认归档目标</button></details>}
      </> : <Empty><h3>让研究目标与证据相互连接</h3><p>从左侧选择目标，查看验收要求与阶段进展。成员自主贡献知识，完成情况由负责人确认。</p></Empty>}</article>
    </div>
  </section>;
}
