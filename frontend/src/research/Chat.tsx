import { Fragment, useEffect, useRef, useState } from "react";
import MarkdownMessage from "../components/MarkdownMessage";
import { api, idPath, listOf, errorText, trackEvent } from "./api";
import { dateText, materialOptions, spaceKindLabel, spaceName, spaceRoles, type AnswerKind, type ChatReply, type Conversation, type Document, type MaterialOption, type Message, type Space, type SuggestedAction } from "./types";
import { Empty, Feedback } from "./ui";
import Citations from "./Citations";
import { accessLost, cancelChatRun, MAX_TENTATIVE_TEXT, replayExpired, startChatRun, watchChatRun } from "./stream";
import type { ChatRunEvent } from "./types";

type LiveRun = { controller: AbortController; id?: string; stopping: boolean; terminal: boolean; cancelSent?: boolean };
type TurnProcess = { question: string; events: ChatRunEvent[] };
const stageLabels: Partial<Record<ChatRunEvent["type"], string>> = { "document.read": "正在读取授权材料版本", "run.started": "研究任务开始", "retrieval.started": "正在检索授权资料", "retrieval.completed": "授权资料检索完成", "retrieval.skipped": "检索已跳过", "retrieval.degraded": "检索降级，结果可能不完整", "rerank.started": "正在重排授权候选依据", "rerank.completed": "依据重排完成", "rerank.skipped": "重排已跳过", "rerank.degraded": "重排降级，保留原候选顺序", "agent.started": "研究处理开始", "agent.completed": "研究处理完成", "answer.final": "回答生成完成", "run.failed": "任务未完成", "run.cancelled": "任务已停止", "run.completed": "回答已保存，需重新核验权限" };
export function canonicalMessages(items: Message[]): Message[] {
  const seen = new Set<string>();
  return items.filter(message => !message.id || (!seen.has(message.id) && !!seen.add(message.id))).map(message => {
    const citations = new Set<string>();
    return { ...message, citations: message.citations?.filter(citation => { const key = JSON.stringify([citation.id ?? null, citation.version_id, citation.chunk_start ?? citation.char_start, citation.chunk_end ?? citation.char_end, citation.content ?? citation.excerpt ?? citation.snippet]); if (citations.has(key)) return false; citations.add(key); return true; }) };
  });
}
const stageReasons: Record<string, string> = { not_configured: "未配置", no_candidates: "没有授权候选资料", progress_mode: "进展模式无需检索", legacy_adapter: "使用兼容检索", embedding_failed: "向量编码不可用", index_sync_failed: "索引同步未完成", vector_search_failed: "向量检索不可用", vector_unavailable: "向量索引不可用", rerank_failed: "重排服务不可用", backpressure: "处理容量已满", run_unavailable: "任务不可用", execution_deadline: "任务处理超时", cancelled: "已请求停止", consumer_timeout: "实时连接租约到期", shutdown: "服务正在关闭" };
const strategies: Record<string, string> = { lexical: "关键词检索", vector: "向量检索", hybrid_rrf: "混合检索", legacy_lexical: "兼容关键词检索" };
const stageDetail = (event: ChatRunEvent) => {
  const parts: string[] = [];
  if (event.type === "document.read" && typeof event.data.document_title === "string") parts.push(`《${event.data.document_title.slice(0, 300)}》`);
  if (typeof event.data.strategy === "string" && strategies[event.data.strategy]) parts.push(strategies[event.data.strategy]);
  for (const [field, label] of [["hit_count", "命中"], ["candidate_count", "候选"]]) { const count = event.data[field]; if (typeof count === "number" && Number.isSafeInteger(count) && count >= 0) parts.push(`${label} ${count}`); }
  const reasons = [event.data.reason, ...(Array.isArray(event.data.reasons) ? event.data.reasons : [])];
  parts.push(...new Set(reasons.filter((reason): reason is string => typeof reason === "string" && !!stageReasons[reason]).map(reason => stageReasons[reason])));
  return parts.length ? ` · ${parts.join(" · ")}` : "";
};
const chatModes = [{ value: "qa", label: "基于知识库问答" }, { value: "align", label: "观点对齐" }, { value: "progress", label: "进展梳理" }];
type ComposerPopover = "material" | "mode" | "help";
const agentLabels: Record<string, string> = { ResearchCoordinator: "科研主助手", KnowledgeQAAgent: "基于知识库问答", LiteratureAlignAgent: "观点对齐", TaskProgressAgent: "进展梳理" };
const bestEffortCancel = (run: LiveRun | null) => { if (run && !run.terminal) { run.stopping = true; if (run.id && !run.cancelSent) { run.cancelSent = true; void cancelChatRun(run.id).catch(() => {}); } run.controller.abort(); } };

