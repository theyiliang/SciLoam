let csrf = "";
export function setCsrf(value?: string) { csrf = value ?? ""; }
export class ApiError extends Error {
  status: number;
  payload?: unknown;
  constructor(status: number, message: string, payload?: unknown) { super(message); this.status = status; this.payload = payload; }
}
function detail(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(detail).join("；");
  if (value && typeof value === "object") {
    const obj = value as Record<string, unknown>;
    return detail(obj.message ?? obj.msg ?? obj.detail ?? obj.error ?? "请求失败");
  }
  return "请求失败";
}
export type ApiOptions = { idempotencyKey?: string };
const idempotentPostPaths = [
  /^\/spaces$/,
  /^\/spaces\/[^/]+\/documents$/,
  /^\/documents\/[^/]+\/versions$/,
  /^\/versions\/[^/]+\/share$/,
  /^\/reviews$/,
  /^\/reviews\/[^/]+\/decision$/,
  /^\/tasks$/,
  /^\/tasks\/[^/]+\/progress$/,
  /^\/conversations$/,
  /^\/goals$/,
  /^\/goals\/[^/]+\/updates$/,
  /^\/goals\/[^/]+\/knowledge$/,
];
export async function api<T>(path: string, method = "GET", body?: unknown, options?: ApiOptions): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  // Each call is a new operation. Explicit retries may reuse a key only with
  // the same path/body; the server enforces replay consistency. Never auto-retry.
  if (method === "POST" && idempotentPostPaths.some(pattern => pattern.test(path.split("?")[0]))) {
    headers["Idempotency-Key"] = options?.idempotencyKey ?? crypto.randomUUID();
  }
  if (method !== "GET") {
    const cookie = typeof document === "undefined" ? undefined : document.cookie.split(";").map(part => part.trim()).find(part => part.startsWith("research_csrf="));
    headers["X-CSRF-Token"] = cookie ? decodeURIComponent(cookie.slice("research_csrf=".length)) : csrf;
  }
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(`/api${path}`, { method, credentials: "include", headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await response.text();
  let payload: unknown;
  try { payload = text ? JSON.parse(text) : undefined; } catch { throw new ApiError(response.status, "服务器未返回 JSON，请检查 API 服务与同源代理配置。"); }
  if (!response.ok) throw new ApiError(response.status, `${detail(payload)}（${response.status}）`, payload);
  return payload as T;
}
// Raw binary import intentionally bypasses the JSON helper: never stringify file bytes.
export async function importKnowledge<T>(spaceId: string, file: File, metadata: { title?: string; source?: string } = {}): Promise<T> {
  if (!/\.(txt|md|docx)$/i.test(file.name)) throw new Error("仅支持 TXT / MD / DOCX；PDF 暂不可用，请先转换为 TXT 或 MD。");
  if (file.size > 8 * 1024 * 1024) throw new Error("导入文件请控制在 8 MB 内。");
  const cookie = typeof document === "undefined" ? undefined : document.cookie.split(";").map(part => part.trim()).find(part => part.startsWith("research_csrf="));
  const headers: Record<string, string> = { Accept: "application/json", "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(file.name), "X-CSRF-Token": cookie ? decodeURIComponent(cookie.slice("research_csrf=".length)) : csrf };
  if (metadata.title?.trim()) headers["X-Knowledge-Title"] = encodeURIComponent(metadata.title.trim());
  if (metadata.source?.trim()) headers["X-Source"] = encodeURIComponent(metadata.source.trim());
  const response = await fetch(`/api/spaces/${encodeURIComponent(spaceId)}/imports`, { method: "POST", credentials: "include", headers, body: file });
  const text = await response.text();
  let payload: unknown;
  try { payload = text ? JSON.parse(text) : undefined; } catch { throw new ApiError(response.status, "导入服务未返回 JSON，请检查 API 服务与同源代理配置；不要自动重复导入。"); }
  if (!response.ok) throw new ApiError(response.status, `${detail(payload)}（${response.status}）`, payload);
  return payload as T;
}
export function listOf<T>(payload: unknown, key: string): T[] {
  if (Array.isArray(payload)) return payload as T[];
  if (payload && typeof payload === "object") {
    const obj = payload as Record<string, unknown>;
    const value = obj[key] ?? obj.items;
    if (Array.isArray(value)) return value as T[];
  }
  throw new Error("服务器列表格式不符合接口约定，请刷新后重试。");
}
export const idPath = (id: string) => encodeURIComponent(id);
export const errorText = (error: unknown) => error instanceof Error ? error.message : "请求失败，请稍后重试。";

/** Fire-and-forget user interaction tracker. Never throws or blocks the UI.
 *  Payloads are metadata-only (IDs, enums, timestamps), never prose. */
export function trackEvent(event_type: "user.answer_feedback" | "user.session_action", action_type: string, meta: Record<string, unknown> = {}): void {
  if (typeof window === "undefined") return;
  const cookie = typeof document === "undefined" ? undefined : document.cookie.split(";").map(part => part.trim()).find(part => part.startsWith("research_csrf="));
  const csrfHeader = cookie ? decodeURIComponent(cookie.slice("research_csrf=".length)) : csrf;
  fetch("/api/events", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfHeader },
    body: JSON.stringify({ event_type, action_type, ...meta, payload: meta.payload ?? {} }),
  }).catch(() => { /* swallow — tracking must never break the app */ });
}
