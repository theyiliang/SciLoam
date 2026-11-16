import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import ts from "typescript";
const require = createRequire(import.meta.url), cache = new Map();
function load(name) {
  if (cache.has(name)) return cache.get(name).exports;
  const module = { exports: {} }; cache.set(name, module);
  const output = ts.transpileModule(readFileSync(new URL(name, import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  new Function("require", "module", "exports", output)(value => value === "./api" ? { idPath: encodeURIComponent } : value === "./stream" ? load("stream.ts") : require(value), module, module.exports);
  return module.exports;
}
const { generationFailure, parseGoalGenerationEvent, watchGoalGeneration } = load("goalDraftRun.ts");

const identity = { run_id: "goal/r", request_id: "request", version_ids: ["v1"] };
const event = (seq, type, data = {}) => ({ ...identity, seq, timestamp: "2026-10-26T12:00:00Z", type, data });
const response = events => new Response(new ReadableStream({ start(controller) { for (const value of events) controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(value)}\n\n`)); controller.close(); } }), { headers: { "Content-Type": "text/event-stream" } });

test("goal events strictly bind request and selected sources, not chat scope", () => {
  assert.equal(parseGoalGenerationEvent(event(1, "generation.started", { method: "rule" }), identity).type, "generation.started");
  for (const patch of [{ run_id: "other" }, { request_id: "other" }, { version_ids: ["private"] }, { version_ids: undefined }]) assert.throws(() => parseGoalGenerationEvent({ ...event(1, "goal.started"), ...patch }, identity), /不一致/);
  assert.equal(parseGoalGenerationEvent(event(1, "answer.delta"), identity), null);
  assert.throws(() => parseGoalGenerationEvent(event(0, "goal.started"), identity), /序号/);
});

test("actual goal watcher resumes GET cursor, deduplicates and reads small completion only", async () => {
  const original = globalThis.fetch, calls = [], seen = [], reconnects = [];
  globalThis.fetch = async (url, options) => { calls.push([url, options]); return calls.length === 1 ? response([event(1, "goal.started"), event(2, "generation.started", { method: "ai" })]) : response([event(2, "generation.started"), event(3, "generation.completed"), event(4, "goal.completed", { draft_id: "draft" })]); };
  try {
    const result = await watchGoalGeneration(identity, new AbortController().signal, value => seen.push(value), attempt => reconnects.push(attempt));
    assert.deepEqual(seen.map(value => value.seq), [1, 2, 3, 4]); assert.deepEqual(reconnects, [1]); assert.equal(result.data.draft_id, "draft");
    assert.equal(calls[0][0], "/api/goal-draft-runs/goal%2Fr/events?after_seq=0"); assert.match(calls[1][0], /after_seq=2$/);
    assert.ok(calls.every(([, options]) => options.credentials === "include" && !options.body && (!options.method || options.method === "GET")));
  } finally { globalThis.fetch = original; }
});

test("goal watcher allows terminal purge sequence gap but refuses ordinary missing stages", async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => response([event(1, "goal.started"), event(9, "goal.cancelled", { reason: "cancelled" })]);
    assert.equal((await watchGoalGeneration(identity, new AbortController().signal, () => {})).type, "goal.cancelled");
    globalThis.fetch = async () => response([event(3, "generation.started")]);
    await assert.rejects(watchGoalGeneration(identity, new AbortController().signal, () => {}), /缺口/);
  } finally { globalThis.fetch = original; }
});

test("goal watcher does not retry forbidden scope or expired replay, cancellation aborts", async () => {
  const original = globalThis.fetch; let calls = 0;
  try {
    globalThis.fetch = async () => { calls++; return new Response("", { status: 403 }); };
    await assert.rejects(watchGoalGeneration(identity, new AbortController().signal, () => {}), /权限/); assert.equal(calls, 1);
    globalThis.fetch = async () => new Response("", { status: 409 });
    await assert.rejects(watchGoalGeneration(identity, new AbortController().signal, () => {}), /过期/);
    const abort = new AbortController(); abort.abort();
    await assert.rejects(watchGoalGeneration(identity, abort.signal, () => {}));
    assert.match(generationFailure("no_extractable_goals"), /未识别/); assert.match(generationFailure("invalid_evidence"), /依据/);
  } finally { globalThis.fetch = original; }
});