export type ChatProps = { space: Space; userId?: string; spaces?: Space[]; onSelectSpace?: (spaceId: string) => void; onOpenKnowledge?: () => void; initialSelectedVersionId?: string; onSelectVersion?: (versionId: string) => void };
const statusTitles = { sufficient: "回答已生成", partial: "目前只能确认部分信息", insufficient: "本次未形成可用回答", conflicting: "来源存在分歧，尚不能形成一致结论" };
const isFailure = (message?: Pick<Message, "answer_kind" | "fallback" | "answer_status">) => message?.answer_kind === "failure" || (!!message?.fallback && (message.answer_kind === "clarification" || (!message.answer_kind && message.answer_status === "insufficient")));
const clarificationTitles = { document: "请选择要查看的文档", scope: "请确认统计范围", comparison: "请补充要比较的材料", other: "请补充具体信息" };
const answerKinds: Record<AnswerKind, { label: string; description: string }> = {
  general: { label: "通识回答", description: "来自模型内置的领域知识，未经本项目材料核验。" },
  grounded: { label: "证据回答", description: "论断由本项目材料的逐字引文支撑，引用见下方。" },
  mixed: { label: "混合回答", description: "通用框架与项目证据并列，分块标注来源。" },
  clarification: { label: "待澄清", description: "" },
  failure: { label: "本次回答未完成", description: "" },
};
const kindInfo = (message?: Message) => {
  if (isFailure(message)) return answerKinds.failure;
  if (message?.answer_kind === "clarification") return { label: clarificationTitles[message.clarification_kind ?? "other"] ?? clarificationTitles.other, description: "" };
  return message?.answer_kind && answerKinds[message.answer_kind];
};
// Legacy responses without a kind remain readable, without presuming every answer is a KB claim.
const needsEvidence = (message: Message) => !isFailure(message) && (message.answer_kind === "grounded" || (!message.answer_kind && !!message.answer_status));
const evidenceWarning = (message: Message) => needsEvidence(message) && (message.answer_kind === "grounded" || message.answer_status !== "insufficient") && !message.citations?.length;
// Suggestions are data, not commands. Never navigate to model URLs or execute tools.
const allowedActions: Record<string, "space" | "knowledge" | "question" | "material"> = { switch_library: "space", add_knowledge: "knowledge", rephrase_question: "question", select_material: "material" };
const actionKind = (action: SuggestedAction) => allowedActions[typeof action === "string" ? action : action.action ?? action.type ?? ""];

