import { api, idPath } from "./api";
import { sseDecoder, StreamError } from "./stream";
import type { GoalDraft } from "./types";

export type GoalGenerationIdentity = { run_id: string; request_id: string; version_ids: string[] };
export type GoalGenerationRun = GoalGenerationIdentity & { status: "queued" | "running" | "completed" | "failed" | "cancelled"; result?: { draft: GoalDraft }; reason?: string };
export type GoalGenerationEvent = { run_id: string; request_id: string; version_ids: string[]; seq: number; timestamp: string; type: string; data: Record<string, unknown> };
export const generationLabels: Record<string, string> = {
  "goal.started": "目标提取已开始",
  "sources.validation.started": "正在校验所选来源与权限",
  "sources.validation.completed": "来源校验通过",
  "sources.read.started": "正在读取选定知识版本",
  "sources.read.completed": "选定版本读取完成",
  "generation.started": "正在提取目标要求",
  "generation.completed": "目标候选提取完成",
  "evidence.validation.started": "正在核对原文依据与目标结构",
  "evidence.validation.completed": "原文依据校验完成",
  "draft.save.started": "正在保存目标草稿",
  "draft.save.completed": "目标草稿已保存",
  "goal.completed": "提取完成，请核对草稿；尚未发布",
  "goal.failed": "本次提取未完成",
  "goal.cancelled": "本次提取已停止",
};
const terminal = new Set(["goal.completed", "goal.failed", "goal.cancelled"]);
export const generationFailure = (reason: unknown): string => ({
  no_extractable_goals: "未识别到可提取的目标。请补充标题、编号或明确的目标/交付物标签，本地规则不做语义推断，请人工整理目标。",
  invalid_evidence: "生成结果未通过原文依据校验，未发布任何目标。请检查来源并重新提取。",
  invalid_proposals: "生成候选不符合结构或原文依据要求，未修改正式目标树。",
  source_changed: "来源版本或目标基线已变化，请重新选择版本并核对。",
  access_changed: "登录或来源权限已变化，已停止提取并清除结果。",
  model_unavailable: "模型服务不可用或返回无效结果，请稍后重试；正式目标树未改变。",
  execution_deadline: "目标提取超时。请检查已有草稿后再决定是否重新生成。",
  consumer_timeout: "连接超出恢复时限，提取已停止。请先检查已有草稿。",
  cancelled: "已停止本次提取，正式目标树未改变。",
  run_unavailable: "目标提取失败，请检查已有草稿和来源后重试。",
} as Record<string, string>)[typeof reason === "string" ? reason : ""] ?? "目标提取未完成，请检查来源和已有草稿；不会自动重新生成。";
export const startGoalGeneration = (body: { version_ids: string[]; request_id: string; regenerates_id?: string; expected_revision?: number }) => api<GoalGenerationRun>("/goal-draft-runs", "POST", body);
export const cancelGoalGeneration = (id: string) => api<GoalGenerationRun>(`/goal-draft-runs/${idPath(id)}/cancel`, "POST", {});
export const getGoalGeneration = (id: string) => api<GoalGenerationRun>(`/goal-draft-runs/${idPath(id)}`);

export function parseGoalGenerationEvent(value: unknown, identity: GoalGenerationIdentity): GoalGenerationEvent | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new StreamError("目标提取事件格式错误。");
  const event = value as Record<string, unknown>;
  if (event.run_id !== identity.run_id || event.request_id !== identity.request_id || !Array.isArray(event.version_ids)
      || JSON.stringify(event.version_ids) !== JSON.stringify(identity.version_ids)) throw new StreamError("目标提取事件来源不一致，已停止显示。", 403);
  if (!Number.isSafeInteger(event.seq) || Number(event.seq) < 1 || typeof event.timestamp !== "string" || !Number.isFinite(Date.parse(event.timestamp)) || typeof event.type !== "string") throw new StreamError("目标提取事件序号或时间无效。");
  if (!(event.type in generationLabels)) return null;
  if (!event.data || typeof event.data !== "object" || Array.isArray(event.data)) throw new StreamError("目标提取事件内容无效。");
  return { ...identity, seq: Number(event.seq), timestamp: event.timestamp, type: event.type, data: event.data as Record<string, unknown> };
}

/** Resume this run only; never repeat a generation POST after a disconnect. */
export async function watchGoalGeneration(identity: GoalGenerationIdentity, signal: AbortSignal, onEvent: (event: GoalGenerationEvent) => void, onReconnect?: (attempt: number) => void): Promise<GoalGenerationEvent> {
  let cursor = 0;
  for (let attempt = 0; attempt <= 2; attempt++) {
    signal.throwIfAborted();
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined, final: GoalGenerationEvent | undefined;
    try {
      const response = await fetch(`/api/goal-draft-runs/${idPath(identity.run_id)}/events?after_seq=${cursor}`, { credentials: "include", headers: { Accept: "text/event-stream" }, signal });
      if (!response.ok) throw new StreamError([401, 403].includes(response.status) ? "登录或来源权限已失效。" : [404, 409, 410].includes(response.status) ? "目标提取事件已过期，请检查已有草稿。" : `目标提取连接失败（${response.status}）。`, response.status, response.status >= 500);
      if (!response.headers.get("Content-Type")?.toLowerCase().startsWith("text/event-stream") || !response.body) throw new StreamError("服务未返回目标提取事件流。");
      reader = response.body.getReader();
      const parser = sseDecoder(value => {
        if (signal.aborted || final) return;
        const event = parseGoalGenerationEvent(value, identity);
        if (!event || event.seq <= cursor) return;
        if (event.seq !== cursor + 1 && !terminal.has(event.type)) throw new StreamError("目标提取事件存在缺口，请检查保存结果。");
        cursor = event.seq; onEvent(event);
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
      throw new StreamError("目标提取连接中断，正在恢复同一次运行。", 0, true);
    } catch (error) {
      if (signal.aborted) throw error;
      if (error instanceof StreamError ? !error.retryable : !(error instanceof TypeError)) throw error;
      if (attempt === 2) throw error;
      onReconnect?.(attempt + 1);
    } finally {
      if (reader) { try { await reader.cancel(); } catch { /* Connection is already closed. */ } reader.releaseLock(); }
    }
  }
  throw new StreamError("目标提取连接不可用，请检查已有草稿。");
}
