import { useEffect, useRef, useState, type ReactNode } from "react";
import MarkdownMessage from "../components/MarkdownMessage";
import { api, ApiError, errorText, idPath, importKnowledge, listOf } from "./api";
import type { Document, Goal, ImportReport, KnowledgeImport, Space, User, Version } from "./types";
import { canManageSpace, dateText, kinds, spaceName, writable } from "./types";
import { Empty, Feedback, PanelHeading, SourceLink } from "./ui";
import { SafetyLesson } from "./Guide";

type Props = { space: Space; spaces: Space[]; user: User; libraryNavigation?: ReactNode; refreshToken?: number; onSpaceDeleted?: () => Promise<void> | void; onAskVersion?: (versionId: string) => void };
type Dialog = "submit" | "version" | "document" | "space" | null;
const currentVersion = (doc: Document) => doc.active_version !== undefined ? doc.active_version : doc.latest_version;
const versionsOf = (doc: Document) => doc.versions?.length ? doc.versions : currentVersion(doc) ? [currentVersion(doc)!] : [];
const isActive = (version: Version) => !version.lifecycle_status || version.lifecycle_status === "active";
const versionName = (version?: Version | null) => `v${version?.number ?? "—"}`;
const lifecycleName = (version: Version) => version.lifecycle_status === "deleted" ? "已删除" : version.lifecycle_status === "withdrawn" ? "已撤回" : "有效";

