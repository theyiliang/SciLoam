import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errorText, listOf, setCsrf } from "./api";
import type { Goal, Session, Space } from "./types";
import { roles, spaceKindLabel, spaceName as displaySpaceName, spaceRoles, spaceSummary } from "./types";
import Auth from "./Auth";
import Account from "./Account";
import Chat from "./Chat";
import Documents from "./Documents";
import Reviews from "./Reviews";
import Goals from "./Goals";
import Members from "./Members";
import Guide, { type GuideDestination } from "./Guide";
import { Empty, Feedback, useAction } from "./ui";
import "./research.css";

type Page = "chat" | "documents" | "reviews" | "goals" | "members" | "account";
const pages: { id: Page; title: string }[] = [{ id: "chat", title: "基于知识库问答" }, { id: "documents", title: "我的知识库" }, { id: "reviews", title: "审核管理" }, { id: "goals", title: "任务管理" }, { id: "members", title: "成员与权限" }, { id: "account", title: "账号与密码" }];

export default function ResearchApp() {
  const [session, setSession] = useState<Session | null>(null);
  const [checking, setChecking] = useState(true);
  const [sessionError, setSessionError] = useState("");
  const [sessionNotice, setSessionNotice] = useState("");
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [activeId, setActiveId] = useState("");
  const [page, setPage] = useState<Page>("chat");
  const [materialContext, setMaterialContext] = useState<{ scope: string; versionId: string } | null>(null);
  const [loadingSpaces, setLoadingSpaces] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [spaceName, setSpaceName] = useState("");
  const [goalId, setGoalId] = useState("");
  const [goals, setGoals] = useState<Goal[]>([]);
  const [menuOpen, setMenuOpen] = useState(false);
  const [documentsRefresh, setDocumentsRefresh] = useState(0);
  const [guideReopen, setGuideReopen] = useState(0);
  const [guideNotice, setGuideNotice] = useState("");
  const navigateGuide = (destination: GuideDestination) => {
    if (action.busy || loadingSpaces || document.querySelector('.rw-main [aria-busy="true"]')) { setGuideNotice("当前操作仍在运行，请完成或停止后再通过引导切换页面。"); return; }
    setGuideNotice(""); setMenuOpen(false);
    if (destination === "basis" || destination === "private") {
      const library = spaces.find(space => space.kind === destination);
      if (!library) { setGuideNotice(`暂无可访问的${destination === "basis" ? "项目" : "个人"}知识库，请刷新知识库或联系负责人。`); return; }
      setActiveId(library.id); setPage("documents"); return;
    }
    if (destination === "members" && session?.user?.role === "member") {
      const library = spaces.find(space => space.kind === "collaboration" && space.owner_id === session.user?.id);
      if (library) setActiveId(library.id);
      else { setPage("documents"); setShowCreate(true); setGuideNotice("请先创建或选择你拥有的协作知识库，再为具体成员授权；不会自动开放个人知识。"); return; }
    }
    setPage(destination);
  };
  const sessionEpoch = useRef(0);
  const action = useAction();
  const clearSession = () => {
    setMaterialContext(null); sessionEpoch.current++; setSession(null); setSpaces([]); setActiveId(""); setGoals([]); setShowCreate(false); setSpaceName(""); setGoalId(""); setPage("chat"); setMenuOpen(false); setLoadingSpaces(false); setGuideNotice(""); setGuideReopen(0); setCsrf(); action.setError(""); action.setNotice("");
  };
  const checkSession = async (signedOut = false) => {
    const epoch = sessionEpoch.current;
    setChecking(true); setSessionError("");
    try { const value = await api<Session>("/auth/me"); if (epoch === sessionEpoch.current) { setCsrf(value.csrf_token); setSession(signedOut ? null : value); } }
    catch (e) { if (epoch === sessionEpoch.current && !(e instanceof ApiError && e.status === 401)) setSessionError(errorText(e)); }
    finally { if (epoch === sessionEpoch.current) setChecking(false); }
  };
  useEffect(() => { let alive = true; api<Session>("/auth/me").then(value => { if (alive) { setCsrf(value.csrf_token); setSession(value); } }).catch(e => { if (alive && !(e instanceof ApiError && e.status === 401)) setSessionError(errorText(e)); }).finally(() => { if (alive) setChecking(false); }); return () => { alive = false; }; }, []);
  const refreshSpaces = useCallback(async () => {
    const epoch = sessionEpoch.current;
    const items = listOf<Space>(await api("/spaces"), "spaces");
    if (epoch !== sessionEpoch.current) return;
    setSpaces(items); setActiveId(previous => items.some(s => s.id === previous) ? previous : items[0]?.id ?? "");
  }, []);
  useEffect(() => {
    if (!session?.user) return;
    const epoch = sessionEpoch.current;
    let alive = true; setLoadingSpaces(true);
    void api("/spaces").then(data => { if (alive && epoch === sessionEpoch.current) { const items = listOf<Space>(data, "spaces"); setSpaces(items); setActiveId(previous => items.some(s => s.id === previous) ? previous : items[0]?.id ?? ""); } }).catch(e => { if (alive && epoch === sessionEpoch.current) action.setError(errorText(e)); }).finally(() => { if (alive && epoch === sessionEpoch.current) setLoadingSpaces(false); });
    void api("/goals").then(data => { if (alive && epoch === sessionEpoch.current) setGoals(listOf<Goal>(data, "goals")); }).catch(() => { /* Optional association must not block independent library loading. */ });
    return () => { alive = false; };
  }, [session?.user?.id]);
  const onPasswordChanged = async () => {
    clearSession(); setSessionNotice("密码修改成功，旧登录会话已撤销。请使用新密码重新登录。");
    await checkSession(true);
  };
  if (checking) return <div className="research-root"><div className="rw-loading" role="status"><div className="rw-wordmark">SciLoam</div><p>正在验证安全会话…</p></div></div>;
  if (!session?.user) return <div className="research-root">{sessionError && <div className="rw-session-error"><Feedback error={sessionError} /><button onClick={() => void checkSession()}>重新连接服务</button></div>}<Auth initialError={sessionError ? "暂无法验证服务连接，请先重试连接。" : ""} initialNotice={sessionNotice} onSession={value => { setSessionError(""); setSessionNotice(""); setSession(value); }} /></div>;
  const user = session.user;
  const activeSpace = spaces.find(s => s.id === activeId);
  const materialScope = `${user.id}:${activeSpace?.id}:${activeSpace?.role}:${activeSpace?.epoch ?? 0}`;
  const rememberMaterial = (versionId: string) => setMaterialContext(versionId ? { scope: materialScope, versionId } : null);
  const independentPage = page !== "chat";
  const pageTitle = page === "reviews" && user.role !== "owner" ? "我的提交" : pages.find(item => item.id === page)?.title;
  const libraryNavigation = <div className="rw-doc-library-nav"><div className="rw-space-heading"><span>知识库列表</span><button aria-label="创建协作知识库" disabled={action.busy} onClick={() => { setShowCreate(!showCreate); const epoch = sessionEpoch.current; void api("/goals").then(data => { if (epoch === sessionEpoch.current) setGoals(listOf<Goal>(data, "goals")); }).catch(e => { if (epoch === sessionEpoch.current) action.setError(errorText(e)); }); }}>＋</button></div>
      {showCreate && <form className="rw-space-create" onSubmit={e => { e.preventDefault(); void action.run(async () => { const created = await api<Space>("/spaces", "POST", { name: spaceName.trim(), kind: "collaboration", ...(goalId ? { goal_id: goalId } : {}) }); await refreshSpaces(); setActiveId(created.id); setSpaceName(""); setGoalId(""); setShowCreate(false); }, "协作知识库已创建。"); }}>
        <label>协作知识库名称<input value={spaceName} onChange={e => setSpaceName(e.target.value)} required maxLength={100} disabled={action.busy} /></label>
        <label>关联目标（可选）<select value={goalId} onChange={e => setGoalId(e.target.value)} disabled={action.busy}><option value="">不关联</option>{goals.map(goal => <option key={goal.id} value={goal.id}>{goal.path || goal.title}</option>)}</select><small>关联不改变知识库权限，也不代表目标完成。</small></label>
        <div className="rw-actions"><button className="rw-primary" disabled={action.busy || !spaceName.trim()}>创建</button><button type="button" disabled={action.busy} onClick={() => setShowCreate(false)}>取消</button></div>
      </form>}
      <div className="rw-space-list">{loadingSpaces && <p role="status">正在加载知识库…</p>}{spaces.map(space => <button key={space.id} className={activeId === space.id ? "active" : ""} aria-label={`选择知识库：${displaySpaceName(space)}`} aria-current={activeId === space.id ? "true" : undefined} onClick={() => { setActiveId(space.id); }}><span className={`rw-space-dot rw-space-${space.kind}`} /><span><strong>{displaySpaceName(space)}</strong><small>{spaceSummary(space) || <>{spaceKindLabel(space) && <>{spaceKindLabel(space)} · </>}{spaceRoles[space.role] ?? space.role}</>}</small></span>{activeId === space.id && <span className="rw-current-mark">‹</span>}</button>)}</div></div>;
  return <div className={`research-root ${page === "chat" ? "rw-chat-page" : ""}`}><div className="rw-shell">
    <aside className={`rw-sidebar ${menuOpen ? "rw-sidebar-open" : ""}`}>
      <div className="rw-sidebar-brand"><div className="rw-logo" aria-hidden="true">S</div><div><strong>SciLoam</strong><span>科研知识协同助手</span></div><button className="rw-mobile-close" aria-label="关闭导航" onClick={() => setMenuOpen(false)}>×</button></div>
      <div className="rw-project"><span className="rw-eyebrow">当前项目</span><strong>{session.project?.name ?? "研究项目"}</strong></div>
      <nav className="rw-nav" aria-label="工作台导航">{pages.map(item => <button key={item.id} className={page === item.id ? "active" : ""} aria-current={page === item.id ? "page" : undefined} onClick={() => { setPage(item.id); setMenuOpen(false); }}>{item.id === "reviews" && user.role !== "owner" ? "我的提交" : item.title}</button>)}</nav>
      <div className="rw-sidebar-bottom"><span className="rw-person-avatar">{user.email.slice(0, 1).toUpperCase()}</span><div><strong title={user.email}>{user.email}</strong><small>{roles[user.role]}</small></div><button disabled={action.busy} onClick={() => void action.run(async () => { await api("/auth/logout", "POST"); clearSession(); setSessionNotice(""); await checkSession(true); })}>退出</button></div>
    </aside>
    {menuOpen && <button className="rw-backdrop" aria-label="关闭导航" onClick={() => setMenuOpen(false)} />}
    <div className="rw-workspace">
      <header className="rw-topbar"><div className="rw-topbar-title"><button className="rw-menu-button" aria-label="打开导航" onClick={() => setMenuOpen(true)}>☰</button><span>工作台</span><span className="rw-separator">/</span><strong>{independentPage ? pageTitle : activeSpace ? displaySpaceName(activeSpace) : "尚未选择知识库"}</strong>{!independentPage && activeSpace && spaceKindLabel(activeSpace) && <span className="rw-badge">{spaceKindLabel(activeSpace)}</span>}</div><div className="rw-actions"><button className="rw-import-guide-trigger" onClick={() => setGuideReopen(previous => previous + 1)}>使用指南</button>{(page === "chat" || page === "documents" || page === "members") && <button disabled={action.busy || loadingSpaces} onClick={() => void action.run(async () => { await refreshSpaces(); if (page === "documents") setDocumentsRefresh(previous => previous + 1); }, "知识库已刷新。")}>刷新知识库</button>}</div></header>
      <main className="rw-main"><Feedback error={action.error} notice={guideNotice || action.notice} />
        <Guide key={`${user.id}:${user.role}`} user={user} reopen={guideReopen} onNavigate={navigateGuide} />
        <div className="rw-page-content">{page === "account" ? <Account key={`account-${user.id}`} user={user} onPasswordChanged={onPasswordChanged} /> : page === "goals" ? <Goals key={`goals-${user.id}`} user={user} /> : page === "reviews" ? <Reviews key={`reviews-${user.id}`} user={user} /> : page === "members" ? <Members key={`members-${user.id}`} user={user} space={activeSpace} spaces={spaces} onSelectSpace={setActiveId} onChanged={refreshSpaces} /> : activeSpace ? <div className="rw-scoped-page" key={`${page}-${activeSpace.id}-${activeSpace.role}-${activeSpace.epoch ?? 0}`}>
          {page === "chat" ? <Chat userId={user.id} space={activeSpace} spaces={spaces} onSelectSpace={setActiveId} onOpenKnowledge={() => setPage("documents")} initialSelectedVersionId={materialContext?.scope === materialScope ? materialContext.versionId : ""} onSelectVersion={rememberMaterial} /> : page === "documents" ? <Documents space={activeSpace} spaces={spaces} user={user} libraryNavigation={libraryNavigation} refreshToken={documentsRefresh} onAskVersion={versionId => { rememberMaterial(versionId); setPage("chat"); }} onSpaceDeleted={async () => { setActiveId(""); setSpaces(previous => previous.filter(space => space.id !== activeSpace.id)); await refreshSpaces(); }} /> : null}
        </div> : loadingSpaces ? <p role="status">正在加载知识库…</p> : <div className="rw-no-library">{page === "documents" && libraryNavigation}<Empty><h3>没有可访问的知识库</h3><p>可接受项目负责人邀请，或在知识页创建协作知识库。</p><button onClick={() => { setPage("documents"); setShowCreate(true); }}>创建协作知识库</button></Empty></div>}</div>
      </main>
      <footer className="rw-footer">知识以原文为准 · 权限由服务端校验 · 个人知识不会自动共享</footer>
    </div>
  </div></div>;
}
