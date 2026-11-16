import { useEffect, useRef, useState } from "react";
import MarkdownMessage from "../components/MarkdownMessage";
import { api, errorText, idPath, listOf } from "./api";
import type { Review, ReviewSnapshot, User } from "./types";
import { dateText, kinds } from "./types";
import { Empty, Feedback, PanelHeading, SourceLink } from "./ui";

const stateLabel: Record<string, string> = { pending: "待审核", approved: "已通过", rejected: "已退回" };
const reviewTitle = (review: Review) => review.title ?? review.document_title ?? "送审知识";
const reviewNumber = (review: Review) => `v${review.number ?? "—"}`;
const submitterOf = (review: Review) => review.submitted_by ?? review.submitter_id ?? review.user_id ?? review.created_by;
export default function Reviews({ user }: { user: User }) {
  const [reviews, setReviews] = useState<Review[]>([]);
  const [selected, setSelected] = useState<Review | null>(null);
  const [snapshot, setSnapshot] = useState<ReviewSnapshot | null>(null);
  const [comment, setComment] = useState("");
  const [loading, setLoading] = useState(true);
  const [reading, setReading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const scope = `${user.id}:${user.role}`;
  const [loadedScope, setLoadedScope] = useState(scope);
  const currentScope = useRef(scope), generation = useRef(0), selectionSeq = useRef(0), listSeq = useRef(0);
  const lock = useRef<object | null>(null);
  if (currentScope.current !== scope) { currentScope.current = scope; generation.current++; selectionSeq.current++; listSeq.current++; }
  const ready = loadedScope === scope;
  const items = ready ? reviews : [];
  const receipt = ready ? selected : null;
  const body = ready ? snapshot : null;
  const submitter = receipt && submitterOf(receipt);
  const self = submitter === user.id;
  const pending = receipt && (receipt.status ?? receipt.decision ?? "pending") === "pending";
  // Backend capability includes frozen lineage, not just the visible submitter.
  const permission = receipt?.can_review ?? receipt?.can_decide;
  const bodyPermission = body?.can_review ?? body?.can_decide;
  const canDecide = user.role === "owner" && !!submitter && !self && !!pending && permission !== false && bodyPermission !== false && (permission === true || bodyPermission === true) && body?.content !== undefined && !reading;
  const valid = (epoch: number) => currentScope.current === scope && generation.current === epoch;
  const fetchReviews = async (epoch: number) => {
    const sequence = ++listSeq.current;
    const result = listOf<Review>(await api("/reviews"), "reviews");
    if (!valid(epoch) || sequence !== listSeq.current) return null;
    setReviews(result); return result;
  };
  useEffect(() => {
    const epoch = ++generation.current;
    lock.current = null; setLoadedScope(scope); setReviews([]); setSelected(null); setSnapshot(null); setComment(""); setError(""); setNotice(""); setBusy(false); setReading(false); setLoading(true);
    void fetchReviews(epoch).catch(e => { if (valid(epoch)) setError(errorText(e)); }).finally(() => { if (valid(epoch)) setLoading(false); });
    return () => { generation.current++; selectionSeq.current++; listSeq.current++; lock.current = null; };
  }, [scope]);
  const select = async (review: Review, epoch = generation.current) => {
    const sequence = ++selectionSeq.current;
    setSelected(review); setSnapshot(null); setComment(""); setReading(true); setError(""); setNotice("");
    try {
      const result = await api<ReviewSnapshot>(`/reviews/${idPath(review.id)}/snapshot`);
      if (!valid(epoch) || sequence !== selectionSeq.current) return;
      if (result.id !== review.id || result.version_id !== review.version_id) throw new Error("审核内容与当前提交不一致，请刷新后重试。");
      setSnapshot(result); setSelected({ ...review, ...result });
    } catch (e) { if (valid(epoch) && sequence === selectionSeq.current) setError(errorText(e)); }
    finally { if (valid(epoch) && sequence === selectionSeq.current) setReading(false); }
  };
  const run = async (operation: (epoch: number) => Promise<void>, success = "") => {
    if (lock.current || !ready) return;
    const token = {}; lock.current = token; const epoch = generation.current;
    setBusy(true); setError(""); setNotice("");
    try { await operation(epoch); if (valid(epoch)) setNotice(success); }
    catch (e) { if (valid(epoch)) setError(errorText(e)); }
    finally { if (lock.current === token) { lock.current = null; if (valid(epoch)) setBusy(false); } }
  };
  const refresh = () => void run(async epoch => { selectionSeq.current++; setSelected(null); setSnapshot(null); setComment(""); setReading(false); await fetchReviews(epoch); });
  const decision = (value: "approved" | "rejected") => {
    if (!receipt || !canDecide) return;
    if (value === "rejected" && !comment.trim()) { setError("退回前请填写具体补充意见。"); return; }
    if (!window.confirm(value === "approved" ? `确认通过《${reviewTitle(receipt)}》${reviewNumber(receipt)}并发布？\n仅将本次送审版本发布至成果知识库，全体项目成员可读，不开放源库其他知识，也不自动完成任务。` : `确认退回《${reviewTitle(receipt)}》${reviewNumber(receipt)}？\n补充意见：${comment.trim()}`)) return;
    void run(async epoch => {
      await api(`/reviews/${idPath(receipt.id)}/decision`, "POST", { decision: value, comment: comment.trim() });
      if (!valid(epoch)) return;
      const result = await fetchReviews(epoch); if (!result || !valid(epoch)) return;
      const updated = result.find(r => r.id === receipt.id);
      if (updated) await select(updated, epoch); else { setSelected(null); setSnapshot(null); }
      if (valid(epoch)) setComment("");
    }, value === "approved" ? "送审版本已通过并发布，成果全组可读。" : "已退回，提交人可补充后重新送审。");
  };
  return <section className="rw-phase5 rw-phase5-reviews" aria-busy={busy}>
    <PanelHeading title={user.role === "owner" ? "审核管理" : "我的提交"} subtitle={user.role === "owner" ? "审阅成员提交的指定版本，不开放来源知识库。" : "这里只显示你的提交、审核状态和反馈，不提供审批入口。"}><button disabled={busy || loading || !ready} onClick={refresh}>刷新提交</button></PanelHeading>
    <Feedback error={ready ? error : ""} notice={ready ? notice : ""} />
    <div className="rw-phase5-layout"><aside className="rw-phase5-list" aria-label="审核提交列表"><h3>{user.role === "owner" ? "待审成果" : "我的提交记录"}</h3>{loading || !ready ? <p role="status">正在加载提交…</p> : items.length ? items.map(review => <button key={review.id} className={`rw-doc-item ${receipt?.id === review.id ? "selected" : ""}`} disabled={busy} onClick={() => void select(review)}><span className="rw-file-icon">审</span><span><strong>{reviewTitle(review)} · {reviewNumber(review)}</strong><small>{stateLabel[review.status ?? review.decision ?? "pending"] ?? "状态待确认"} · {dateText(review.submitted_at ?? review.created_at)}</small><small>{review.source_space_kind ? `来自${kinds[review.source_space_kind]}` : "送审版本"}</small></span></button>) : <Empty>暂无送审记录。请从个人或协作知识库直接提交送审版本。</Empty>}</aside>
      <article className="rw-phase5-detail">{receipt ? <><p className="rw-eyebrow">送审内容</p><div className="rw-spread"><h3>{reviewTitle(receipt)} · {reviewNumber(receipt)}</h3><span className="rw-badge">{stateLabel[receipt.status ?? receipt.decision ?? "pending"] ?? "状态待确认"}</span></div><p className="rw-phase5-note">提交于 {dateText(receipt.submitted_at ?? receipt.created_at)}</p>
        <div className="rw-alert"><strong>本次可见范围：仅本次送审内容</strong><p>{receipt.source_space_kind ? `来源类型：${kinds[receipt.source_space_kind]}。` : ""}不会获得源库进入、检索或其他知识阅读权限。源版本或源库撤回后，本次提交内容仍保留以供复核。</p></div>
        {reading ? <p role="status">正在加载审核内容…</p> : body ? <><div className="rw-version-meta"><span>{body.filename || "文本知识"}</span><SourceLink source={body.source} /></div><div className="rw-document-content"><MarkdownMessage text={body.content ?? "审核内容暂未加载，请刷新后重试。"} /></div></> : <p className="rw-muted">未加载到审核内容，请重新选择提交或刷新。</p>}
        {receipt.comment && <div className="rw-alert"><strong>负责人反馈</strong><p>{receipt.comment}</p></div>}
        <div className="rw-phase5-publication"><h4>成果发布范围</h4><p>{receipt.published_version_id ? "已发布至成果知识库，全体项目成员可读。" : "审核通过后，仅将本次送审版本发布到成果知识库，全体项目成员可读。"}不会公开源库其他知识，也不会自动标记任务完成。</p></div>
        {canDecide ? <div className="rw-form"><label htmlFor="review-comment">审核意见<textarea id="review-comment" rows={4} value={comment} onChange={e => setComment(e.target.value)} disabled={busy} maxLength={10000} placeholder="说明认可依据或需要补充的内容…" /></label><div className="rw-actions"><button className="rw-primary" disabled={busy} onClick={() => decision("approved")}>通过并发布</button><button disabled={busy} onClick={() => decision("rejected")}>退回补充</button></div></div> : user.role === "owner" && pending && !reading ? <p className="rw-muted">{self ? "这是你自己的提交，不能审核自身成果。" : "此提交不可由你审核；提交人及来源链作者不能审核自身成果，最终以服务端权限为准。"}</p> : !pending ? <p className="rw-muted">此提交已处理，不能重复决定。</p> : null}
        {receipt.goal_id && <div className="rw-alert"><strong>关联项目目标</strong><p>{receipt.goal_path ?? receipt.goal_title ?? "历史目标"}{receipt.goal_archived ? "（已归档）" : ""}</p><p>关联只表示知识支撑关系，不改变可见范围，也不自动确认目标完成。</p></div>}
        <details className="rw-details"><summary>技术详情</summary><dl><dt>审核单 ID</dt><dd>{receipt.id}</dd><dt>送审版本 ID</dt><dd>{receipt.version_id}</dd>{receipt.task_id && <><dt>关联任务 ID</dt><dd>{receipt.task_id}</dd></>}</dl></details>
      </> : <Empty><h3>{user.role === "owner" ? "让成果经得起复核" : "每次提交，都有回执"}</h3><p>选择提交，阅读送审版本、审核状态与负责人反馈。</p></Empty>}</article>
    </div>
  </section>;
}
