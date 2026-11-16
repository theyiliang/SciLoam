import { useState } from "react";
import { api } from "./api";
import { passwordError, roles, type PasswordChange, type User } from "./types";
import { Feedback, PanelHeading, useAction } from "./ui";

export default function Account({ user, onPasswordChanged }: { user: User; onPasswordChanged: () => Promise<void> }) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [visible, setVisible] = useState({ current: false, next: false, confirm: false });
  const action = useAction();
  return <section className="rw-account">
    <PanelHeading title="账号与密码" subtitle="仅管理你自己的登录信息。" />
    <section className="rw-card">
      <h3>当前账号</h3><p>{user.email} <span className="rw-badge">{roles[user.role]}</span></p>
      <div className="rw-divider" />
      <h3>修改密码</h3>
      <Feedback error={action.error} notice={action.notice} />
      <form className="rw-form rw-password-form" onSubmit={event => { event.preventDefault(); void action.run(async () => {
        if (!currentPassword) throw new Error("请输入当前密码。");
        const error = passwordError(newPassword, confirmation); if (error) throw new Error(error);
        const result = await api<PasswordChange>("/auth/password", "POST", { current_password: currentPassword, new_password: newPassword });
        if (result.password_changed !== true || result.reauthenticate !== true) throw new Error("服务端未确认密码变更，请重新验证登录状态。");
        setCurrentPassword(""); setNewPassword(""); setConfirmation(""); setVisible({ current: false, next: false, confirm: false });
        await onPasswordChanged();
      }); }}>
        <label htmlFor="account-current-password">当前密码</label>
        <div className="rw-password-row"><input id="account-current-password" type={visible.current ? "text" : "password"} autoComplete="current-password" required disabled={action.busy} value={currentPassword} onChange={e => setCurrentPassword(e.target.value)} /><button type="button" aria-label={visible.current ? "隐藏当前密码" : "显示当前密码"} aria-pressed={visible.current} aria-controls="account-current-password" onClick={() => setVisible({ ...visible, current: !visible.current })}>{visible.current ? "隐藏" : "显示"}</button></div>
        <label htmlFor="account-new-password">新密码</label>
        <div className="rw-password-row"><input id="account-new-password" type={visible.next ? "text" : "password"} autoComplete="new-password" required minLength={12} disabled={action.busy} value={newPassword} onChange={e => setNewPassword(e.target.value)} /><button type="button" aria-label={visible.next ? "隐藏新密码" : "显示新密码"} aria-pressed={visible.next} aria-controls="account-new-password" onClick={() => setVisible({ ...visible, next: !visible.next })}>{visible.next ? "隐藏" : "显示"}</button></div>
        <label htmlFor="account-confirmation">确认新密码</label>
        <div className="rw-password-row"><input id="account-confirmation" type={visible.confirm ? "text" : "password"} autoComplete="new-password" required minLength={12} disabled={action.busy} value={confirmation} onChange={e => setConfirmation(e.target.value)} /><button type="button" aria-label={visible.confirm ? "隐藏确认新密码" : "显示确认新密码"} aria-pressed={visible.confirm} aria-controls="account-confirmation" onClick={() => setVisible({ ...visible, confirm: !visible.confirm })}>{visible.confirm ? "隐藏" : "显示"}</button></div>
        <p className="rw-small rw-muted">新密码至少12个字符。修改成功后将撤销旧登录会话，需要重新登录；确认新密码仅用于本页校验，不发送到服务端。</p>
        <button className="rw-primary" disabled={action.busy}>{action.busy ? "正在修改…" : "确认修改密码"}</button>
      </form>
      <div className="rw-divider" /><h3>忘记密码？</h3><p className="rw-small rw-muted">联系部署管理员核验身份后恢复。尚未接入邮件服务，不提供邮件重置入口。</p>
    </section>
  </section>;
}