export default function Chat({ space, userId = "", spaces, onSelectSpace, onOpenKnowledge, initialSelectedVersionId = "", onSelectVersion }: ChatProps) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [active, setActive] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState("qa");
  const [materials, setMaterials] = useState<MaterialOption[]>([]);
  const [selectedVersionId, setSelectedVersionId] = useState(initialSelectedVersionId);
  const [materialsLoading, setMaterialsLoading] = useState(true);
  const [materialsError, setMaterialsError] = useState("");
  const [materialRevision, setMaterialRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [referencesOpen, setReferencesOpen] = useState(true);
  const [popover, setPopover] = useState<ComposerPopover | null>(null);
  const [materialSearch, setMaterialSearch] = useState("");
  const composer = useRef<HTMLFormElement>(null);
  const popoverPanel = useRef<HTMLDivElement>(null);
  const popoverTrigger = useRef<HTMLButtonElement | null>(null);
  const [popoverMaxHeight, setPopoverMaxHeight] = useState(300);
  const closePopover = (restoreFocus = false) => { setPopover(null); if (restoreFocus) popoverTrigger.current?.focus(); };
  const togglePopover = (name: ComposerPopover, trigger: HTMLButtonElement) => {
    if (busy) return;
    if (popover === name) { closePopover(true); return; }
    popoverTrigger.current = trigger;
    setMaterialSearch(""); setPopover(name);
  };
  const [citationMessage, setCitationMessage] = useState(-1);
  const [progress, setProgress] = useState<ChatRunEvent[]>([]);
  const [tentative, setTentative] = useState("");
  const [pendingQuestion, setPendingQuestion] = useState("");
  // Local presentation records belong to a verified answer, never to the next round.
  const [savedProcesses, setSavedProcesses] = useState<Record<string, TurnProcess>>({});
  const [stopping, setStopping] = useState(false);
  const liveRun = useRef<LiveRun | null>(null);
  const scope = `${userId}:${space.id}:${space.role}:${space.epoch ?? 0}`;
  const [loadedScope, setLoadedScope] = useState(scope);
  const currentScope = useRef(scope);
  const sequence = useRef(0);
  // Invalidate during render too: effects may not have cleaned up when a promise resolves.
  if (currentScope.current !== scope) { currentScope.current = scope; sequence.current++; }
  const lock = useRef<object | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const libraryList = useRef<HTMLDivElement>(null);
  const availableSpaces = spaces ?? [space];
  const visibleMessages = loadedScope === scope ? messages : [];
  const visibleHistory = loadedScope === scope ? conversations : [];
  const selectedMessage = visibleMessages[citationMessage];
  const valid = (requestScope: string, seq: number) => currentScope.current === requestScope && sequence.current === seq;
  const loadHistory = async (requestScope: string, seq: number) => {
    const items = listOf<Conversation>(await api(`/conversations?space_id=${idPath(space.id)}`), "conversations");
    if (valid(requestScope, seq)) setConversations(items);
  };
  useEffect(() => {
    const seq = ++sequence.current;
    bestEffortCancel(liveRun.current); liveRun.current = null;
    lock.current = null; setBusy(false); setLoading(true); setLoadedScope(scope);
    setConversations([]); setActive(""); setMessages([]); setQuestion(""); setError(""); setNotice(""); setCitationMessage(-1); setReferencesOpen(true); setTentative(""); setProgress([]); setPendingQuestion(""); setSavedProcesses({}); setStopping(false);
    void loadHistory(scope, seq).catch(e => { if (valid(scope, seq)) setError(errorText(e)); }).finally(() => { if (valid(scope, seq)) setLoading(false); });
    return () => { sequence.current++; lock.current = null; bestEffortCancel(liveRun.current); liveRun.current = null; };
  }, [scope]);
  useEffect(() => {
    let alive = true;
    setMaterials([]); setMaterialsLoading(true); setMaterialsError("");
    const requestedVersion = loadedScope === scope ? selectedVersionId : initialSelectedVersionId;
    setSelectedVersionId(requestedVersion);
    void api(`/spaces/${idPath(space.id)}/documents`).then(data => {
      if (!alive || currentScope.current !== scope) return;
      const options = materialOptions(listOf<Document>(data, "documents"));
      setMaterials(options);
      if (requestedVersion && !options.some(option => option.version_id === requestedVersion)) {
        setSelectedVersionId(""); onSelectVersion?.("");
        setMaterialsError("原先选择的材料版本已不可用，请重新选择；不会自动改用其他版本。");
      }
    }).catch(e => {
      if (!alive || currentScope.current !== scope) return;
      setSelectedVersionId(""); onSelectVersion?.(""); setMaterialsError(`材料列表加载失败：${errorText(e)} 可不指定材料继续提问。`);
    }).finally(() => { if (alive && currentScope.current === scope) setMaterialsLoading(false); });
    return () => { alive = false; };
  }, [scope, materialRevision]);
  useEffect(() => { setPopover(null); setMaterialSearch(""); }, [scope]);
  useEffect(() => { if (busy) setPopover(null); }, [busy]);
  useEffect(() => {
    if (!popover || typeof document === "undefined") return;
    const resize = () => { const top = composer.current?.getBoundingClientRect().top ?? 380; const boundary = composer.current?.closest(".rw-chat-center")?.getBoundingClientRect().top ?? 0; setPopoverMaxHeight(Math.max(0, Math.min(360, top - boundary - 12))); };
    resize();
    const panel = popoverPanel.current;
    const content = panel?.querySelector<HTMLElement>(".rw-popover-heading + div:not([hidden]), .rw-popover-heading ~ div:not([hidden])");
    (content?.querySelector<HTMLElement>("input, select, button") ?? panel?.querySelector<HTMLElement>("button"))?.focus();
    const outside = (event: PointerEvent) => { if (!panel?.contains(event.target as Node) && !popoverTrigger.current?.contains(event.target as Node)) closePopover(); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { event.preventDefault(); closePopover(true); } };
    document.addEventListener("pointerdown", outside); document.addEventListener("keydown", escape); window.addEventListener("resize", resize);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); window.removeEventListener("resize", resize); };
  }, [popover]);
  useEffect(() => { const element = input.current; if (element) { element.style.height = "44px"; element.style.height = `${Math.max(44, Math.min(140, element.scrollHeight))}px`; } }, [question, scope]);
  const selectedMaterial = loadedScope === scope ? materials.find(option => option.version_id === selectedVersionId) : undefined;
  const chooseMaterial = (versionId: string) => {
    const action_type = versionId ? "select_document" : "deselect_document";
    trackEvent("user.session_action", action_type, { space_id: space.id, conversation_id: active || undefined, document_version_id: versionId || undefined });
    setSelectedVersionId(versionId); onSelectVersion?.(versionId);
  };
  useEffect(() => { bottom.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, [messages, busy, tentative, progress]);
  const clearAccess = () => { setMaterials([]); setSelectedVersionId(""); onSelectVersion?.(""); setMessages([]); setConversations([]); setActive(""); setQuestion(""); setCitationMessage(-1); setTentative(""); setProgress([]); setPendingQuestion(""); setSavedProcesses({}); setNotice(""); };
  const stop = () => {
    const current = liveRun.current;
    if (!current || current.stopping || current.terminal) return;
    current.stopping = true; setStopping(true); setTentative(""); setNotice("正在停止任务；临时回答未作为完成答案保存。");
    // If start is still pending, its continuation sends the cancel once id is known.
    if (current.id) {
      const stopScope = scope, stopSeq = sequence.current;
      current.cancelSent = true;
      void cancelChatRun(current.id).then(() => { if (valid(stopScope, stopSeq)) setNotice("任务已停止；临时回答未作为完成答案，请刷新历史核对是否已有完整回答。"); }).catch(e => {
        if (!valid(stopScope, stopSeq)) return;
        if (accessLost(e)) { clearAccess(); setError("登录或访问权限已失效，已清除回答，请重新验证权限。"); }
        else setError(`${errorText(e)} 停止确认失败，请刷新历史核对；不会把临时回答当作完成答案。`);
      });
    }
    current.controller.abort();
  };

  const run = async (operation: (seq: number, requestScope: string) => Promise<void>) => {
    if (lock.current) return;
    const token = {}; lock.current = token;
    const seq = ++sequence.current; const requestScope = scope;
    setBusy(true); setError(""); setNotice("");
    try { await operation(seq, requestScope); }
    catch (e) { if (valid(requestScope, seq)) { if (accessLost(e)) clearAccess(); setError(accessLost(e) ? "登录或访问权限已失效，已清除回答，请重新验证权限。" : errorText(e)); } }
    finally { if (lock.current === token) { lock.current = null; if (valid(requestScope, seq)) { setBusy(false); setLoading(false); } } }
  };
  const select = (id: string) => void run(async (seq, requestScope) => {
    setActive(id); setMessages([]); setCitationMessage(-1); setQuestion(""); setProgress([]); setTentative(""); setPendingQuestion(""); setSavedProcesses({});
    const result = await api<Conversation>(`/conversations/${idPath(id)}`);
    if (!valid(requestScope, seq)) return;
    if (result.space_id && result.space_id !== space.id) throw Object.assign(new Error("会话不属于当前知识库，请刷新历史。"), { status: 403 });
    setMessages(canonicalMessages(result.messages ?? [])); setCitationMessage((result.messages?.length ?? 0) - 1);
  });
  const submit = () => {
    const text = question.trim(); if (!text || loading || loadedScope !== scope || (mode !== "progress" && materialsLoading && selectedVersionId)) return;
    void run(async (seq, requestScope) => {
      let conversation = active;
      const current: LiveRun = { controller: new AbortController(), stopping: false, terminal: false };
      liveRun.current = current; setTentative(""); setProgress([]); setPendingQuestion(text); setStopping(false);
      let preview = "";
      const turnEvents: ChatRunEvent[] = [];
      try {
        if (!conversation) {
          const created = await api<Conversation>("/conversations", "POST", { space_id: space.id });
          if (!valid(requestScope, seq)) return;
          conversation = created.id; setActive(conversation);
          await loadHistory(requestScope, seq);
          if (!valid(requestScope, seq)) return;
        } else {
          trackEvent("user.session_action", "new_question_in_same_conv", { conversation_id: conversation, space_id: space.id });
        }
        if (current.stopping) return;
        const identity = { space_id: space.id, conversation_id: conversation, request_id: crypto.randomUUID() };
        const started = await startChatRun({ ...identity, question: text, mode, ...(selectedMaterial && mode !== "progress" ? { selected_version_id: selectedMaterial.version_id } : {}) });
        if (!started || typeof started.run_id !== "string" || !started.run_id || typeof started.message_id !== "string" || !started.message_id) throw new Error("服务未返回有效任务编号，请刷新历史。");
        current.id = started.run_id;
        if (!valid(requestScope, seq) || current.stopping) { bestEffortCancel(current); return; }
        if ((started.space_id !== undefined && started.space_id !== space.id) || (started.conversation_id !== undefined && started.conversation_id !== conversation) || (started.request_id !== undefined && started.request_id !== identity.request_id)) throw Object.assign(new Error("服务返回的任务作用域不一致，请检查历史。"), { status: 403 });
        const terminal = await watchChatRun({ ...identity, run_id: started.run_id, message_id: started.message_id }, current.controller.signal, event => {
          if (!valid(requestScope, seq) || current.stopping) return;
          if (event.type === "answer.delta") {
            preview += event.data.text as string;
            if (preview.length > MAX_TENTATIVE_TEXT) throw new Error("临时回答过大，已停止显示；请刷新历史。");
            setTentative(preview);
          } else {
            turnEvents.push(event); if (turnEvents.length > 100) turnEvents.shift();
            setProgress([...turnEvents]);
            if (["answer.final", "run.failed", "run.cancelled", "run.completed"].includes(event.type)) setTentative("");
          }
        }, attempt => { if (valid(requestScope, seq) && !current.stopping) { setNotice(`实时连接中断，保留已校验片段，正在重连同一任务（${attempt}/2）；不会重复提交问题。`); } });
        current.terminal = true;
        if (!valid(requestScope, seq) || current.stopping) return;
        if (terminal.type === "run.completed") { clearAccess(); setNotice("回答已保存；请重新验证权限并查看历史。不会重新发送问题或显示撤权前正文。"); return; }
        if (terminal.type !== "answer.final") {
          setTentative(""); setNotice(terminal.type === "run.cancelled" ? "任务已停止，临时回答未作为完成答案；请刷新历史核对。" : "任务未完成，临时回答已清除；请刷新历史检查是否已有完整保存的回答。");
          return;
        }
        const result = terminal.data as unknown as ChatReply;
        if (result.conversation_id !== conversation || typeof result.answer !== "string") throw new Error("服务返回的会话或最终回答不一致，请检查对话历史。");
        // The persisted conversation is canonical, including after a previous timeout.
        const saved = await api<Conversation>(`/conversations/${idPath(conversation)}`);
        if (!valid(requestScope, seq)) return;
        if (saved.space_id && saved.space_id !== space.id) throw Object.assign(new Error("会话不属于当前知识库。"), { status: 403 });
        const restored = canonicalMessages(saved.messages ?? []);
        const last = restored.findIndex(message => message.id === started.message_id);
        if (result.message_id !== started.message_id || last < 0 || restored[last].role !== "assistant" || (restored[last].content ?? restored[last].text ?? restored[last].answer) !== result.answer) throw new Error("尚未读到本次已保存的完整回答，请刷新历史核对。");
        // Compatibility with servers that do not yet persist presentation metadata.
        if ((restored[last].content ?? restored[last].answer) === result.answer) restored[last] = { ...restored[last], citations: result.citations ?? restored[last].citations, events: restored[last].events ?? result.events, fallback: restored[last].fallback ?? result.fallback, answer_kind: restored[last].answer_kind ?? result.answer_kind, clarification_kind: restored[last].clarification_kind ?? result.clarification_kind, answer_status: restored[last].answer_status ?? result.answer_status, missing_evidence: restored[last].missing_evidence ?? result.missing_evidence, suggested_actions: restored[last].suggested_actions ?? result.suggested_actions };
        setSavedProcesses(items => ({ ...items, [started.message_id]: { question: text, events: [...turnEvents] } }));
        setPendingQuestion(""); setProgress([]);
        setTentative(""); setMessages(canonicalMessages(restored)); setCitationMessage(last);
        if (!isFailure(restored[last])) setQuestion("");
        setNotice(isFailure(restored[last]) ? "本次回答未完成，问题已保留；可修改后发送或重试本问题。" : "回答已保存。");
        try { await loadHistory(requestScope, seq); } catch (e) { if (valid(requestScope, seq)) { if (accessLost(e)) { clearAccess(); setError("登录或访问权限已失效，已清除回答，请重新验证权限。"); } else setNotice("回答已保存，但历史列表刷新失败。可手动刷新历史。"); } }
      } catch (e) {
        if (!valid(requestScope, seq)) return;
        setTentative("");
        if (accessLost(e)) { clearAccess(); setError("登录或访问权限已失效，已清除回答，请重新验证权限后重新进入知识库。"); }
        else if (replayExpired(e) && (e as { status?: number }).status === 404) { clearAccess(); setError("任务已过期或不可访问，已清除显示内容；请重新验证权限并刷新对话历史，不会重放临时回答。"); }
        else if (current.stopping) setNotice("任务已请求停止；临时回答未作为完成答案，请刷新历史核对。");
        else {
          // No automatic POST retry: a timeout can still mean the server persisted it.
          setError(`${errorText(e)} ${replayExpired(e) ? "实时记录无法重放；" : ""}输入已保留；请先刷新并检查对话历史，确认是否已有回答，再决定是否重新发送。`);
        }
      } finally {
        if (!current.terminal) bestEffortCancel(current);
        if (liveRun.current === current) { liveRun.current = null; if (valid(requestScope, seq)) setStopping(false); }
      }
    });
  };
  const newConversation = () => {
    if (lock.current) return;
    sequence.current++; bestEffortCancel(liveRun.current); liveRun.current = null; setTentative(""); setProgress([]); setPendingQuestion(""); setSavedProcesses({}); setActive(""); setMessages([]); setQuestion(""); setError(""); setNotice(""); setCitationMessage(-1); setLoading(false);
  };
  const pickLibrary = () => { libraryList.current?.focus(); setNotice(onSelectSpace ? "请在左侧选择有权访问的知识库；切换后使用独立会话。" : "当前仅连接此知识库，请先从工作台选择其他授权知识库。"); };
  const revise = () => { input.current?.focus(); setNotice("请在下方补充研究对象、资料名称或需要核对的范围，再发送问题。"); };
  const retryQuestion = (text: string) => {
    if (busy || loadedScope !== scope) return;
    setQuestion(text); input.current?.focus();
    setNotice("已填回原问题，可修改后发送；尚未重新提交。");
  };
  const pickMaterial = (trigger: HTMLButtonElement) => {
    if (busy || mode === "progress" || loadedScope !== scope) return;
    popoverTrigger.current = trigger;
    setMaterialSearch(""); setPopover("material");
  };
  const evidenceActions = (message: Message) => {
    if (isFailure(message)) return null;
    const needed = needsEvidence(message) && (message.answer_status !== "sufficient" || !message.citations?.length);
    const choices = new Set((message.suggested_actions ?? []).map(actionKind).filter(Boolean));
    if (needed) { choices.add("space"); choices.add("knowledge"); choices.add("question"); }
    const proc = message.id ? savedProcesses[message.id] : undefined;
    const runId = proc?.events?.[0]?.run_id;
    return <div className="rw-actions">{choices.has("material") && <button type="button" disabled={busy || mode === "progress" || loadedScope !== scope} onClick={e => pickMaterial(e.currentTarget)}>选择材料</button>}{choices.has("space") && <button type="button" onClick={pickLibrary}>切换知识库</button>}{choices.has("knowledge") && onOpenKnowledge && <button type="button" onClick={onOpenKnowledge}>补充相关知识</button>}{choices.has("question") && <button type="button" onClick={revise}>修改问题</button>}<button type="button" className="rw-feedback-up" aria-label="这条回答有帮助" onClick={() => trackEvent("user.answer_feedback", "thumbs_up", { run_id: runId, conversation_id: active || undefined, space_id: space.id })}>👍</button><button type="button" className="rw-feedback-down" aria-label="这条回答没有帮助" onClick={() => trackEvent("user.answer_feedback", "thumbs_down", { run_id: runId, conversation_id: active || undefined, space_id: space.id })}>👎</button></div>;
  };
  const stageText = (event: ChatRunEvent) => `${stageLabels[event.type] ?? "处理进度"}${typeof event.data.agent === "string" && agentLabels[event.data.agent] ? ` · ${agentLabels[event.data.agent]}` : ""}${stageDetail(event)}`;
  const processView = (events: ChatRunEvent[], saved = false, failed = false) => {
    if (!events.length) return null;
    const latest = events[events.length - 1];
    const degraded = events.filter(event => event.type.endsWith(".degraded"));
    return <details className="rw-run-progress" aria-label={saved ? "已保存的处理进度" : "实时处理进度"}><summary>{saved && failed ? "处理结束 · " : saved ? "已完成 · " : latest.type === "run.failed" ? "未完成 · " : latest.type === "run.cancelled" ? "已停止 · " : latest.type === "run.completed" ? "待核验 · " : "处理中 · "}{saved && failed ? "本次回答未完成" : stageText(latest)}{degraded.length > 0 && degraded[degraded.length - 1] !== latest && <span className="rw-process-degraded"> · {stageText(degraded[degraded.length - 1])}</span>}<span className="rw-process-expand"> · 展开过程</span></summary><ol>{events.map(event => <li key={`${event.run_id}:${event.seq}`}><time dateTime={event.timestamp}>{dateText(event.timestamp)}</time><span>{stageText(event)}</span></li>)}</ol></details>;
  };
  const compactSuccess = notice === "回答已保存。";
  return <section className="rw-chat rw-reading-first" aria-label="基于知识库问答">
    <header className="rw-chat-heading"><div><h2>基于知识库问答</h2><p>自然提问，按需结合授权材料。材料事实以引用原文为准。</p></div><div className="rw-reading-actions"><button type="button" aria-expanded={referencesOpen} aria-controls="rw-answer-evidence" onClick={() => setReferencesOpen(!referencesOpen)}>{referencesOpen ? "收起回答依据" : "展开回答依据"}</button></div></header>
    <Feedback error={loadedScope === scope ? error : ""} notice={loadedScope === scope && !compactSuccess ? notice : ""} />
    <div className={`rw-chat-layout ${referencesOpen ? "" : "rw-chat-folded"}`}>
      <aside className="rw-chat-libraries" aria-label="知识库与历史">
        <h3>当前可访问的知识库</h3>
        <div ref={libraryList} tabIndex={-1} aria-label="选择授权知识库">{availableSpaces.map(item => <button key={item.id} type="button" className={`rw-chat-library ${item.id === space.id ? "selected" : ""}`} aria-current={item.id === space.id ? "true" : undefined} disabled={!onSelectSpace || busy || item.id === space.id} onClick={() => { if (!lock.current) { trackEvent("user.session_action", "switch_space", { from_space_id: space.id, to_space_id: item.id }); onSelectSpace?.(item.id); } }}><strong>{spaceName(item)}</strong><small>{spaceKindLabel(item) && <>{spaceKindLabel(item)} · </>}{spaceRoles[item.role] ?? item.role}{item.id === space.id ? " · 当前" : ""}</small></button>)}</div>
        <p className="rw-chat-note">列表仅显示你有权访问的知识库。负责人也不默认访问协作库。</p>
        {onOpenKnowledge && <button type="button" className="rw-chat-manage" onClick={onOpenKnowledge}>知识库 / 创建协作库</button>}
        <div className="rw-chat-history-heading"><h3>对话历史</h3><button type="button" disabled={busy} onClick={newConversation}>＋ 新对话</button></div>
        <button type="button" disabled={busy || loading} onClick={() => void run((seq, requestScope) => loadHistory(requestScope, seq))}>刷新历史</button>
        {loading ? <p role="status">正在加载历史…</p> : visibleHistory.length ? visibleHistory.map(item => <button key={item.id} type="button" className={`rw-chat-history-item ${active === item.id ? "selected" : ""}`} disabled={busy} onClick={() => select(item.id)}><strong>{item.title || "新的研究问题"}</strong><small>{item.created_at ? dateText(item.created_at) : "当前知识库会话"}</small></button>) : <p className="rw-chat-note">还没有对话，从第一个问题开始。</p>}
      </aside>
      <div className="rw-chat-center">
        <div className="rw-chat-status"><strong>{spaceName(space)}</strong><span className="rw-badge">知识增强</span>{loadedScope === scope && compactSuccess && <span className="rw-saved-notice" role="status">✓ {notice}</span>}</div>
        <div className="rw-chat-messages" aria-live="polite" aria-busy={busy}>
          {!visibleMessages.length && !busy && !pendingQuestion && <Empty><h3>我是您的科研协同助手</h3><p>可以讨论概念、研究方法与建议，也可以结合材料核对项目事实、对齐研究口径，或了解已记录的任务进展。</p></Empty>}
          {visibleMessages.map((message, index) => {
            const process = message.role === "assistant" && message.id ? savedProcesses[message.id] : undefined;
            const failed = isFailure(message);
            const info = kindInfo(message);
            const originalUser = failed ? visibleMessages.slice(0, index).reverse().find(item => item.role === "user") : undefined;
            const originalQuestion = process?.question ?? originalUser?.content ?? originalUser?.text ?? originalUser?.answer;
            return <Fragment key={message.id ?? index}>
            {process && <>{/* Some older history responses omit the user record. */}{visibleMessages[index - 1]?.role !== "user" && <article className="rw-chat-message rw-chat-user"><MarkdownMessage text={process.question} /></article>}{processView(process.events, true, isFailure(message))}</>}
            <article className={`rw-chat-message ${message.role === "user" ? "rw-chat-user" : "rw-chat-assistant"}`}>
            {message.role !== "user" && <div className="rw-chat-assistant-name"><span aria-hidden="true">S</span><strong>SciLoam</strong>{message.fallback && !isFailure(message) && <span className="rw-badge rw-warning">{message.answer_status === "insufficient" ? "未生成模型回答" : "原文摘录 · 非 AI 总结"}</span>}</div>}
            {message.role !== "user" && info && (failed || message.answer_kind === "clarification") && <p className="rw-chat-note"><strong>{info.label}</strong></p>}
            {message.role !== "user" && message.answer_status && message.answer_status !== "sufficient" && needsEvidence(message) && <h3 className={`rw-chat-answer-status rw-chat-${message.answer_status}`}>{statusTitles[message.answer_status]}</h3>}
            {message.role !== "user" && evidenceWarning(message) && <p className="rw-chat-evidence-warning">当前没有可核验的引用依据，不应将以下内容当作已证实的结论。</p>}
            <MarkdownMessage text={message.content ?? message.text ?? message.answer ?? ""} />
            {message.role !== "user" && !failed && (info?.description || (message.answer_status === "sufficient" && needsEvidence(message)) || message.fallback) && <details className="rw-details rw-chat-answer-details"><summary>回答说明</summary>{info?.description && <p><strong>{info.label}</strong> · {info.description}</p>}{message.answer_status === "sufficient" && needsEvidence(message) && <p className="rw-chat-sufficient">{statusTitles.sufficient}</p>}{message.fallback && <p>本次为本地原文摘录，非模型流式生成。</p>}</details>}
            {message.role !== "user" && failed && originalQuestion && <div className="rw-actions"><button type="button" disabled={busy || loadedScope !== scope} onClick={() => retryQuestion(originalQuestion)}>重试本问题</button></div>}
            {message.role !== "user" && <>{!isFailure(message) && (Array.isArray(message.missing_evidence) ? message.missing_evidence.length > 0 : !!message.missing_evidence) && !(message.content ?? message.text ?? message.answer ?? "").includes("尚缺证据：") && <div className="rw-chat-missing"><strong>还缺少的依据</strong>{Array.isArray(message.missing_evidence) ? <ul>{message.missing_evidence.map((item, i) => <li key={i}>{item}</li>)}</ul> : <p>{message.missing_evidence}</p>}</div>}{evidenceActions(message)}{message.citations?.length ? <button type="button" className="rw-chat-show-citations" onClick={() => { setCitationMessage(index); setReferencesOpen(true); }}>查看这条回答的 {message.citations.length} 条依据</button> : null}{message.events?.length && Array.isArray(message.events) ? processView((message.events as Record<string, unknown>[]).map((e, i) => ({ run_id: "", seq: i + 1, timestamp: "", type: String(e.type ?? "") as ChatRunEvent["type"], data: Object.fromEntries(Object.entries(e).filter(([k]) => k !== "type")) })), true, false) : null}</>}
          </article></Fragment>; })}
          {loadedScope === scope && pendingQuestion && <article className="rw-chat-message rw-chat-user"><MarkdownMessage text={pendingQuestion} /></article>}
          {loadedScope === scope && processView(progress)}
          {loadedScope === scope && tentative && <section className="rw-run-tentative" aria-label="临时回答（未保存）"><h3>临时回答 · 尚未保存</h3><p className="rw-chat-note">仅显示服务端已校验片段；以完成后保存的回答为准，停止或权限变化会清除。</p><MarkdownMessage text={tentative} /></section>}
          {busy && loadedScope === scope && <p className="rw-chat-note" role="status">正在处理科研问题，按需读取授权资料并实时显示服务进度，运行期间暂不能切换知识库或历史。</p>}<div ref={bottom} />
        </div>
        {loadedScope === scope && materialsError && <p className="rw-material-error" role="status">{materialsError}</p>}
        <form ref={composer} className="rw-chat-composer" onSubmit={e => { e.preventDefault(); submit(); }}>
          <div id="rw-composer-options" ref={popoverPanel} className={`rw-composer-popover rw-composer-${popover ?? "closed"}`} hidden={!popover || busy || loadedScope !== scope} role="dialog" aria-label={popover === "material" ? "选择当前材料" : popover === "mode" ? "选择问答模式" : "范围与使用说明"} style={{ maxHeight: popoverMaxHeight }}>
            <div className="rw-popover-heading"><strong>{popover === "material" ? "选择当前材料" : popover === "mode" ? "选择问答模式" : "范围与使用说明"}</strong><button type="button" aria-label="关闭选项" onClick={() => closePopover(true)}>×</button></div>
            <div hidden={popover !== "material"} className="rw-material-picker">
              <input aria-label="搜索文档标题" placeholder="搜索文档标题…" value={materialSearch} onChange={e => setMaterialSearch(e.target.value)} />
              <label htmlFor="rw-material">当前材料（可选）</label><select id="rw-material" value={selectedMaterial?.version_id ?? ""} disabled={busy || mode === "progress" || materialsLoading || loadedScope !== scope} onChange={e => { chooseMaterial(e.target.value); closePopover(true); }}><option value="">不指定材料 · 按问题使用授权知识库</option>{(loadedScope === scope ? materials : []).filter(option => option.version_id === selectedVersionId || option.title.toLocaleLowerCase().includes(materialSearch.trim().toLocaleLowerCase())).map(option => <option key={option.version_id} value={option.version_id}>{option.title} · v{option.number ?? "—"}{option.current ? " · 当前版本" : " · 历史版本"}</option>)}</select>
              <button type="button" disabled={busy || materialsLoading} onClick={() => setMaterialRevision(value => value + 1)}>刷新材料</button>{materialsLoading && <small role="status">正在加载可选材料…</small>}
              <p>仅明确选择后才使用此版本；不选择不会默认使用第一份文档。</p>
            </div>
            <div hidden={popover !== "mode"}><label htmlFor="rw-mode">模式</label><select id="rw-mode" value={mode} onChange={e => { setMode(e.target.value); closePopover(true); }} disabled={busy}>{chatModes.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select><p>基于知识库问答：通用讨论，按需结合材料。观点对齐：对照来源与差异。进展梳理：仅核实已记录进展，不使用选中材料。</p></div>
            <div hidden={popover !== "help"}><p>选中材料限定明确指代，不意味着每次必须读取。材料事实请结合引用原文核验；以已保存终稿为准。</p><p>本地摘录不伪装成流式生成。运行期间暂不能切换知识库或历史。</p></div>
          </div>
          <div className="rw-composer-tools">
            {selectedMaterial && <div className="rw-current-material" role="group" aria-label="当前材料"><span title={`《${selectedMaterial.title}》 · v${selectedMaterial.number ?? "—"}`}>{mode === "progress" ? "已选材料（进展模式不使用）" : "当前材料"}：《{selectedMaterial.title}》 · v{selectedMaterial.number ?? "—"}{selectedMaterial.current ? " · 当前版本" : " · 历史版本"}</span><button type="button" aria-label="清除当前材料" disabled={busy} onClick={() => chooseMaterial("")}>×</button></div>}
            <button type="button" className="rw-composer-tool" aria-expanded={popover === "material"} aria-controls="rw-composer-options" disabled={busy || mode === "progress" || loadedScope !== scope} onClick={e => togglePopover("material", e.currentTarget)}>＋ 材料</button>
            <button type="button" className="rw-composer-tool rw-mode-trigger" aria-expanded={popover === "mode"} aria-controls="rw-composer-options" disabled={busy} onClick={e => togglePopover("mode", e.currentTarget)}>{chatModes.find(item => item.value === mode)?.label} ▾</button>
          </div>
          <div className="rw-composer-input"><label className="rw-visually-hidden" htmlFor="rw-question">提出科研问题</label><textarea ref={input} id="rw-question" value={loadedScope === scope ? question : ""} onChange={e => setQuestion(e.target.value)} placeholder="输入问题，或继续追问……" rows={2} required disabled={busy} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); } }} />
            {busy && liveRun.current ? <button type="button" className="rw-composer-stop" aria-label={stopping ? "正在停止回答" : "停止回答"} onClick={stop} disabled={stopping}>{stopping ? "正在停止…" : "停止回答"}</button> : <button className="rw-primary rw-composer-send" aria-label="发送问题 ↑" title="发送问题 ↑" disabled={busy || loading || !!(mode !== "progress" && materialsLoading && selectedVersionId) || !question.trim()}><span aria-hidden="true">↑</span><span className="rw-visually-hidden">发送问题 ↑</span></button>}
          </div>
          <div className="rw-composer-foot"><span>Enter 发送 · Shift + Enter 换行</span><button type="button" disabled={busy} aria-expanded={popover === "help"} aria-controls="rw-composer-options" onClick={e => togglePopover("help", e.currentTarget)}>范围与说明 ⓘ</button></div>
        </form>
      </div>
      {referencesOpen && <aside id="rw-answer-evidence" className="rw-chat-references" aria-label="回答依据"><h3>回答依据</h3>{selectedMessage?.citations?.length ? <Citations key={`${scope}-${active}-${citationMessage}`} items={selectedMessage.citations} /> : <p className="rw-chat-note">{selectedMessage?.role === "assistant" ? isFailure(selectedMessage) ? "本次回答未完成，暂无可核验的回答依据。请查看回答中的处理说明。" : needsEvidence(selectedMessage) ? "这条材料回答暂未附可核验引用，请核对回答中的依据限制；需要时可切换知识库或补充材料。" : selectedMessage.answer_kind === "mixed" ? "本条未附材料引用。通用建议不等于项目事实，请留意回答中说明的材料缺失与适用范围。" : "本条未附材料引用；通用讨论与澄清不要求知识库命中，不代表已核实项目事实。" : "提问后，可在这里核对引用片段与版本原文。"}</p>}<p className="rw-chat-note">仅展示当前所选回答的引用；原文访问仍由服务端校验权限。</p></aside>}
    </div>
  </section>;
}
