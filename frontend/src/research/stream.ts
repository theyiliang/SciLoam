import { api, idPath } from "./api";
import type { ChatRun, ChatRunEvent, ChatRunIdentity, ChatRunInput } from "./types";

export const MAX_SSE_FRAME = 128 * 1024;
export const MAX_TENTATIVE_TEXT = 1024 * 1024;
const eventTypes = new Set(["document.read", "run.started", "retrieval.started", "retrieval.completed", "retrieval.skipped", "retrieval.degraded", "rerank.started", "rerank.completed", "rerank.skipped", "rerank.degraded", "agent.started", "agent.completed", "answer.delta", "answer.final", "run.failed", "run.cancelled", "run.completed"]);
const terminal = new Set(["answer.final", "run.failed", "run.cancelled", "run.completed"]);
export class StreamError extends Error {
  constructor(message: string, public status = 0, public retryable = false) { super(message); }
}
export const accessLost = (error: unknown) => !!error && typeof error === "object" && "status" in error && (error.status === 401 || error.status === 403);
export const replayExpired = (error: unknown) => error instanceof StreamError && [404, 409, 410].includes(error.status);
export const startChatRun = (body: ChatRunInput) => api<ChatRun>("/chat/runs", "POST", body);
export const cancelChatRun = (id: string) => api<ChatRun>(`/chat/runs/${idPath(id)}/cancel`, "POST", {});

/** Streaming decoder: never synthesize deltas from a completed answer. */
export function sseDecoder(onData: (data: unknown) => void) {
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let line = "", data: string[] = [], frameSize = 0, cr = false;
  const finishLine = () => {
    if (!line) {
      if (data.length) {
        let value: unknown;
        try { value = JSON.parse(data.join("\n")); } catch { throw new StreamError("实时事件 JSON 格式错误，请刷新历史。"); }
        onData(value);
      }
      data = []; frameSize = 0;
    } else if (line === "data" || line.startsWith("data:")) data.push(line === "data" ? "" : line.slice(5).replace(/^ /, ""));
    line = "";
  };
  return {
    push(bytes: Uint8Array, end = false) {
      const text = decoder.decode(bytes, { stream: !end });
      for (const char of text) {
        if (cr) { cr = false; if (char === "\n") continue; }
        frameSize += char.length;
        if (frameSize > MAX_SSE_FRAME) throw new StreamError("实时事件过大，已停止读取；请刷新历史。");
        if (char === "\r" || char === "\n") { finishLine(); cr = char === "\r"; }
        else line += char;
      }
      // An unterminated frame is deliberately discarded, not interpreted as final.
    },
  };
}
function parseEvent(value: unknown, identity: ChatRunIdentity & { run_id: string; message_id: string }): ChatRunEvent | null {
  if (!value || typeof value !== "object") throw new StreamError("实时事件格式错误。");
  const event = value as Record<string, unknown>;
  if (event.run_id !== identity.run_id || (event.space_id !== undefined && event.space_id !== identity.space_id) || (event.conversation_id !== undefined && event.conversation_id !== identity.conversation_id) || (event.request_id !== undefined && event.request_id !== identity.request_id)) throw new StreamError("实时事件作用域不一致，已清除显示。", 403);
  if (!Number.isSafeInteger(event.seq) || Number(event.seq) < 1 || typeof event.type !== "string" || typeof event.timestamp !== "string" || !Number.isFinite(Date.parse(event.timestamp))) throw new StreamError("实时事件序号或时间无效。");
  if (!eventTypes.has(event.type)) return null;
  const payload = event.data ?? event.payload;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new StreamError("实时事件内容无效。");
  const data = payload as Record<string, unknown>;
  if ((event.type === "answer.delta" || event.type === "answer.final") && data.message_id !== identity.message_id) throw new StreamError("实时回答消息编号不一致，已清除显示。", 403);
  if (event.type === "answer.delta" && (typeof data.text !== "string" || data.text.length > MAX_SSE_FRAME)) throw new StreamError("临时回答片段无效。");
  return { run_id: identity.run_id, seq: Number(event.seq), timestamp: event.timestamp, type: event.type as ChatRunEvent["type"], data };
}

/** Reconnect only this GET and cursor; never repeat POST /runs. */
export async function watchChatRun(identity: ChatRunIdentity & { run_id: string; message_id: string }, signal: AbortSignal, onEvent: (event: ChatRunEvent) => void, onReconnect?: (attempt: number) => void): Promise<ChatRunEvent> {
  let afterSeq = 0;
  for (let attempt = 0; attempt <= 2; attempt++) {
    signal.throwIfAborted();
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    let final: ChatRunEvent | undefined;
    try {
      const response = await fetch(`/api/chat/runs/${idPath(identity.run_id)}/events?after_seq=${afterSeq}`, { method: "GET", credentials: "include", headers: { Accept: "text/event-stream" }, signal });
      if (!response.ok) throw new StreamError(response.status === 401 || response.status === 403 ? "登录或访问权限已失效，请重新验证权限。" : [404, 409, 410].includes(response.status) ? "实时记录已过期，请刷新并检查对话历史。" : `实时连接失败（${response.status}）。`, response.status, response.status >= 500);
      if (!response.headers.get("Content-Type")?.toLowerCase().startsWith("text/event-stream") || !response.body) throw new StreamError("服务器未返回实时事件流，请检查服务配置。");
      reader = response.body.getReader();
      const parser = sseDecoder(value => {
        if (signal.aborted || final) return;
        const event = parseEvent(value, identity);
        if (!event || event.seq <= afterSeq) return;
        if (event.seq !== afterSeq + 1 && !terminal.has(event.type)) throw new StreamError("实时事件序列有缺口，请刷新历史核对完整回答。");
        afterSeq = event.seq;
        onEvent(event);
        if (terminal.has(event.type)) final = event;
      });
      while (!final) {
        signal.throwIfAborted();
        const chunk = await reader.read();
        signal.throwIfAborted();
        if (chunk.done) { parser.push(new Uint8Array(), true); break; }
        parser.push(chunk.value);
      }
      if (final) return final;
      throw new StreamError("实时连接中断；请检查已保存历史，不要重复发送。", 0, true);
    } catch (error) {
      if (signal.aborted) throw error;
      if (error instanceof StreamError ? !error.retryable : !(error instanceof TypeError)) throw error;
      if (attempt === 2) throw error;
      onReconnect?.(attempt + 1);
    } finally {
      if (reader) { try { await reader.cancel(); } catch { /* connection already closed */ } reader.releaseLock(); }
    }
  }
  throw new StreamError("实时连接不可用。");
}
