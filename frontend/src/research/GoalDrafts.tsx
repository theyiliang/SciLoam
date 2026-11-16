import { useEffect, useRef, useState } from "react";
import { api, ApiError, errorText, idPath, listOf } from "./api";
import type { Document, GoalDraft, GoalDraftField, GoalDraftNode, GoalDraftReceipt, GoalDraftSuggestion, Space, User, Version } from "./types";
import { dateText, spaceName } from "./types";
import { Feedback } from "./ui";
import { cancelGoalGeneration, generationFailure, generationLabels, getGoalGeneration, startGoalGeneration, watchGoalGeneration, type GoalGenerationEvent } from "./goalDraftRun";
import { StreamError } from "./stream";

const fields: { field: GoalDraftField; label: string }[] = [{ field: "title", label: "研究目标" }, { field: "description", label: "描述" }, { field: "acceptance_criteria", label: "验收标准" }, { field: "deliverables", label: "交付物" }, { field: "time_requirement", label: "时间要求（原文）" }, { field: "due_date", label: "明确截止日期" }, { field: "constraints", label: "约束" }];
const methods = { rule: "规则提取（本地原文辅助，需核对）", ai: "AI 生成建议（历史记录）", manual: "人工改动" };
const operations = { add: "新增", modify: "修改", move: "移动", retire: "退役（归档）" };
const versionsOf = (doc: Document): Version[] => {
  const current = doc.active_version !== undefined ? doc.active_version : doc.latest_version;
  return (doc.versions?.length ? doc.versions : current ? [current] : []).filter(v => (!v.lifecycle_status || v.lifecycle_status === "active") && (v.is_current ?? v.id === current?.id));
};
export function draftDescendants(nodes: GoalDraftNode[], id: string): Set<string> {
  const result = new Set([id]); let changed = true;
  while (changed) { changed = false; for (const node of nodes) if (node.parent_id && result.has(node.parent_id) && !result.has(node.id)) { result.add(node.id); changed = true; } }
  return result;
}
export default function GoalDrafts({ user, onClose, onPublished }: { user: User; onClose: () => void; onPublished: () => Promise<void> | void }) {
  const [drafts, setDrafts] = useState<GoalDraft[]>([]), [draft, setDraft] = useState<GoalDraft | null>(null);
  const [nodes, setNodes] = useState<GoalDraftNode[]>([]), [spaces, setSpaces] = useState<Space[]>([]), [documents, setDocuments] = useState<Document[]>([]);
  const [spaceId, setSpaceId] = useState(""), [selected, setSelected] = useState<string[]>([]), [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false), [loading, setLoading] = useState(true), [spacesLoading, setSpacesLoading] = useState(true), [pickerLoading, setPickerLoading] = useState(false);
  const [error, setError] = useState(""), [listError, setListError] = useState(""), [spacesError, setSpacesError] = useState("");
  const [stale, setStale] = useState(false), [receipt, setReceipt] = useState<GoalDraftReceipt | null>(null), [checked, setChecked] = useState<string[]>([]);
  const scope = `${user.id}:${user.role}`, scopeRef = useRef(scope), generation = useRef(0), pickerSeq = useRef(0), lock = useRef<object | null>(null);
  const [loadedScope, setLoadedScope] = useState(scope);
  const [progress, setProgress] = useState<GoalGenerationEvent[]>([]), [generationNotice, setGenerationNotice] = useState("");
  const [startedAt, setStartedAt] = useState<number | null>(null), [elapsed, setElapsed] = useState(0), [generating, setGenerating] = useState(false);
  const [expanded, setExpanded] = useState<string[]>([]);
  const activeRun = useRef<{ id?: string; abort: AbortController; epoch: number; stopped: boolean } | null>(null);
  const resultPanel = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!generating || startedAt === null) return;
    const timer = setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [generating, startedAt]);
  if (scopeRef.current !== scope) { scopeRef.current = scope; generation.current++; pickerSeq.current++; }
  const valid = (epoch: number) => scopeRef.current === scope && generation.current === epoch;
  useEffect(() => {
    const epoch = ++generation.current; lock.current = null; setProgress([]); setGenerationNotice(""); setGenerating(false); setElapsed(0); setStartedAt(null); setExpanded([]); setLoadedScope(scope); setDrafts([]); setDraft(null); setNodes([]); setSpaces([]); setDocuments([]); setSpaceId(""); setSelected([]); setDirty(false); setChecked([]); setError(""); setListError(""); setSpacesError(""); setStale(false); setReceipt(null); setBusy(false); setLoading(true); setSpacesLoading(true); setPickerLoading(false);
    if (user.role === "owner") {
      void api("/goal-drafts").then(data => { if (valid(epoch)) setDrafts(listOf<GoalDraft>(data, "drafts")); }).catch(e => { if (valid(epoch)) setListError(errorText(e)); }).finally(() => { if (valid(epoch)) setLoading(false); });
      void api("/spaces").then(data => { if (valid(epoch)) setSpaces(listOf<Space>(data, "spaces").filter(space => space.kind === "basis")); }).catch(e => { if (valid(epoch)) setSpacesError(errorText(e)); }).finally(() => { if (valid(epoch)) setSpacesLoading(false); });
    } else { setLoading(false); setSpacesLoading(false); }
    return () => { generation.current++; pickerSeq.current++; lock.current = null;
      const previous = activeRun.current; activeRun.current = null;
      if (previous) { previous.stopped = true; previous.abort.abort(); if (previous.id) void cancelGoalGeneration(previous.id).catch(() => {}); }
    };
  }, [scope]);
  const run = async (operation: (epoch: number) => Promise<void>) => {
    if (lock.current || user.role !== "owner" || loadedScope !== scope) return;
    const token = {}; lock.current = token; const epoch = generation.current; setBusy(true); setError("");
    try { await operation(epoch); }
    catch (e) { if (valid(epoch)) { if (e instanceof ApiError && e.status === 409) { setStale(true); setError(`${errorText(e)} 草稿版本、来源或目标树已变化。保留当前内容但停止保存、发布与采纳。请重新打开最新草稿或重新提取并人工核对；不会自动重试、重设基线或覆盖。`); } else setError(errorText(e)); } }
    finally { if (lock.current === token) { lock.current = null; if (valid(epoch)) setBusy(false); } }
  };
  const apply = (value: GoalDraft) => { setDraft(value); setNodes(value.nodes); setExpanded([]); setDirty(false); setChecked([]); setStale(false); setReceipt(null); setDrafts(previous => [value, ...previous.filter(item => item.id !== value.id)]); };
  const chooseSpace = async (id: string) => {
    const epoch = generation.current, request = ++pickerSeq.current; setSpaceId(id); setDocuments([]); setSelected([]); setPickerLoading(!!id); setError("");
    if (!id) return;
    try { const items = listOf<Document>(await api(`/spaces/${idPath(id)}/documents`), "documents"); if (valid(epoch) && request === pickerSeq.current) setDocuments(items); }
    catch (e) { if (valid(epoch) && request === pickerSeq.current) setError(errorText(e)); }
    finally { if (valid(epoch) && request === pickerSeq.current) setPickerLoading(false); }
  };
  const extract = (regenerate = false) => {
    if (!selected.length || selected.length > 5 || (regenerate && !draft)) { setError("请先选择1至5个项目知识版本，再开始提取。"); return; }
    if (dirty && !window.confirm("重新提取将离开当前未保存修改，确认继续？")) return;
    void run(async epoch => {
      const operation = { abort: new AbortController(), epoch, stopped: false } as { id?: string; abort: AbortController; epoch: number; stopped: boolean };
      activeRun.current = operation; setProgress([]); setElapsed(0); setStartedAt(Date.now()); setGenerating(true); setGenerationNotice("已提交提取请求，正在建立运行；请勿重复点击。");
      const versionIds = [...selected], requestId = crypto.randomUUID();
      try {
        const started = await startGoalGeneration({ version_ids: versionIds, request_id: requestId, ...(regenerate ? { regenerates_id: draft!.id, expected_revision: draft!.revision } : {}) });
        operation.id = started.run_id;
        if (operation.stopped || !valid(epoch) || activeRun.current !== operation) {
          const stopped = await cancelGoalGeneration(started.run_id).catch(() => null);
          if (valid(epoch) && activeRun.current === operation) setGenerationNotice(stopped?.status === "completed" ? "停止前草稿已保存，请从已有草稿中核对；正式目标树未改变。" : "已停止本次提取，正式目标树未改变。");
          return;
        }
        if (started.request_id !== requestId || JSON.stringify(started.version_ids) !== JSON.stringify(versionIds)) throw new StreamError("目标提取运行来源不一致。", 403);
        setGenerationNotice("正在执行目标提取；下方仅显示服务端真实阶段。");
        const finish = async (draftId?: string) => {
          const value = draftId ? await api<GoalDraft>(`/goal-drafts/${idPath(draftId)}`) : (await getGoalGeneration(started.run_id)).result?.draft;
          if (!value) throw new Error("运行已结束但未读取到草稿，请刷新已有草稿检查，不要直接重复生成。");
          if (operation.stopped || !valid(epoch) || activeRun.current !== operation) return;
          if (!Array.isArray(value.nodes) || value.sources.some(source => !versionIds.includes(source.version_id))) throw new StreamError("返回草稿来源不一致。", 403);
          apply(value); setGenerationNotice(`已生成${value.nodes.length}个目标候选，请展开逐项核对；尚未发布。`);
          resultPanel.current?.scrollTo?.({ top: 0 });
        };
        try {
          const final = await watchGoalGeneration(started, operation.abort.signal, event => {
            if (!operation.stopped && valid(epoch) && activeRun.current === operation) { setProgress(previous => [...previous, event].slice(-50)); setGenerationNotice(generationLabels[event.type]); }
          }, attempt => { if (valid(epoch) && !operation.stopped) setGenerationNotice(`连接暂时中断，正在恢复同一次提取（${attempt}/2），不会重复创建草稿。`); });
          if (operation.stopped || !valid(epoch)) return;
          if (final.type === "goal.completed") await finish(typeof final.data.draft_id === "string" ? final.data.draft_id : undefined);
          else { const message = generationFailure(final.data.reason); setGenerationNotice(message); if (final.type === "goal.failed") setError(message); }
        } catch (error) {
          if (operation.stopped || operation.abort.signal.aborted || !valid(epoch)) return;
          if (error instanceof StreamError && [404, 409, 410].includes(error.status)) {
            const recovered = await getGoalGeneration(started.run_id);
            if (recovered.status === "completed") await finish();
            else throw error;
          } else throw error;
        }
      } catch (error) {
        if (!operation.stopped && valid(epoch)) {
          if ((error instanceof StreamError || error instanceof ApiError) && [401, 403].includes(error.status)) { setDraft(null); setNodes([]); setDrafts([]); setProgress([]); }
          setGenerationNotice("本次提取未完成。请检查提示或已有草稿，不会自动重复生成。"); throw error;
        }
      } finally {
        if (activeRun.current === operation) { activeRun.current = null; if (valid(epoch)) { setGenerating(false); setStartedAt(null); } }
      }
    });
  };
  const stopGeneration = () => {
    const operation = activeRun.current;
    if (!operation || operation.stopped) return;
    operation.stopped = true; operation.abort.abort(); setGenerationNotice("正在停止本次提取，并核对是否已保存草稿…");
    if (operation.id) void cancelGoalGeneration(operation.id).then(value => {
      if (!valid(operation.epoch)) return;
      setGenerationNotice(value.status === "completed" ? "停止前草稿已保存，请从已有草稿中核对；正式目标树未改变。" : "已停止本次提取，正式目标树未改变。");
      void api("/goal-drafts").then(data => { if (valid(operation.epoch)) setDrafts(listOf<GoalDraft>(data, "drafts")); }).catch(() => {});
    }).catch(() => { if (valid(operation.epoch)) setGenerationNotice("已停止读取。服务端将按连接租约结束，请先检查已有草稿。"); });
  };
  const open = (id: string) => { if (dirty && !window.confirm("打开其他草稿将放弃未保存修改，确认继续？")) return; void run(async epoch => { const value = await api<GoalDraft>(`/goal-drafts/${idPath(id)}`); if (valid(epoch)) apply(value); }); };
  const edit = (id: string, patch: Partial<GoalDraftNode>) => { setNodes(previous => previous.map(node => node.id === id ? { ...node, ...patch, origin: "suggested", generation_method: "manual" } : node)); setChecked(previous => previous.filter(value => value !== id)); setDirty(true); };
  const save = () => { if (!draft || stale || !dirty || !nodes.length || nodes.some(n => !n.title.trim() || !Number.isSafeInteger(n.position) || n.position < 0 || n.position > 10000)) return; void run(async epoch => { const value = await api<GoalDraft>(`/goal-drafts/${idPath(draft.id)}`, "PATCH", { expected_revision: draft.revision, nodes }); if (valid(epoch)) apply(value); }); };
  const publish = () => {
    if (!draft || stale || dirty || draft.status !== "draft" || !nodes.length || checked.length !== nodes.length || !draft.publication_readiness?.publishable || draft.suggestions?.length) return;
    if (!window.confirm("确认人工核对并发布此目标草稿？\n只追加新目标，不覆盖已有目标。目标内容对所有项目成员可读。每项原文辅助提取、历史AI建议（如有）、人工改动及验收标准已核对。来源或目标树变化将拒绝发布。")) return;
    void run(async epoch => { const result = await api<GoalDraftReceipt>(`/goal-drafts/${idPath(draft.id)}/publish`, "POST", { expected_revision: draft.revision, confirmed: true }); if (!valid(epoch)) return; setReceipt(result); setDraft(previous => previous ? { ...previous, status: "published" } : null); setDrafts(previous => previous.map(item => item.id === draft.id ? { ...item, status: "published" } : item)); await onPublished(); });
  };
  const abandon = () => {
    if (!draft || draft.status !== "draft" || busy) return;
    if (!window.confirm("确认废弃此未发布草稿？草稿、来源与生成快照保留为只读历史，不物理删除，也不改变正式目标树。未保存修改不会保留。")) return;
    void run(async epoch => { const value = await api<GoalDraft>(`/goal-drafts/${idPath(draft.id)}/abandon`, "POST", { expected_revision: draft.revision, confirmed: true }); if (valid(epoch)) apply(value); });
  };
  const reject = (suggestion: GoalDraftSuggestion) => {
    if (!draft || draft.status !== "draft" || busy || stale || dirty || suggestion.status !== "pending") return;
    if (!window.confirm(`确认拒绝此项${operations[suggestion.operation]}建议「${suggestion.node.title}」？不会修改正式目标树；拒绝决定将保留。`)) return;
    void run(async epoch => { const value = await api<GoalDraft>(`/goal-drafts/${idPath(draft.id)}/reject`, "POST", { suggestion_id: suggestion.id, expected_revision: draft.revision, confirmed: true }); if (valid(epoch)) apply(value); });
  };
  const adopt = (suggestion: GoalDraftSuggestion) => {
    if (!draft || draft.status !== "draft" || busy || stale || dirty || suggestion.status !== "pending" || !checked.includes(`suggestion:${suggestion.id}`)) return;
    if (!window.confirm(`确认逐项采纳“${operations[suggestion.operation]}”建议「${suggestion.node.title}」？\n${suggestion.operation === "retire" ? "只归档目标，不删除历史。" : "此操作会改变正式目标树，目标内容对全体项目成员可读。"}目标基线 r${suggestion.expected_goal_revision ?? "新建"}、草稿 r${draft.revision}；冲突时停止，不自动覆盖。`)) return;
    void run(async epoch => { const value = await api<GoalDraft>(`/goal-drafts/${idPath(draft.id)}/adopt`, "POST", { suggestion_id: suggestion.id, expected_revision: draft.revision, ...(suggestion.expected_goal_revision != null ? { expected_goal_revision: suggestion.expected_goal_revision } : {}), confirmed: true }); if (!valid(epoch)) return; apply(value); await onPublished(); });
  };
  if (user.role !== "owner" || loadedScope !== scope) return null;
  const readonly = busy || stale || (!!draft && draft.status !== "draft") || !!draft?.regenerates_id || !!draft?.suggestions?.length;
  const toggleChecked = (id: string, value: boolean) => setChecked(previous => value ? [...new Set([...previous, id])] : previous.filter(item => item !== id));
  const evidence = (node: GoalDraftNode) => <details className="rw-import-node-source"><summary>查看字段原文依据与字符位置</summary>{fields.map(({ field, label }) => <div key={field}><strong>{label}</strong>{node.field_sources?.[field]?.length ? node.field_sources[field]!.map((source, index) => <blockquote key={`${field}:${index}`} className="rw-prewrap">版本 {source.version_id} · 字符 [{source.start}, {source.end})（非页码）<br />{source.quote}</blockquote>) : <p>未提供此字段原文依据，不能当作原文要求。</p>}</div>)}{node.source && <details><summary>节点整体原文引用</summary><p>字符 [{node.source.start}, {node.source.end})（非页码）</p><blockquote className="rw-prewrap">{node.source.quote}</blockquote></details>}</details>;
  return <section className="rw-import-drafts" aria-labelledby="goal-drafts-title" aria-busy={busy}>
    <div className="rw-spread"><div><h3 id="goal-drafts-title">项目目标草稿</h3><p>AI 目标生成与重新生成已暂停。本地原文辅助提取仅按规则摘取标题、编号与明确标签，不理解研究语义，可能漏提或误提；草稿不是正式目标，需负责人逐项核对。历史 AI 记录保留原标识。</p></div><button disabled={busy} onClick={() => { if (!dirty || window.confirm("关闭将放弃未保存修改，确认关闭？")) onClose(); }}>关闭草稿面板</button></div><Feedback error={error} />
    <ol className="rw-goal-generation-steps" aria-label="目标提取步骤"><li>1 选择依据</li><li aria-current={generating ? "step" : undefined}>2 开始提取</li><li aria-current={draft && !generating ? "step" : undefined}>3 核对草稿</li><li>4 确认发布</li></ol>
    {(generationNotice || generating) && <section className="rw-goal-generation-progress" aria-label="目标提取进度"><div className="rw-spread"><strong role="status">{generationNotice}</strong>{generating && <button type="button" onClick={stopGeneration}>停止提取</button>}</div><p>{generating ? `已等待 ${elapsed} 秒；正在等待的阶段不会被模拟为完成。` : "提取只创建草稿，正式目标不会自动改变。"}</p>{progress.length > 0 && <ol>{progress.map(event => <li key={`${event.run_id}:${event.seq}`}><time dateTime={event.timestamp}>{dateText(event.timestamp)}</time><span>{generationLabels[event.type]}{event.type === "generation.started" ? event.data.method === "ai" ? " · 历史 AI 阶段（当前产品已暂停）" : " · 本地规则提取，需人工核对" : ""}</span></li>)}</ol>}</section>}
    <div className="rw-import-draft-layout"><aside><h4>选择项目知识依据</h4><Feedback error={spacesError} /><p>仅列出可读项目知识当前版本，每次最多 5 个。</p><label>项目知识库<select aria-label="草稿项目知识库" value={spaceId} disabled={busy || spacesLoading} onChange={e => void chooseSpace(e.target.value)}><option value="">选择项目知识库</option>{spaces.map(space => <option key={space.id} value={space.id}>{spaceName(space)}</option>)}</select></label>{spacesLoading && <p role="status">正在加载项目知识库…</p>}{pickerLoading && <p role="status">正在读取知识版本…</p>}<div className="rw-import-version-choices">{documents.flatMap(doc => versionsOf(doc).map(version => <label key={version.id} className="rw-inline"><input type="checkbox" aria-label={`${doc.title} v${version.number ?? "—"}`} checked={selected.includes(version.id)} disabled={busy || (!selected.includes(version.id) && selected.length >= 5)} onChange={e => setSelected(previous => e.target.checked ? [...previous, version.id] : previous.filter(id => id !== version.id))} /><span>{doc.title} · v{version.number ?? "—"}</span></label>))}</div><button className="rw-primary" disabled={busy || spacesLoading || pickerLoading || !selected.length} onClick={() => extract()}>本地原文辅助提取（需核对）</button>{draft && <button disabled={draft.status === "abandoned" || busy || pickerLoading || !selected.length} onClick={() => extract(true)}>本地规则重新提取差异草案</button>}<div className="rw-draft-pick-note"><small>已选 {selected.length}/5。{!selected.length && "请先选择项目知识库，再勾选依据版本后开始提取。"}</small><small>本地规则仅创建新草稿及差异建议，需逐项人工确认，不直接覆盖正式树；不调用 AI。</small></div>{spaceId && !pickerLoading && !documents.length && <p>此项目知识库没有可选知识。请先在“知识库”录入立项依据，再回到这里提取。</p>}<h4>已有草稿</h4><Feedback error={listError} />{loading ? <p role="status">正在加载草稿…</p> : drafts.length ? <ul className="rw-import-draft-list">{drafts.map(item => <li key={item.id}><button disabled={busy} aria-current={draft?.id === item.id ? "true" : undefined} onClick={() => open(item.id)}>{item.status === "published" ? "已发布" : item.status === "abandoned" ? "已废弃" : "待核对"} · {dateText(item.created_at)} · r{item.revision}</button>{item.publication_readiness && !item.publication_readiness.publishable && <p className="rw-warning">此稿暂不可发布：{item.publication_readiness.reasons.join("；")}</p>}</li>)}</ul> : !listError && <p>暂无目标草稿。</p>}</aside>
      <article ref={resultPanel} aria-label="目标草稿结果">{draft ? <><div className="rw-spread"><h4>{draft.status === "published" ? "已发布草稿" : draft.status === "abandoned" ? "已废弃草稿（只读历史）" : "逐项核对草稿"}</h4><span className="rw-badge">{draft.generation?.method === "ai" ? methods.ai : methods.rule} · r{draft.revision}</span></div>{draft.limitations?.length > 0 && <details><summary>提取局限（请核对）</summary><ul>{draft.limitations.map((line, index) => <li key={index}>{line}</li>)}</ul></details>}<p>“原文明确要求 / 建议”表示证据性质；“规则提取 / AI 生成 / 人工改动”表示生成方式。人工编辑保留原始引用，但引用不证明修改后的内容。</p>{draft.publication_readiness && !draft.publication_readiness.publishable && <div className="rw-alert rw-warning">此稿暂不可发布：{draft.publication_readiness.reasons.join("；")}。仍可查看、核对其他草稿。</div>}{stale && <div className="rw-alert rw-warning">此草稿已停止保存、发布与采纳。重新打开最新草稿或重新提取并核对，不自动 rebase。</div>}
        <nav className="rw-goal-draft-summary" aria-label="生成目标摘要"><h4>{nodes.length}个目标候选 · 展开后核对</h4>{nodes.map((node, index) => <button type="button" key={node.id} aria-expanded={expanded.includes(node.id)} onClick={() => setExpanded(previous => previous.includes(node.id) ? previous.filter(id => id !== node.id) : [...previous, node.id])}>{index + 1}. {node.title || "未命名目标"} · {checked.includes(node.id) ? "已核对" : "待核对"}</button>)}</nav>
        {nodes.map((node, index) => { const excluded = draftDescendants(nodes, node.id); return <details key={node.id} className="rw-goal-draft-editor" open={expanded.includes(node.id)} onToggle={event => { const opened = event.currentTarget.open; setExpanded(previous => opened ? [...new Set([...previous, node.id])] : previous.filter(id => id !== node.id)); }}><summary>目标 {index + 1}：{node.title || "未命名目标"} · {node.origin === "explicit" ? "原文明确要求" : "建议（非原文要求）"}</summary><fieldset disabled={readonly} className="rw-import-draft-node"><legend>{methods[node.generation_method] ?? "生成方式未记录"}</legend>{fields.map(({ field, label }) => <label key={field}>{label}{field === "title" ? <input aria-label={`草稿目标${index + 1}标题`} value={node.title} maxLength={500} onChange={e => edit(node.id, { title: e.target.value })} /> : field === "due_date" ? <input aria-label={`草稿目标${index + 1}日期`} type="date" value={node.due_date?.slice(0, 10) ?? ""} onChange={e => edit(node.id, { due_date: e.target.value || null })} /> : <textarea aria-label={`草稿目标${index + 1}${label}`} rows={3} maxLength={20000} value={node[field] ?? ""} onChange={e => edit(node.id, { [field]: e.target.value })} />}</label>)}<label>上级草稿目标<select aria-label={`草稿目标${index + 1}上级`} value={node.parent_id ?? ""} onChange={e => edit(node.id, { parent_id: e.target.value || null })}><option value="">顶层目标</option>{nodes.filter(parent => !excluded.has(parent.id)).map(parent => <option key={parent.id} value={parent.id}>{parent.title}</option>)}</select></label><label>排序位置<input aria-label={`草稿目标${index + 1}位置`} type="number" min={0} max={10000} step={1} value={node.position} onChange={e => edit(node.id, { position: Number(e.target.value) })} /></label>{evidence(node)}<label className="rw-inline"><input type="checkbox" aria-label={`已核对草稿目标${index + 1}`} checked={checked.includes(node.id)} disabled={readonly || dirty} onChange={e => toggleChecked(node.id, e.target.checked)} />已核对该项原文要求、建议、人工改动及验收标准</label><button type="button" disabled={readonly || nodes.some(child => child.parent_id === node.id)} onClick={() => { setNodes(previous => previous.filter(item => item.id !== node.id)); setChecked([]); setDirty(true); }}>删除此草稿节点</button></fieldset></details>; })}
        {!!draft.suggestions?.length && <section aria-label="重新生成差异建议"><h4>差异建议 · 不直接覆盖正式树</h4><p>逐项核对并采纳，每次采纳都会校验草稿与正式目标 revision。以下旧值为生成时快照，不会自动刷新基线。新增父目标需先采纳，再采纳依赖子目标；退役只归档，不删除历史。</p>{draft.suggestions.map(suggestion => <fieldset key={suggestion.id} disabled={draft.status !== "draft" || busy || stale || dirty || suggestion.status !== "pending"}><legend>{operations[suggestion.operation]} · {suggestion.node.title} · {suggestion.status === "adopted" ? "已采纳" : suggestion.status === "rejected" ? "已拒绝" : "待核对"}</legend><p>目标：{suggestion.target_goal_id ?? "新建目标"} · 目标 revision：{suggestion.expected_goal_revision ?? "新建"}</p>{suggestion.target_goal_id && <details><summary>对比生成时正式目标与建议值</summary>{suggestion.before ? [suggestion.before].map(goal => <div key={goal.id}>{goal.revision !== suggestion.expected_goal_revision && <p className="rw-warning">生成时快照 r{goal.revision}；此前逐项采纳已将目标基线推进至 r{suggestion.expected_goal_revision}，请再次核对。</p>}{fields.map(({ field, label }) => <div key={field}><strong>{label}</strong><p className="rw-prewrap">当前：{goal[field] || "未设置"}</p><p className="rw-prewrap">建议：{suggestion.node[field] || "未设置"}</p></div>)}<p>当前上级：{goal.parent_id ?? "顶层"} · 位置：{goal.position}</p><p>建议上级节点：{suggestion.node.parent_id ?? "顶层"} · 位置：{suggestion.node.position}（草稿节点会由服务端映射到正式目标）</p></div>) : <p>未提供生成时正式目标快照，请先核对正式目标再操作。</p>}</details>}{fields.map(({ field, label }) => <p key={field} className="rw-prewrap"><strong>{label}：</strong>{suggestion.node[field] || "未设置"}</p>)}<p>上级：{suggestion.node.parent_id ?? "顶层"} · 位置：{suggestion.node.position}</p>{evidence(suggestion.node)}<label className="rw-inline"><input type="checkbox" aria-label={`已核对差异${suggestion.id}`} checked={checked.includes(`suggestion:${suggestion.id}`)} onChange={e => toggleChecked(`suggestion:${suggestion.id}`, e.target.checked)} />已核对此项差异及目标基线</label><button disabled={draft.status !== "draft" || busy || stale || dirty || suggestion.status !== "pending" || !checked.includes(`suggestion:${suggestion.id}`)} onClick={() => adopt(suggestion)}>确认采纳此项{operations[suggestion.operation]}</button><button disabled={draft.status !== "draft" || busy || stale || dirty || suggestion.status !== "pending"} onClick={() => reject(suggestion)}>拒绝此项{operations[suggestion.operation]}</button></fieldset>)}</section>}
        <div className="rw-actions">{draft.status === "draft" && <button className="rw-danger" disabled={busy} onClick={abandon}>废弃此草稿</button>}<button disabled={readonly} onClick={() => { setNodes(previous => [...previous, { id: crypto.randomUUID(), parent_id: null, title: "", description: "", acceptance_criteria: "", deliverables: "", constraints: "", time_requirement: "", due_date: null, position: previous.length, origin: "suggested", generation_method: "manual", field_sources: {}, source: null }]); setChecked([]); setDirty(true); }}>添加人工建议节点</button><button disabled={readonly || !dirty || !nodes.length || nodes.some(node => !node.title.trim())} onClick={save}>保存草稿修改</button><button className="rw-primary" disabled={readonly || dirty || !nodes.length || checked.filter(id => nodes.some(node => node.id === id)).length !== nodes.length || !draft.publication_readiness?.publishable || !!draft.suggestions?.length} onClick={publish}>人工确认并追加发布</button></div><p>发布只追加、不覆盖已有目标；所有项目成员可读。先保存修改，再逐项核对与最终确认。无依据的手工节点可保存，但不能直接发布。</p>{receipt && <div className="rw-alert rw-success" role="status"><strong>已追加发布 {receipt.goal_ids.length} 个目标</strong><details><summary>查看发布回执 goal_ids</summary><ul>{receipt.goal_ids.map(id => <li key={id}>{id}</li>)}</ul></details></div>}<details><summary>草稿来源版本与校验信息</summary><ul>{draft.sources.map(source => <li key={source.version_id}>v{source.number} · 版本 {source.version_id}</li>)}</ul><p>来源校验：{draft.sources_hash}</p><p>目标树基线：{draft.base_tree_hash}</p></details></> : <p>选择依据版本提取新草稿，或打开已有草稿。保存草稿不会改变目标树。</p>}</article>
    </div>
  </section>;
}
