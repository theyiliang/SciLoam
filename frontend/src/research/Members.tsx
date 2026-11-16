import { useEffect, useRef, useState } from "react";
import { api, errorText, idPath, listOf } from "./api";
import type { Member, Space, User } from "./types";
import { activeMember, canManageSpace, memberId, memberName, roles, spaceName, spaceRoles } from "./types";
import { Empty, Feedback, PanelHeading, useAction } from "./ui";
import { SafetyLesson } from "./Guide";

export default function Members({ user, space, spaces = space ? [space] : [], onSelectSpace, onChanged }: { user: User; space?: Space; spaces?: Space[]; onSelectSpace?: (id: string) => void; onChanged: () => Promise<void> }) {
  const [members, setMembers] = useState<Member[]>([]);
  const [access, setAccess] = useState<Member[]>([]);
  const [email, setEmail] = useState("");
  const [inviteToken, setInviteToken] = useState("");
  const [acceptToken, setAcceptToken] = useState("");
  const [chosen, setChosen] = useState("");
  const [role, setRole] = useState("reader");
  const [loading, setLoading] = useState(true);
  const [peopleLoading, setPeopleLoading] = useState(true);
  const [peopleError, setPeopleError] = useState("");
  const [loadError, setLoadError] = useState("");
  const accessScope = `${user.id}:${space?.id ?? ""}:${space?.role ?? ""}:${space?.epoch ?? 0}`;
  const [loadedAccessScope, setLoadedAccessScope] = useState(accessScope);
  const visibleAccess = loadedAccessScope === accessScope ? access : [];
  const accessScopeRef = useRef(accessScope);
  accessScopeRef.current = accessScope;
  const action = useAction();
  const projectOwner = user.role === "owner";
  const canManage = !!space && canManageSpace(space, user);
  const fixed = space?.kind === "basis" || space?.kind === "results";
  const load = async () => {
    const [people, grants] = await Promise.all([
      projectOwner || spaces.some(item => canManageSpace(item, user)) ? api("/members") : Promise.resolve([]),
      canManage ? api(`/spaces/${idPath(space!.id)}/members`) : Promise.resolve([]),
    ]);
    return { people: listOf<Member>(people, "members").filter(activeMember), grants: listOf<Member>(grants, "members") };
  };
  const refresh = async () => { const value = await load(); if (accessScopeRef.current !== accessScope) return; setMembers(value.people); setAccess(value.grants); setLoadedAccessScope(accessScope); setLoadError(""); };
  useEffect(() => {
    let alive = true; setPeopleLoading(true); setPeopleError(""); setMembers([]); setInviteToken("");
    if (projectOwner || spaces.some(item => canManageSpace(item, user))) void api("/members").then(value => { if (alive) setMembers(listOf<Member>(value, "members").filter(activeMember)); }).catch(e => { if (alive) setPeopleError(errorText(e)); }).finally(() => { if (alive) setPeopleLoading(false); });
    else setPeopleLoading(false);
    return () => { alive = false; };
  }, [user.id, projectOwner, spaces.some(item => canManageSpace(item, user))]);
  useEffect(() => {
    let alive = true; setLoading(true); setLoadedAccessScope(accessScope); setLoadError(""); setAccess([]); setChosen(""); action.setError(""); action.setNotice("");
    const request = canManage && space ? api(`/spaces/${idPath(space.id)}/members`) : Promise.resolve([]);
    request.then(value => { if (alive) setAccess(listOf<Member>(value, "members")); }).catch(e => { if (alive) setLoadError(errorText(e)); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [space?.id, space?.role, space?.owner_id, space?.kind, space?.epoch, user.id, canManage]);
  const candidates = members.filter(member => memberId(member) !== space?.owner_id && !visibleAccess.some(grant => memberId(grant) === memberId(member)));
  const grant = (id: string, value: string) => void action.run(async () => {
    if (!canManage || id === space?.owner_id || !members.some(member => memberId(member) === id)) throw new Error("没有管理此授权的权限。");
    await api(`/spaces/${idPath(space!.id)}/members/${idPath(id)}`, "PUT", { role: value });
    setChosen(""); await refresh(); await onChanged();
  }, "知识库权限已更新，旧会话需要重新校验。");
  return <section className="rw-members-page" aria-busy={action.busy}>
    <PanelHeading title="成员与权限" subtitle="项目负责人和空间所有者是不同角色；加入项目不代表可见所有个人或协作知识。">
      {canManage && <button disabled={action.busy || loading} onClick={() => void action.run(refresh)}>刷新管理列表</button>}
    </PanelHeading>
    <Feedback error={action.error || peopleError || loadError} notice={action.notice} />
    <div className="rw-members-grid">
      <section className="rw-card">
        {projectOwner && <>
          <h3>项目成员</h3>
          {peopleLoading ? <p role="status">加载中…</p> : members.length ? <ul className="rw-people">{members.map(member => <li key={memberId(member)}>
            <span className="rw-person-avatar">{memberName(member).slice(0, 1).toUpperCase()}</span>
            <div><strong>{memberName(member)}</strong><small>{roles[member.role ?? member.user?.role ?? "member"]}{memberId(member) === user.id ? " · 你" : ""}</small></div>
            {memberId(member) !== user.id && (member.role ?? member.user?.role) !== "owner" && <button className="rw-danger rw-disable-member" disabled={action.busy} onClick={() => {
              if (!window.confirm(`确认停用成员 ${memberName(member)}？\n该成员将无法继续登录，既有会话与知识库授权由服务端撤销。你仍不能读取该成员的个人知识库。`)) return;
              void action.run(async () => { await api(`/members/${idPath(memberId(member))}`, "DELETE"); await refresh(); await onChanged(); }, "成员已停用。");
            }}>停用</button>}
          </li>)}</ul> : <Empty>暂无成员。</Empty>}
          <div className="rw-divider" /><h4>邀请项目成员</h4>
          <p className="rw-small rw-muted">令牌仅在下方展示，请手动复制发送给受邀者，不会自动发送邮件。</p>
          <form className="rw-form" onSubmit={e => { e.preventDefault(); void action.run(async () => {
            setInviteToken(""); const result = await api<{ token?: string; invite_token?: string }>("/invites", "POST", { email: email.trim() });
            const token = result.token ?? result.invite_token; if (!token) throw new Error("服务端未返回邀请令牌。"); setInviteToken(token); setEmail("");
          }, "邀请已创建，请复制令牌给对应成员。"); }}>
            <label>受邀邮箱<input type="email" required disabled={action.busy} value={email} onChange={e => setEmail(e.target.value)} /></label>
            <button disabled={action.busy || !email.trim()}>生成邀请令牌</button>
          </form>
          {inviteToken && <div className="rw-token"><label>邀请令牌<input readOnly value={inviteToken} onFocus={e => e.target.select()} /></label><button disabled={action.busy} onClick={() => void action.run(async () => {
            if (!navigator.clipboard) throw new Error("当前浏览器不支持自动复制，请选中令牌手动复制。"); await navigator.clipboard.writeText(inviteToken);
          }, "邀请令牌已复制。")}>复制令牌</button></div>}
          <div className="rw-divider" />
        </>}
        <h3>接受已有邀请</h3>
        <p className="rw-small rw-muted">接受项目邀请不会自动获得其他成员个人或协作知识库的权限。</p>
        <form className="rw-form" onSubmit={e => { e.preventDefault(); void action.run(async () => {
          await api("/invites/accept", "POST", { token: acceptToken.trim() }); setAcceptToken(""); await refresh(); await onChanged();
        }, "邀请已接受。"); }}>
          <label>邀请令牌<input value={acceptToken} required disabled={action.busy} onChange={e => setAcceptToken(e.target.value)} autoComplete="off" /></label>
          <button disabled={action.busy || !acceptToken.trim()}>接受邀请</button>
        </form>
      </section>
      <section className="rw-card">
        <h3>知识库授权</h3><label>选择授权知识库<select aria-label="授权知识库" disabled={action.busy} value={space?.id ?? ""} onChange={e => onSelectSpace?.(e.target.value)}><option value="">选择知识库</option>{spaces.map(item => <option key={item.id} value={item.id}>{spaceName(item)}</option>)}</select></label>
        {!space ? <Empty>暂无可授权的知识库。项目成员与邀请不依赖知识库；可先在知识页创建协作库。</Empty> : <><p className="rw-eyebrow">当前知识库</p><h3>{spaceName(space)}</h3>
        {canManage ? <>
          <p className="rw-small rw-muted">你是此知识库的空间所有者，可以为所有活跃项目成员（包括项目负责人）授予阅读或编辑权限。项目负责人没有默认访问特权。</p>
          <p className="rw-sharing-notice">授权后，对方可在授权范围内阅读或编辑知识。取消授权会阻断后续访问，并使旧会话失效、正在进行的输出重新校验；无法收回已经合法下载或复制的内容。</p>
          {loading ? <p role="status">正在加载授权…</p> : !loadError && <>
            <ul className="rw-access">
              <li><div><strong>{user.email} · 你</strong><small>空间所有者</small></div><span className="rw-badge">固定所有权</span></li>
              {visibleAccess.filter(member => memberId(member) !== space?.owner_id).map(member => { const id = memberId(member); return <li key={id}>
                <div><strong>{memberName(member)}</strong><small>{spaceRoles[member.role ?? "reader"] ?? member.role}</small></div>
                <div className="rw-actions">
                  <select aria-label={`${memberName(member)}的空间角色`} value={member.role ?? "reader"} disabled={action.busy} onChange={e => grant(id, e.target.value)}><option value="editor">可编辑</option><option value="reader">只读</option></select>
                  <button className="rw-danger" disabled={action.busy} onClick={() => {
                    if (!canManage || id === space?.owner_id || !window.confirm(`确认取消 ${memberName(member)} 对「${spaceName(space)}」的授权？\n后续访问将被阻断，旧会话失效，正在进行的输出需重新校验。已经合法下载或复制的内容无法收回。`)) return;
                    void action.run(async () => { await api(`/spaces/${idPath(space!.id)}/members/${idPath(id)}`, "DELETE"); await refresh(); await onChanged(); }, "授权已取消，旧会话失效；已复制的内容无法收回。");
                  }}>取消授权</button>
                </div>
              </li>; })}
            </ul>
            {!visibleAccess.some(member => memberId(member) !== space?.owner_id) && <p className="rw-small rw-muted">尚未共享，仅空间所有者可访问。</p>}
            <div className="rw-divider" /><h4>添加知识库授权</h4><SafetyLesson key={`${user.id}:${user.role}:authorization`} user={user} topic="authorization" />
            {candidates.length ? <form className="rw-form" onSubmit={e => { e.preventDefault(); if (chosen) grant(chosen, role); }}>
              <label>活跃项目成员<select value={chosen} onChange={e => setChosen(e.target.value)} required disabled={action.busy}><option value="">选择待授权成员</option>{candidates.map(member => <option key={memberId(member)} value={memberId(member)}>{memberName(member)}{(member.role ?? member.user?.role) === "owner" ? "（项目负责人，也需授权）" : ""}</option>)}</select></label>
              <label>访问角色<select value={role} onChange={e => setRole(e.target.value)} disabled={action.busy}><option value="reader">只读</option><option value="editor">可编辑</option></select></label>
              <button className="rw-primary" disabled={action.busy || !chosen}>添加授权</button>
            </form> : <p className="rw-small rw-muted">暂无待授权的活跃成员。</p>}
          </>}
        </> : <div className="rw-access-summary">
          <p>你的知识库角色：<span className="rw-badge">{spaceRoles[space.role]}</span></p>
          <p className="rw-small rw-muted">{fixed ? space.kind === "basis" ? "项目知识库由项目负责人维护，全体项目成员可读，不提供独立授权或撤权。" : "成果知识库仅通过审核流程发布，全体项目成员可读，不提供独立授权或撤权。" : "你可按当前授权阅读或编辑知识。授权成员管理仅向此知识库的空间所有者开放，项目负责人身份不自动获得管理权限。"}</p>
          {!fixed && <p className="rw-small rw-muted">取消授权会阻断后续访问，并使旧会话失效；已合法下载或复制的内容无法收回。</p>}
        </div>}</>}
      </section>
    </div>
  </section>;
}
