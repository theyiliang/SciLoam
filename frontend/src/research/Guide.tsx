import { useEffect, useRef, useState } from "react";
import type { User } from "./types";

export type GuideDestination = "basis" | "private" | "goals" | "members" | "reviews";
export const guideKey = (user: User) => `sciloam-guide-v1:${user.id}:${user.role}`;
export function guideDismissed(user: User): boolean {
  try { return localStorage.getItem(guideKey(user)) === "dismissed"; } catch { return false; }
}
export function dismissGuide(user: User): void {
  try { localStorage.setItem(guideKey(user), "dismissed"); } catch { /* A blocked browser store must not block the workspace. */ }
}
export const guideSteps = (role: User["role"]): { title: string; description: string; destination: GuideDestination; action: string }[] => role === "owner" ? [
  { title: "整理项目知识", description: "在项目知识库录入研究依据，保留原文与具体版本。", destination: "basis", action: "打开项目知识" },
  { title: "邀请项目成员", description: "邀请成员加入项目；加入项目不代表获授权访问个人或协作知识库。", destination: "members", action: "打开成员与权限" },
  { title: "审核研究成果", description: "核验成员提交的指定版本，通过后该成果副本对全体项目成员可读。", destination: "reviews", action: "打开审核管理" },
] : [
  { title: "整理个人知识", description: "先将笔记保存在个人知识库，不会自动共享给项目成员。", destination: "private", action: "打开个人知识" },
  { title: "按需授权协作", description: "在协作知识库明确授权成员；分享独立副本不会开放整个源知识库。", destination: "members", action: "打开成员与权限" },
  { title: "关联研究目标", description: "选择目标，关联有权读取的知识版本并自主记录进展；关联不会改变源权限。", destination: "goals", action: "打开任务管理" },
  { title: "提交与查看反馈", description: "在知识库中提交指定版本审核，再在我的提交查看反馈。审核通过不代表目标自动完成。", destination: "reviews", action: "打开我的提交" },
];

export type LessonTopic = "authorization" | "review" | "deletion";
export const lessonKey = (user: User, topic: LessonTopic) => `sciloam-lesson-v1:${user.id}:${user.role}:${topic}`;
export function lessonDismissed(user: User, topic: LessonTopic): boolean {
  try { return localStorage.getItem(lessonKey(user, topic)) === "dismissed"; } catch { return false; }
}
const lessons: Record<LessonTopic, { title: string; content: string }> = {
  authorization: { title: "授权前先了解可见范围", content: "授权只作用于当前知识库。只读成员可读取、检索和对话，可编辑成员还可写入知识；项目负责人也需要明确授权。撤权阻断后续访问，但不能收回已合法复制的内容。" },
  review: { title: "送审的是指定版本", content: "负责人只看到这次提交的独立版本，不会因此进入整个源库。审核通过后该版本面向全体项目成员；关联目标不等于完成目标，源知识撤回也不会自动收回审核副本。" },
  deletion: { title: "删除、撤回与独立副本", content: "删除或撤回会影响后续原文访问和检索，但不级联删除已分享、送审或发布的独立副本。当前版本须明确指定保留版本，受保护版本保留历史。请在下方危险操作确认中核对实际影响。" },
};
/** Browser stores only dismissal flags, never document content or research data. */
export function SafetyLesson({ user, topic, popup = false }: { user: User; topic: LessonTopic; popup?: boolean }) {
  const key = lessonKey(user, topic);
  const [state, setState] = useState(() => ({ key, open: popup && !lessonDismissed(user, topic) }));
  const [remember, setRemember] = useState(false);
  useEffect(() => { setState({ key, open: popup && !lessonDismissed(user, topic) }); setRemember(false); }, [key, popup]);
  if (state.key !== key) return null;
  const lesson = lessons[topic];
  if (!state.open) return popup ? null : <aside className="rw-import-lesson" aria-label={lesson.title}><button type="button" onClick={() => setState({ key, open: true })}>重看{topic === "authorization" ? "授权" : topic === "review" ? "送审" : "删除"}教学说明</button></aside>;
  const close = (skipNextTime: boolean) => { if (skipNextTime || !popup) { try { localStorage.setItem(key, "dismissed"); } catch { /* Teaching remains usable with blocked storage. */ } } setState({ key, open: false }); };
  return <aside className="rw-import-lesson" aria-label={lesson.title}><div className="rw-spread"><h4>{lesson.title}</h4><button type="button" onClick={() => close(popup ? remember : true)}>关闭教学说明</button></div><p>{lesson.content}</p>{popup && <label className="rw-inline"><input type="checkbox" checked={remember} onChange={e => setRemember(e.target.checked)} />下次不再弹出此说明</label>}<small>{popup ? "关闭说明不会确认、跳过或执行任何操作。" : "可关闭并随时重看；关闭说明不会确认、跳过或执行任何操作。"}</small></aside>;
}

export default function Guide({ user, reopen, onNavigate }: { user: User; reopen: number; onNavigate: (destination: GuideDestination) => void }) {
  const key = guideKey(user);
  const [state, setState] = useState(() => ({ key, open: false }));
  const previousReopen = useRef(reopen);
  useEffect(() => { setState({ key, open: false }); }, [key]);
  const close = () => { dismissGuide(user); setState({ key, open: false }); };
  useEffect(() => {
    if (!state.open) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [state.open, key]);
  useEffect(() => { if (previousReopen.current !== reopen) { previousReopen.current = reopen; setState({ key, open: true }); } }, [reopen, key]);
  if (state.key !== key || !state.open) return null;
  return <div className="rw-guide-overlay"><button className="rw-guide-backdrop" aria-label="关闭使用指南" onClick={close} /><section className="rw-import-guide" role="dialog" aria-modal="true" aria-labelledby="guide-title"><div className="rw-spread"><div><h2 id="guide-title">{user.role === "owner" ? "负责人使用指南" : "成员使用指南"}</h2><p>按你的研究流程选择下一步；指南不改变权限，也不执行研究操作。</p></div><button autoFocus className="rw-guide-close" aria-label="关闭指南" title="关闭指南" onClick={close}>×</button></div><ol>{guideSteps(user.role).map((step, index) => <li key={step.title}><span className="rw-import-step">{index + 1}</span><h3>{step.title}</h3><p>{step.description}</p><button onClick={() => { onNavigate(step.destination); close(); }}>{step.action}</button></li>)}</ol></section></div>;
}