export default function Documents({ space, spaces, user, libraryNavigation, refreshToken = 0, onSpaceDeleted, onAskVersion }: Props) {
  const lastRefresh = useRef(refreshToken);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [goals, setGoals] = useState<Goal[]>([]);
  const [selected, setSelected] = useState<Document | null>(null);
  const [version, setVersion] = useState<Version | null>(null);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [filename, setFilename] = useState("");
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const [goalId, setGoalId] = useState("");
  const [dialog, setDialog] = useState<Dialog>(null);
  const [reason, setReason] = useState("");
  const [replacement, setReplacement] = useState("");
  const [loading, setLoading] = useState(true);
  const [reading, setReading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showImport, setShowImport] = useState(false);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [entrySpaceId, setEntrySpaceId] = useState(space.id);
  const [saveReport, setSaveReport] = useState("");
  const [importReport, setImportReport] = useState<ImportReport | null>(null);
  const scope = `${user.id}:${space.id}:${space.role}:${space.epoch ?? 0}`;
  const [loadedScope, setLoadedScope] = useState(scope);
  const scopeRef = useRef(scope), generation = useRef(0), selectionSeq = useRef(0), listSeq = useRef(0);
  const lock = useRef<object | null>(null);
  if (scopeRef.current !== scope) { scopeRef.current = scope; generation.current++; selectionSeq.current++; listSeq.current++; }
  const ready = loadedScope === scope;
  const docs = ready ? documents : [];
  const doc = ready ? selected : null;
  const displayed = ready ? version : null;
  const canWrite = writable(space, user) && space.kind !== "results";
  const canManage = canWrite && !!doc && (doc.created_by === user.id || (space.owner_id === user.id && space.role === "owner"));
  const canWithdrawDocument = !!doc && (space.kind === "results" ? user.role === "owner" : canManage);
  const canDeleteSpace = space.kind === "collaboration" && canManageSpace(space, user);
  const canSubmit = canWrite && user.role !== "owner" && (space.kind === "private" || space.kind === "collaboration");
  const canShare = space.kind !== "private" || space.owner_id === user.id;
  const collaboration = spaces.filter(s => s.kind === "collaboration" && s.id !== space.id && writable(s, user));
  const active = !!displayed && isActive(displayed);
  const current = !!displayed && (displayed.is_current ?? displayed.id === (doc && currentVersion(doc)?.id));
  const replacements = doc ? versionsOf(doc).filter(v => isActive(v) && v.id !== displayed?.id) : [];
  const valid = (epoch: number) => scopeRef.current === scope && generation.current === epoch;
  const clearSelection = () => { selectionSeq.current++; setSelected(null); setVersion(null); setEditing(false); setContent(""); setTitle(""); setFilename(""); setSource(""); setReading(false); setDialog(null); setGoalId(""); setTarget(""); setReason(""); setReplacement(""); };
  const fetchDocuments = async (epoch: number) => {
    const request = ++listSeq.current;
    const result = listOf<Document>(await api(`/spaces/${idPath(space.id)}/documents`), "documents");
    if (!valid(epoch) || request !== listSeq.current) return null;
    setDocuments(result);
    setSelected(previous => previous ? result.find(item => item.id === previous.id) ?? null : null);
    setVersion(previous => {
      if (!previous) return null;
      const metadata = result.flatMap(versionsOf).find(item => item.id === previous.id);
      return metadata ? { ...previous, ...metadata, content: isActive(metadata) ? previous.content : undefined } : null;
    });
    return result;
  };
  useEffect(() => {
    const epoch = ++generation.current;
    lock.current = null; setLoadedScope(scope); clearSelection(); setDocuments([]); setGoals([]); setLoading(true); setBusy(false); setError(""); setNotice(""); setShowImport(false); setImportFile(null); setEntrySpaceId(space.id); setSaveReport(""); setImportReport(null);
    void fetchDocuments(epoch).catch(e => { if (valid(epoch)) setError(errorText(e)); }).finally(() => { if (valid(epoch)) setLoading(false); });
    if (canSubmit) void api("/goals").then(data => { if (valid(epoch)) setGoals(listOf<Goal>(data, "goals")); }).catch(() => { /* Optional goal association must not block knowledge access. */ });
    return () => { generation.current++; selectionSeq.current++; listSeq.current++; lock.current = null; };
  }, [scope]);
  useEffect(() => {
    if (refreshToken === lastRefresh.current) return;
    lastRefresh.current = refreshToken;
    const epoch = generation.current;
    void fetchDocuments(epoch).catch(e => { if (valid(epoch)) setError(errorText(e)); });
  }, [refreshToken]);
  const run = async (operation: (epoch: number) => Promise<void>, success = "") => {
    if (lock.current || !ready) return;
    const token = {}; lock.current = token; const epoch = generation.current;
    setBusy(true); setError(""); setNotice("");
    try { await operation(epoch); if (valid(epoch)) setNotice(success); }
    catch (e) { if (valid(epoch)) setError(errorText(e)); }
    finally { if (lock.current === token) { lock.current = null; if (valid(epoch)) setBusy(false); } }
  };
  const loadVersion = async (item: Document, metadata: Version, epoch = generation.current) => {
    const request = ++selectionSeq.current;
    setSelected(item); setVersion(metadata); setEditing(false); setContent(""); setFilename(""); setSource(""); setTitle(item.title); setDialog(null); setGoalId(""); setTarget(""); setError(""); setNotice("");
    if (!isActive(metadata)) { setReading(false); return; }
    setReading(true);
    try {
      const result = await api<Version>(`/versions/${idPath(metadata.id)}`);
      if (!valid(epoch) || request !== selectionSeq.current) return;
      if (result.id !== metadata.id || (result.document_id && result.document_id !== item.id) || (result.space_id && result.space_id !== space.id)) throw new Error("知识版本不一致，请刷新后重试。");
      setVersion({ ...metadata, ...result }); setContent(result.content ?? ""); setFilename(result.filename ?? item.filename ?? ""); setSource(result.source ?? item.source ?? "");
    } catch (e) { if (valid(epoch) && request === selectionSeq.current) { setVersion({ ...metadata, content: undefined }); setError(errorText(e)); } }
    finally { if (valid(epoch) && request === selectionSeq.current) setReading(false); }
  };
  const chooseDocument = (item: Document) => {
    const metadata = currentVersion(item) ?? versionsOf(item)[0];
    if (metadata) void loadVersion(item, metadata); else { clearSelection(); setSelected(item); setError("该知识尚无可读版本。"); }
  };
  const startNew = () => { clearSelection(); setEditing(true); setShowImport(false); setImportFile(null); setImportReport(null); setSaveReport(""); setEntrySpaceId(space.id); setError(""); setNotice(""); };
  const importAndSave = () => {
    if (!canWrite || !importFile || !spaces.some(s => s.id === entrySpaceId && writable(s, user) && s.kind !== "results")) return;
    const file = importFile;
    void run(async epoch => {
      if (!/\.(txt|md|docx)$/i.test(file.name)) throw new Error("仅支持 TXT / MD / DOCX；PDF 暂不可用，请先转换为 TXT 或 MD。");
      if (file.size > 8 * 1024 * 1024) throw new Error("导入文件请控制在 8 MB 内。");
      const result = await importKnowledge<KnowledgeImport>(entrySpaceId, file, { title, source });
      if (!valid(epoch)) return;
      const saved = "document" in result ? result.document : result;
      const report = "document" in result ? result.report : result.import_report;
      if (!saved?.id) throw new Error("导入结果缺少知识记录，请刷新核对，勿重复导入。");
      setImportReport(report ?? null); setSaveReport(`文件已保存至「${spaceName(spaces.find(s => s.id === entrySpaceId) ?? space)}」，未自动共享或送审。`); setImportFile(null); setShowImport(false); setEditing(false);
      if (entrySpaceId !== space.id) { clearSelection(); return; }
      const items = await fetchDocuments(epoch); if (!items || !valid(epoch)) return;
      const updated = items.find(item => item.id === saved.id);
      if (updated && currentVersion(updated)) await loadVersion(updated, currentVersion(updated)!, epoch);
    }, "文件已解析并保存为新知识；请核对导入报告与原文，不代表送审或共享。");
  };
  const save = () => {
    if (!canWrite || !content.trim() || !title.trim() || (!doc && !spaces.some(s => s.id === entrySpaceId && writable(s, user) && s.kind !== "results"))) return;
    void run(async epoch => {
      const result = doc
        ? await api<Version>(`/documents/${idPath(doc.id)}/versions`, "POST", { content, filename: filename.trim(), source: source.trim() })
        : await api<Document>(`/spaces/${idPath(entrySpaceId)}/documents`, "POST", { title: title.trim(), content, filename: filename.trim(), source: source.trim() });
      if (!valid(epoch)) return;
      setSaveReport(`已保存至「${spaceName(spaces.find(s => s.id === entrySpaceId) ?? space)}」${doc ? "，新增版本保留历史。" : "，新知识未自动共享或送审。"}`);
      if (!doc && entrySpaceId !== space.id) { clearSelection(); return; }
      const items = await fetchDocuments(epoch); if (!items || !valid(epoch)) return;
      const updated = items.find(item => item.id === (doc?.id ?? result.id));
      if (updated && currentVersion(updated)) await loadVersion(updated, currentVersion(updated)!, epoch);
      else { clearSelection(); setError("知识已保存，但未读到当前版本，请刷新核对。"); }
    }, "知识已保存，检索索引状态以服务端结果为准。");
  };
  const openDialog = (value: Dialog) => { setReason(""); setReplacement(""); setDialog(value); setError(""); setNotice(""); };
  const submitReview = () => {
    if (!canSubmit || !displayed || !active || reading) return;
    void run(async epoch => {
      await api("/reviews", "POST", { version_id: displayed.id, ...(goalId ? { goal_id: goalId } : {}) });
      if (!valid(epoch)) return;
      setDialog(null); await fetchDocuments(epoch);
    }, "所选版本已提交审核，可在我的提交查看状态。源知识库没有因此开放。");
  };
  const share = () => {
    if (!displayed || !active || !canShare || reading) return;
    const destination = collaboration.find(s => s.id === target);
    if (!destination || !window.confirm(`将「${doc?.title}」${versionName(displayed)}的独立副本分享到「${spaceName(destination)}」？\n目标库成员可读取副本，源知识库的其他内容不会开放。撤回源知识不影响独立副本。`)) return;
    void run(async epoch => { await api(`/versions/${idPath(displayed.id)}/share`, "POST", { target_space_id: target }); if (valid(epoch)) await fetchDocuments(epoch); }, "独立副本已分享到协作知识库。");
  };
  const removeVersion = () => {
    if (!canManage || !doc || !displayed || !active) return;
    const withdrawing = displayed.protected === true;
    if (withdrawing && (!reason.trim() || current)) return;
    if (!withdrawing && current && !replacements.some(v => v.id === replacement)) return;
    void run(async epoch => {
      try {
        if (withdrawing) await api(`/versions/${idPath(displayed.id)}/withdraw`, "POST", { reason: reason.trim() });
        else await api(`/versions/${idPath(displayed.id)}`, "DELETE", { ...(replacement ? { replacement_version_id: replacement } : {}), ...(reason.trim() ? { reason: reason.trim() } : {}) });
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) throw new Error(`${errorText(e)} 版本可能已被引用、送审或复制保护，请刷新版本状态后使用撤回；当前版本须先新增版本或撤回整份知识。`);
        throw e;
      }
      if (!valid(epoch)) return;
      clearSelection(); await fetchDocuments(epoch);
    }, withdrawing ? "版本已撤回，历史状态和已提交的审核内容保留。" : "版本已标记删除，不物理清除历史；独立副本不受影响。");
  };
  const withdrawDocument = () => {
    if (!doc || !canWithdrawDocument) return;
    void run(async epoch => { await api(`/documents/${idPath(doc.id)}`, "DELETE"); if (!valid(epoch)) return; clearSelection(); await fetchDocuments(epoch); }, "整份知识已撤回。已分享副本、送审版本与已发布成果仍独立保留。");
  };
  const deleteSpace = () => {
    if (!canDeleteSpace) return;
    void run(async epoch => {
      await api(`/spaces/${idPath(space.id)}`, "DELETE"); if (!valid(epoch)) return;
      clearSelection(); setDocuments([]); setDialog(null);
      await onSpaceDeleted?.();
    }, "协作知识库已删除，独立分享与成果副本不受影响。");
  };
  const pending = busy || loading || !ready;
  const readable = active && !reading && displayed?.content !== undefined;
  if (!ready) return <section className="rw-phase5"><p role="status">正在加载知识…</p></section>;
  return <section className="rw-phase5" aria-busy={busy}>
    <PanelHeading title="知识库" subtitle="版本可读、来源可查；送审只提交所选版本，不开放整个知识库。">
      {canWrite && <button className="rw-primary" disabled={pending || editing} onClick={startNew}>＋ 录入知识</button>}
    </PanelHeading>
    <Feedback error={ready ? error : ""} notice={ready && !saveReport ? notice : ""} />
    {importReport && <section className="rw-import-import-report" aria-label="导入报告"><h4>导入报告</h4><p>{importReport.origin_filename} · {importReport.format?.toUpperCase() ?? "文件"}{importReport.paragraph_count != null && ` · ${importReport.paragraph_count} 个文本段落`} · 不提供页码</p>{importReport.warnings.length ? <ul>{importReport.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul> : <p>服务端未报告额外警告，请仍核对原文。</p>}</section>}
    <div className="rw-phase5-layout">
      <aside className="rw-phase5-list" aria-label="当前知识库的知识">{libraryNavigation && <fieldset className="rw-library-controls" disabled={pending || editing}>{libraryNavigation}</fieldset>}{canDeleteSpace && <details className="rw-detail-fold"><summary>知识库管理</summary><button className="rw-danger" disabled={pending || editing} onClick={() => openDialog("space")}>删除知识库</button></details>}<h3>库内知识 <span>{docs.length}</span></h3>
        {loading || !ready ? <p role="status">正在加载知识…</p> : docs.length ? docs.map(item => <button key={item.id} className={`rw-doc-item ${doc?.id === item.id ? "selected" : ""}`} disabled={busy || editing} onClick={() => chooseDocument(item)}><span className="rw-file-icon">文</span><span><strong>{item.title}</strong><small>{versionName(currentVersion(item))} · 更新 {dateText(item.updated_at ?? currentVersion(item)?.created_at ?? item.created_at)}</small></span></button>) : <Empty>此知识库还没有知识。{canWrite ? "可手动填写或上传 TXT / MD / DOCX。" : "请联系有编辑权限的成员。"}</Empty>}
      </aside>
      <article className="rw-phase5-detail"><p className="rw-document-scope">{spaceName(space)} / {editing ? "知识录入" : "知识原文"}<small>{space.kind === "private" ? "仅本人及明确授权成员可见" : space.kind === "collaboration" ? "仅库内授权成员可见，项目负责人不自动加入" : "全体项目成员可读"}</small></p>{!editing && !doc ? <Empty><h3>每份知识，都有来处</h3><p>选择知识查看原文与版本，或录入新的研究知识。</p></Empty> : <>
        <div className="rw-spread"><div><p className="rw-eyebrow">{editing ? doc ? "新增版本" : "录入知识" : "知识原文"}</p><h3>{doc?.title || "新知识"}</h3></div>{!editing && <div className="rw-actions">{onAskVersion && <button disabled={busy || reading || !readable} onClick={() => { if (displayed && readable) onAskVersion(displayed.id); }}>以此版本提问</button>}{canWrite && <button disabled={busy || reading || !readable} onClick={() => setEditing(true)}>新增版本</button>}{canWithdrawDocument && <button className="rw-danger" disabled={busy || reading} onClick={() => openDialog("document")}>撤回整份知识</button>}</div>}</div>
        {editing ? <form className="rw-form" onSubmit={e => { e.preventDefault(); if (showImport && !doc) importAndSave(); else save(); }}>
          {!doc && <><label>保存到知识库<select aria-label="录入目标知识库" disabled={busy} value={entrySpaceId} onChange={e => setEntrySpaceId(e.target.value)}>{spaces.filter(s => writable(s, user) && s.kind !== "results").map(s => <option key={s.id} value={s.id}>{spaceName(s)} · {kinds[s.kind]}</option>)}</select></label><div className="rw-actions" role="group" aria-label="录入方式"><button type="button" aria-pressed={!showImport} disabled={busy} onClick={() => setShowImport(false)}>手动填写</button><button type="button" aria-pressed={showImport} disabled={busy} onClick={() => setShowImport(true)}>上传文件</button></div></>}
          <label htmlFor="knowledge-title">知识标题<input id="knowledge-title" value={title} required={!showImport} disabled={!!doc || busy} onChange={e => setTitle(e.target.value)} maxLength={200} /></label>
          <div className="rw-form-grid"><label htmlFor="knowledge-filename">文件名<input id="knowledge-filename" value={filename} onChange={e => setFilename(e.target.value)} disabled={busy} placeholder="研究笔记.md" /></label><label htmlFor="knowledge-source">来源<input id="knowledge-source" value={source} onChange={e => setSource(e.target.value)} disabled={busy} placeholder="原始出处或 https://…" /></label></div>
          {showImport && !doc && <section className="rw-import-import" aria-label="上传文件"><p>选择 TXT / MD / DOCX（最大 8 MB），选择不会保存。标题留空时使用文件名；DOCX 只提取段落和表格，不保留分页、图片或附件。PDF 暂不可用。</p><label>待导入文件<input aria-label="待导入文件" type="file" accept=".txt,.md,.docx,text/plain,text/markdown,application/vnd.openxmlformats-officedocument.wordprocessingml.document" disabled={busy} onChange={e => { setImportFile(e.target.files?.[0] ?? null); setImportReport(null); }} /></label><p>点击“导入并保存”才由服务端解析并写入所选库，不覆盖已有知识。</p></section>}
          <p className="rw-phase5-note">来源仅记录出处。当前版本接口不支持更新说明，暂不提供填写或保存说明。</p>
          {(!showImport || !!doc) && <label htmlFor="knowledge-content">正文<textarea id="knowledge-content" className="rw-text-editor" rows={16} value={content} onChange={e => setContent(e.target.value)} required disabled={busy} placeholder="输入 Markdown / 纯文本…" /></label>}
          <div className="rw-actions"><button className="rw-primary" disabled={busy || !canWrite || (showImport && !doc ? !importFile : !content.trim() || !title.trim())}>{busy ? "保存中…" : doc ? "保存为新版本" : showImport ? "导入并保存" : "保存知识"}</button><button type="button" disabled={busy} onClick={() => { setContent(displayed?.content ?? ""); setFilename(displayed?.filename ?? ""); setSource(displayed?.source ?? ""); setEditing(false); setShowImport(false); setImportFile(null); }}>取消编辑</button></div>
        </form> : <>
          {doc && <div className="rw-phase5-version-picker"><label className="rw-inline">知识版本<select aria-label="知识版本" disabled={busy} value={displayed?.id ?? ""} onChange={e => { const metadata = versionsOf(doc).find(v => v.id === e.target.value); if (metadata) void loadVersion(doc, metadata); }}>{versionsOf(doc).map(v => <option key={v.id} value={v.id}>{versionName(v)} · {v.is_current ?? v.id === currentVersion(doc)?.id ? "当前 · " : ""}{lifecycleName(v)} · {dateText(v.created_at)}</option>)}</select></label>{canManage && active && <button className="rw-danger" disabled={busy || reading} onClick={() => openDialog("version")}>{displayed?.protected ? "撤回版本" : "删除版本"}</button>}</div>}
          {displayed && <div className="rw-version-meta"><span className="rw-badge">{versionName(displayed)}{current ? " · 当前" : ""} · {lifecycleName(displayed)}</span><span>更新 {dateText(displayed.updated_at ?? displayed.created_at)}</span>{active && <span>{displayed.filename || "文本知识"}</span>}{displayed.protected && <span className="rw-badge rw-warning">受保护 · 已引用 / 送审 / 复制</span>}</div>}
          {reading ? <p role="status">正在读取版本正文…</p> : displayed && !active ? <div className="rw-alert"><strong>{lifecycleName(displayed)}</strong><p>此版本仅保留历史状态，不再读取正文，也不能分享或重新送审。已有送审版本与独立副本不受影响。</p></div> : readable ? <><div className="rw-phase5-source"><span>来源</span><SourceLink source={displayed?.source} />{!displayed?.source && <span className="rw-muted">未注明</span>}</div><div className="rw-document-content"><MarkdownMessage text={displayed?.content ?? ""} /></div></> : <p className="rw-muted">未读取到正文，请重新选择版本或刷新。</p>}
          {doc && <details className="rw-detail-fold"><summary>版本历史 · {versionsOf(doc).length} 个版本</summary><div className="rw-phase5-history"><div className="rw-table-wrap"><table><thead><tr><th>版本 / 更新日期</th><th>状态</th><th>使用情况</th></tr></thead><tbody>{versionsOf(doc).map(v => <tr key={v.id}><td><button className="rw-phase5-text-button" disabled={busy} onClick={() => void loadVersion(doc, v)}>{versionName(v)}</button><small>{dateText(v.created_at)}</small></td><td>{lifecycleName(v)}{(v.is_current ?? v.id === currentVersion(doc)?.id) && " · 当前"}</td><td>{v.protected ? "受保护，保留引用 / 审核 / 副本依据" : "普通版本"}</td></tr>)}</tbody></table></div><p className="rw-phase5-note">历史不会静默回退；删除只标记状态。当前接口尚不支持版本更新说明。</p></div></details>}
          {readable && <details className="rw-detail-fold"><summary>送审与分享</summary><div className="rw-phase5-version-actions">{user.role === "owner" ? <div><h4>提交所选版本</h4><p>负责人不提交送审：成员提交的版本由你在「审核管理」中统一审核，避免自己审核自己的成果。</p></div> : canSubmit && <div><h4>提交所选版本</h4><p>直接送审，无需先分享。负责人只看到本次提交的版本，审核通过后成果全组可见。</p><button className="rw-primary" disabled={busy} onClick={() => openDialog("submit")}>送审当前所选版本</button></div>}{canShare && collaboration.length > 0 && <div><h4>分享独立副本</h4><p>仅分享到你有写入权限的协作知识库，不改变源库权限。</p><div className="rw-actions"><select aria-label="分享目标协作知识库" value={target} disabled={busy} onChange={e => setTarget(e.target.value)}><option value="">选择目标协作知识库</option>{collaboration.map(s => <option key={s.id} value={s.id}>{spaceName(s)}</option>)}</select><button disabled={busy || !target} onClick={share}>分享副本</button></div></div>}</div></details>}
          {displayed && <details className="rw-details"><summary>技术详情</summary><dl><dt>知识 ID</dt><dd>{doc?.id}</dd><dt>版本 ID</dt><dd>{displayed.id}</dd><dt>索引状态</dt><dd>{displayed.index_status ?? "未知"}</dd></dl></details>}
        </>}
      </>}{saveReport && <div className="rw-document-status" role="status" aria-label="保存报告">{saveReport}</div>}</article>
    </div>
    {ready && dialog && <div className="rw-phase5-modal-backdrop"><section className="rw-phase5-modal" role="dialog" aria-modal="true" aria-labelledby="knowledge-dialog-title" onKeyDown={e => { if (e.key === "Escape" && !busy) setDialog(null); }}><SafetyLesson key={`${user.id}:${user.role}:${dialog === "submit" ? "review" : "deletion"}`} user={user} topic={dialog === "submit" ? "review" : "deletion"} popup={dialog === "submit"} /><h3 id="knowledge-dialog-title">{dialog === "submit" ? "提交指定版本审核" : dialog === "space" ? "删除协作知识库" : dialog === "document" ? "撤回整份知识" : displayed?.protected ? "撤回受保护版本" : "删除版本"}</h3>
      {dialog === "submit" ? <><p>将《{doc?.title}》{versionName(displayed)}提交审核，本次内容将作为审核依据保留。</p><div className="rw-alert"><strong>负责人可见：本次送审内容</strong><p>不会获得源知识库的进入、检索或其他知识的阅读权限。撤回源版本或源库后，此送审版本仍独立保留。</p></div><p>审核通过后，本次提交的版本将发布到成果知识库，<strong>全体项目成员可读</strong>，不发布源库其他知识。</p><label>关联目标（可选）<select aria-label="送审关联目标" value={goalId} disabled={busy} onChange={e => setGoalId(e.target.value)}><option value="">不关联目标</option>{goals.map(goal => <option key={goal.id} value={goal.id}>{goal.path || goal.title}</option>)}</select><small>关联不等于授予源知识权限，也不代表目标已完成。</small></label><div className="rw-actions"><button disabled={busy} onClick={() => setDialog(null)}>取消</button><button className="rw-primary" disabled={busy || !canSubmit || !readable} onClick={submitReview}>确认提交审核</button></div></> : dialog === "space" ? <><p>将删除「{spaceName(space)}」，影响当前库的 <strong>{docs.length} 份知识</strong>及全部已授权成员。成员将不能继续访问、检索或在本库对话，相关旧会话需要重新校验。</p><p>独立分享副本、已有送审版本和成果库副本不会级联删除。其他知识库与成员权限不受影响。</p><div className="rw-actions"><button disabled={busy} onClick={() => setDialog(null)}>保留知识库</button><button className="rw-danger" disabled={busy || !canDeleteSpace} onClick={deleteSpace}>确认删除知识库</button></div></> : dialog === "document" ? <><p>撤回《{doc?.title}》及其全部版本；此后原文不再可读或被检索。</p><p>已分享副本、送审版本与已发布成果独立保留，不能通过撤回源知识将它们收回。</p><div className="rw-actions"><button disabled={busy} onClick={() => setDialog(null)}>保留知识</button><button className="rw-danger" disabled={busy || !canWithdrawDocument} onClick={withdrawDocument}>确认撤回整份知识</button></div></> : <><p>《{doc?.title}》{versionName(displayed)} · {displayed?.protected ? "已被引用、送审或复制，仅可撤回，保留历史依据。" : "普通版本可标记删除，不物理清除。"}</p>{current && (displayed?.protected ? <div className="rw-alert rw-warning">这是当前版本，不能单独撤回。请先新增版本，或撤回整份知识。</div> : <label>明确指定保留的当前版本<select aria-label="替换当前版本" value={replacement} disabled={busy || !replacements.length} onChange={e => setReplacement(e.target.value)}><option value="">{replacements.length ? "请选择，不会自动回退" : "没有其他有效版本，请先新增或撤回整份知识"}</option>{replacements.map(v => <option key={v.id} value={v.id}>{versionName(v)} · {dateText(v.created_at)}</option>)}</select></label>)}<label>原因{displayed?.protected ? "（必填）" : "（可选）"}<textarea aria-label="版本处理原因" rows={3} value={reason} disabled={busy} onChange={e => setReason(e.target.value)} maxLength={2000} /></label><p className="rw-phase5-note">独立分享、送审版本及已发布副本不会级联删除或撤回。</p><div className="rw-actions"><button disabled={busy} onClick={() => setDialog(null)}>取消</button><button className="rw-danger" disabled={busy || !canManage || (displayed?.protected ? current || !reason.trim() : current && !replacement)} onClick={removeVersion}>{displayed?.protected ? "确认撤回版本" : "确认删除版本"}</button>{current && canWithdrawDocument && <button disabled={busy} onClick={() => openDialog("document")}>改为撤回整份知识</button>}</div></>}
      <Feedback error={error} />
    </section></div>}
  </section>;
}
