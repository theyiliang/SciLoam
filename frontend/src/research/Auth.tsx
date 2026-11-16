import { useState } from "react";
import { api, setCsrf } from "./api";
import { Feedback, useAction } from "./ui";
import { passwordError, type Session } from "./types";

export default function Auth({ onSession, initialError = "", initialNotice = "" }: { onSession: (session: Session) => void; initialError?: string; initialNotice?: string }) {
  const [register, setRegister] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmation, setShowConfirmation] = useState(false);
  const [invite, setInvite] = useState("");
  const action = useAction();
  return <div className="rw-auth">
    <section className="rw-auth-story">
      <div className="rw-wordmark">SciLoam <span>科研知识协同助手</span></div>
      <h1>让项目知识有迹可循，<br />让团队协同有章可序。</h1>
      <p>私人积累、可控共享、明确审核。让每一份知识的来源、版本与协作过程清晰可追溯。</p>
      <div className="rw-auth-note">个人知识独立可见 · 共享由所有者决定 · 审核不授予源库访问权</div>
    </section>
    <section className="rw-auth-card">
      <p className="rw-eyebrow">科研知识协同助手</p>
      <h2>{register ? "接受邀请，开始协作" : "欢迎回来"}</h2>
      <p className="rw-muted">{register ? "使用项目负责人提供的邀请令牌注册。" : "使用项目账号登录，不会自动创建账号。"}</p>
      <Feedback error={action.error || initialError} notice={action.notice || initialNotice} />
      <form onSubmit={event => { event.preventDefault(); void action.run(async () => {
        if (register) { const error = passwordError(password, confirmation); if (error) throw new Error(error); }
        const session = await api<Session>(register ? "/auth/register" : "/auth/login", "POST", { email: email.trim(), password, ...(register ? { invite_token: invite.trim() } : {}) });
        if (!session.user) throw new Error("登录响应中缺少用户信息。");
        setCsrf(session.csrf_token); onSession(session);
      }); }}>
        <label htmlFor="auth-email">邮箱</label>
        <input id="auth-email" type="email" autoComplete="username" required disabled={action.busy} value={email} onChange={e => setEmail(e.target.value)} />
        <label htmlFor="auth-password">密码</label>
        <div className="rw-password-row"><input id="auth-password" type={showPassword ? "text" : "password"} autoComplete={register ? "new-password" : "current-password"} required minLength={register ? 12 : undefined} disabled={action.busy} value={password} onChange={e => setPassword(e.target.value)} /><button type="button" aria-label={showPassword ? "隐藏密码" : "显示密码"} aria-pressed={showPassword} aria-controls="auth-password" onClick={() => setShowPassword(!showPassword)}>{showPassword ? "隐藏" : "显示"}</button></div>
        {register && <>
          <label htmlFor="auth-confirmation">确认密码</label>
          <div className="rw-password-row"><input id="auth-confirmation" type={showConfirmation ? "text" : "password"} autoComplete="new-password" required minLength={12} disabled={action.busy} value={confirmation} onChange={e => setConfirmation(e.target.value)} /><button type="button" aria-label={showConfirmation ? "隐藏确认密码" : "显示确认密码"} aria-pressed={showConfirmation} aria-controls="auth-confirmation" onClick={() => setShowConfirmation(!showConfirmation)}>{showConfirmation ? "隐藏" : "显示"}</button></div>
          <p className="rw-small rw-muted">密码至少12个字符。确认密码仅用于本页校验。</p>
          <label htmlFor="auth-invite">邀请令牌</label><input id="auth-invite" required disabled={action.busy} value={invite} onChange={e => setInvite(e.target.value)} autoComplete="off" placeholder="粘贴项目负责人发来的邀请令牌" />
        </>}
        <button className="rw-primary rw-full" disabled={action.busy}>{action.busy ? "正在验证…" : register ? "注册并加入项目" : "登录 SciLoam"}</button>
      </form>
      <button className="rw-link-button" disabled={action.busy} onClick={() => { setRegister(!register); setPassword(""); setConfirmation(""); setShowPassword(false); setShowConfirmation(false); action.setError(""); }}>{register ? "已有账号？返回登录" : "收到邀请？受邀注册"}</button>
      <details className="rw-auth-recovery"><summary>忘记密码？</summary><p className="rw-small rw-muted">请联系部署管理员核验身份后恢复密码。尚未接入邮件服务，不会发送密码重置邮件。</p></details>
      <p className="rw-small rw-muted">会话通过 HttpOnly Cookie 管理。所有写入均由服务端校验身份与权限。</p>
    </section>
  </div>;
}
