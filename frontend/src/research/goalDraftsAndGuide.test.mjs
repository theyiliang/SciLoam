import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import ts from "typescript";
import { importKnowledge, ApiError, setCsrf } from "./api.ts";
const directory = dirname(fileURLToPath(import.meta.url)), require = createRequire(import.meta.url);
const owner = { id: "owner", role: "owner", email: "o@example.org" }, member = { ...owner, id: "member", role: "member" };
const basis = { id: "basis/1", kind: "basis", name: "Basis", role: "reader", owner_id: owner.id };
const version = { id: "v1", number: 1, is_current: true }, doc = { id: "doc", title: "立项依据", active_version: version, versions: [version] };
const node = { id: "n1", parent_id: null, title: "实验目标", description: "原文说明", acceptance_criteria: "人工核验", due_date: null, position: 0, origin: "explicit", generation_method: "rule", deliverables: "研究报告", constraints: "遵守伦理", time_requirement: "六个月内完成", field_sources: { title: [{ version_id: "v1", quote: "实验目标", start: 10, end: 14 }] }, source: { version_id: "v1", quote: "实验目标", start: 10, end: 14 } };
const draft = { id: "draft/1", created_by: owner.id, status: "draft", revision: 2, method: "extractive", generation: { method: "rule" }, publication_readiness: { publishable: true, reasons: [] }, suggestions: [], sources: [{ version_id: "v1", document_id: "doc", space_id: basis.id, number: 1 }], sources_hash: "sourcehash", base_tree_hash: "treehash", nodes: [node], limitations: ["仅提取原文，不推断验收标准。"] };
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; };
function harness(filename, request = async () => []) {
  let states = [], refs = [], effects = [], si = 0, ri = 0, ei = 0, pending = [], tree, props;
  const calls = [], cache = new Map();
  const react = { useState(initial) { const i = si++; if (!(i in states)) states[i] = typeof initial === "function" ? initial() : initial; return [states[i], value => { states[i] = typeof value === "function" ? value(states[i]) : value; }]; }, useRef(initial) { return refs[ri++] ??= { current: initial }; }, useEffect(fn, dependencies) { const i = ei++, old = effects[i]; if (!old || dependencies.some((v, j) => !Object.is(v, old.dependencies[j]))) pending.push(() => { old?.cleanup?.(); effects[i] = { dependencies, cleanup: fn() }; }); }, useCallback(fn) { return fn; } };
  const jsx = (type, props, key) => ({ type, props: props ?? {}, key });
  const api = { async api(...args) { calls.push(args); return request(...args); }, async importKnowledge(...args) { calls.push(["IMPORT", ...args]); return request("IMPORT", ...args); }, ApiError, idPath: encodeURIComponent, errorText: e => e.message, listOf: (value, key) => Array.isArray(value) ? value : value[key], setCsrf() {} };
  let lastRunBody;
  const goalRun = {
    generationLabels: { "goal.started": "目标提取已开始", "generation.started": "正在提取目标要求", "goal.completed": "提取完成" }, generationFailure: reason => `提取失败：${reason}`,
    async startGoalGeneration(body) { lastRunBody = body; calls.push(["/goal-draft-runs", "POST", body]); const value = await request("/goal-draft-runs", "POST", body); return { run_id: "run", request_id: body.request_id, version_ids: body.version_ids, ...value }; },
    async watchGoalGeneration(identity, signal, onEvent) { onEvent({ ...identity, seq: 1, timestamp: "2026-10-26T12:00:00Z", type: "generation.started", data: { method: "rule" } }); const final = { ...identity, seq: 2, timestamp: "2026-10-26T12:00:01Z", type: "goal.completed", data: {} }; onEvent(final); return final; },
    async getGoalGeneration() { return { status: "completed", result: { draft: lastRunBody.regenerates_id ? await request(`/goal-drafts/${encodeURIComponent(lastRunBody.regenerates_id)}/regenerate`, "POST", { version_ids: lastRunBody.version_ids, expected_revision: lastRunBody.expected_revision }) : draft } }; },
    async cancelGoalGeneration(id) { calls.push([`/goal-draft-runs/${id}/cancel`, "POST", {}]); return { status: "cancelled" }; },
  };
  const ui = { Feedback: "Feedback", PanelHeading: "PanelHeading", Empty: "Empty", SourceLink: "SourceLink", useAction() { return { error: "", notice: "", busy: false, setError() {}, setNotice() {}, async run(fn) { await fn(); } }; } };
  function load(file) {
    if (cache.has(file)) return cache.get(file).exports;
    const module = { exports: {} }; cache.set(file, module);
    const code = ts.transpileModule(readFileSync(file, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    const localRequire = name => name === "react" ? react : name === "react/jsx-runtime" ? { jsx, jsxs: jsx, Fragment: "Fragment" } : name === "./api" ? api : name === "./goalDraftRun" ? goalRun : name === "./stream" ? { StreamError: class StreamError extends Error {} } : name === "./ui" ? ui : name === "./types" || name === "./Guide" ? load(resolve(directory, `${name.slice(2)}.${name === "./types" ? "ts" : "tsx"}`)) : name.endsWith(".css") ? {} : name.startsWith(".") ? { default: name.slice(2) } : require(name);
    new Function("require", "module", "exports", code)(localRequire, module, module.exports); return module.exports;
  }
  const exports = load(resolve(directory, filename));
  function render(next = props) { props = next; si = ri = ei = 0; tree = exports.default(props); const jobs = pending; pending = []; jobs.forEach(fn => fn()); return tree; }
  const textOf = n => Array.isArray(n) ? n.map(textOf).join("") : n && typeof n === "object" ? textOf(n.props?.children) : n === false || n == null ? "" : String(n);
  const nodes = () => { const result = []; function visit(n) { if (Array.isArray(n)) n.forEach(visit); else if (n && typeof n === "object") { result.push(n); visit(n.props?.children); } } visit(tree); return result; };
  const find = (type, label) => { const n = nodes().find(n => n.type === type && (n.props["aria-label"] === label || textOf(n) === label)); assert.ok(n, `${type} ${label}`); return n; };
  return { render, nodes, calls, exports, goalRun, unmount() { for (const effect of effects) effect?.cleanup?.(); }, setComponent(name) { exports.default = exports[name]; }, text: () => textOf(tree), find, async settle() { await new Promise(resolve => setImmediate(resolve)); render(); }, click(label) { const n = find("button", label); assert.ok(!n.props.disabled, label); n.props.onClick(); render(); }, change(type, label, value) { find(type, label).props.onChange({ target: { value } }); render(); } };
}
const requestFor = async (path, method, body) => path === "/goal-drafts" ? method === "POST" ? draft : [draft] : path === "/spaces" ? [basis, { ...basis, id: "private", kind: "private" }] : path.endsWith("/documents") ? [doc] : method === "PATCH" ? { ...draft, revision: 3, nodes: body.nodes } : draft;
async function drafts(request = requestFor) { const h = harness("GoalDrafts.tsx", request); h.render({ user: owner, onClose() {}, onPublished() {} }); await h.settle(); h.click("待核对 · — · r2"); await h.settle(); return h; }
async function confirm(value, fn) { const previous = globalThis.window; const messages = []; globalThis.window = { confirm: text => { messages.push(text); return value; } }; try { await fn(messages); } finally { if (previous === undefined) delete globalThis.window; else globalThis.window = previous; } }

test("goal product clearly pauses AI, labels local assistance and preserves historical AI provenance", async () => {
  const historical = { ...draft, method: "ai", generation: { method: "ai" }, nodes: [{ ...node, generation_method: "ai" }] };
  const h = await drafts(async (path, method, body) => path === "/goal-drafts" ? [historical] : path === `/goal-drafts/${encodeURIComponent(draft.id)}` ? historical : requestFor(path, method, body));
  assert.match(h.text(), /AI 目标生成与重新生成已暂停/);
  assert.match(h.text(), /不理解研究语义，可能漏提或误提/);
  assert.ok(h.find("button", "本地原文辅助提取（需核对）"));
  assert.ok(h.find("button", "本地规则重新提取差异草案"));
  assert.match(h.text(), /AI 生成建议（历史记录）/);
  assert.ok(!h.nodes().some(n => n.type === "button" && /AI.*生成/.test(String(n.props.children))));
});

test("goal extraction shows real stages until completion, stops once and does not repeat a generation POST", async () => {
  const h = harness("GoalDrafts.tsx", requestFor), pending = deferred(); let emit;
  h.goalRun.watchGoalGeneration = async (identity, signal, onEvent) => { emit = onEvent; signal.addEventListener("abort", () => pending.resolve({ ...identity, type: "goal.cancelled", data: { reason: "cancelled" } })); return pending.promise; };
  h.render({ user: owner, onClose() {}, onPublished() {} }); await h.settle();
  h.change("select", "草稿项目知识库", basis.id); await h.settle(); h.find("input", "立项依据 v1").props.onChange({ target: { checked: true } }); h.render(); h.click("本地原文辅助提取（需核对）"); await h.settle();
  assert.match(h.text(), /正在执行目标提取/); emit({ run_id: "run", seq: 1, timestamp: "2026-10-26T12:00:00Z", type: "generation.started", data: { method: "ai" } }); h.render();
  assert.match(h.text(), /历史 AI 阶段（当前产品已暂停）/); h.click("停止提取"); await h.settle();
  assert.equal(h.calls.filter(([path]) => path === "/goal-draft-runs").length, 1); assert.equal(h.calls.filter(([path]) => path.endsWith("/cancel")).length, 1); assert.match(h.text(), /已停止/); h.unmount();
});

test("stopping before run creation returns cancels the late run without subscribing", async () => {
  const h = harness("GoalDrafts.tsx", requestFor), pending = deferred(); let watched = false;
  h.goalRun.startGoalGeneration = body => { h.calls.push(["/goal-draft-runs", "POST", body]); return pending.promise; }; h.goalRun.watchGoalGeneration = async () => { watched = true; };
  h.render({ user: owner, onClose() {}, onPublished() {} }); await h.settle(); h.change("select", "草稿项目知识库", basis.id); await h.settle(); h.find("input", "立项依据 v1").props.onChange({ target: { checked: true } }); h.render(); h.click("本地原文辅助提取（需核对）"); h.click("停止提取");
  pending.resolve({ run_id: "late", request_id: "r", version_ids: ["v1"] }); await h.settle(); assert.equal(watched, false); assert.equal(h.calls.filter(([path]) => path === "/goal-draft-runs/late/cancel").length, 1); assert.match(h.text(), /已停止/); h.unmount();
});

test("draft list and spaces load independently; stale source readiness is per draft", async () => {
  const waiting = deferred(), obsolete = { ...draft, id: "obsolete", revision: 1, publication_readiness: { publishable: false, reasons: ["来源版本已更新"] } };
  const h = harness("GoalDrafts.tsx", async path => path === "/spaces" ? waiting.promise : path === "/goal-drafts" ? [obsolete, draft] : obsolete);
  h.render({ user: owner, onClose() {}, onPublished() {} }); await h.settle();
  assert.ok(h.find("button", "待核对 · — · r2")); assert.match(h.text(), /此稿暂不可发布：来源版本已更新/); assert.doesNotMatch(h.text(), /正在加载草稿/);
  h.click("待核对 · — · r1"); await h.settle(); assert.equal(h.find("button", "人工确认并追加发布").props.disabled, true);
  waiting.resolve([basis]); await h.settle(); assert.equal(h.find("select", "草稿项目知识库").props.disabled, false);
  const failed = harness("GoalDrafts.tsx", async path => { if (path === "/spaces") throw new Error("library unavailable"); return [draft]; });
  failed.render({ user: owner, onClose() {}, onPublished() {} }); await failed.settle(); assert.ok(failed.find("button", "待核对 · — · r2")); assert.ok(failed.nodes().some(n => n.type === "Feedback" && n.props.error === "library unavailable"));
});

test("owner abandons unpublished draft only after explicit confirmation and preserves read-only history", async () => {
  const h = await drafts(async (path, method, body) => path.endsWith("/abandon") ? { ...draft, status: "abandoned", revision: 3, publication_readiness: { publishable: false, reasons: ["Draft abandoned"] } } : requestFor(path, method, body));
  await confirm(false, async () => { h.click("废弃此草稿"); await h.settle(); assert.ok(!h.calls.some(([p]) => p.endsWith("/abandon"))); });
  await confirm(true, async () => { h.click("废弃此草稿"); await h.settle(); }); assert.deepEqual(h.calls.find(([p]) => p.endsWith("/abandon")), ["/goal-drafts/draft%2F1/abandon", "POST", { expected_revision: 2, confirmed: true }]); assert.match(h.text(), /已废弃草稿（只读历史）/); assert.equal(h.find("button", "保存草稿修改").props.disabled, true); assert.ok(!h.nodes().some(n => n.type === "button" && n.props.children === "废弃此草稿"));
});

test("field evidence and generation method stay separate; edits invalidate item check and require saved recheck", async () => {
  const h = await drafts(); assert.match(h.text(), /原文明确要求/); assert.match(h.text(), /规则提取/); assert.equal(h.find("textarea", "草稿目标1交付物").props.value, "研究报告"); assert.equal(h.find("textarea", "草稿目标1时间要求（原文）").props.value, "六个月内完成"); assert.equal(h.find("input", "草稿目标1日期").props.value, "");
  h.find("input", "已核对草稿目标1").props.onChange({ target: { checked: true } }); h.render(); assert.equal(h.find("button", "人工确认并追加发布").props.disabled, false);
  h.change("textarea", "草稿目标1交付物", "人工新增交付物"); assert.match(h.text(), /建议（非原文要求）.*人工改动/); assert.equal(h.find("input", "已核对草稿目标1").props.checked, false);
  assert.equal(h.find("button", "人工确认并追加发布").props.disabled, true); h.click("保存草稿修改"); await h.settle();
  const sent = h.calls.find(([, method]) => method === "PATCH")[2].nodes[0]; assert.equal(sent.generation_method, "manual"); assert.equal(sent.origin, "suggested"); assert.deepEqual(sent.field_sources, node.field_sources); assert.equal(sent.deliverables, "人工新增交付物");
  assert.equal(h.find("button", "人工确认并追加发布").props.disabled, true);
});

test("regeneration is proposal-only; each adoption requires checked item, confirmation and both revisions", async () => {
  const suggestion = { id: "s1", operation: "modify", node: { ...node, title: "建议目标", generation_method: "ai", origin: "suggested" }, before: { id: "g1", title: "当前目标", description: "当前说明", acceptance_criteria: "", revision: 7, parent_id: null, position: 0 }, target_goal_id: "g1", expected_goal_revision: 7, status: "pending" };
  const generated = { ...draft, id: "regenerated", revision: 3, generation: { method: "ai" }, suggestions: [suggestion] };
  const h = await drafts(async (path, method, body) => path === "/goals" ? [{ id: "g1", title: "当前目标", description: "当前说明", acceptance_criteria: "", revision: 7, parent_id: null, position: 0 }] : path.endsWith("/regenerate") ? generated : path.endsWith("/adopt") ? { ...generated, revision: 4, suggestions: [{ ...suggestion, status: "adopted" }] } : requestFor(path, method, body));
  h.change("select", "草稿项目知识库", basis.id); await h.settle(); h.find("input", "立项依据 v1").props.onChange({ target: { checked: true } }); h.render(); h.click("本地规则重新提取差异草案"); await h.settle();
  const generationRequest = h.calls.find(([path]) => path === "/goal-draft-runs")[2]; assert.deepEqual(generationRequest.version_ids, ["v1"]); assert.equal(generationRequest.regenerates_id, draft.id); assert.equal(generationRequest.expected_revision, 2); assert.ok(generationRequest.request_id); assert.ok(!h.calls.some(([path, method]) => path.startsWith("/goals") && method && method !== "GET")); assert.match(h.text(), /AI 生成建议/); await h.settle(); assert.match(h.text(), /当前：当前目标/); assert.match(h.text(), /建议：建议目标/); assert.equal(h.find("button", "保存草稿修改").props.disabled, true);
  assert.equal(h.find("button", "确认采纳此项修改").props.disabled, true); h.find("input", "已核对差异s1").props.onChange({ target: { checked: true } }); h.render();
  await confirm(false, async () => { h.click("确认采纳此项修改"); await h.settle(); assert.ok(!h.calls.some(([path]) => path.endsWith("/adopt"))); });
  await confirm(true, async () => { h.click("确认采纳此项修改"); await h.settle(); });
  assert.deepEqual(h.calls.find(([path]) => path.endsWith("/adopt")), ["/goal-drafts/regenerated/adopt", "POST", { suggestion_id: "s1", expected_revision: 3, expected_goal_revision: 7, confirmed: true }]); assert.match(h.text(), /已采纳/);
  const conflict = await drafts(async (path, method, body) => { if (path.endsWith("/adopt")) throw new ApiError(409, "goal_revision_changed"); return path === "/goal-drafts/draft%2F1" ? generated : requestFor(path, method, body); });
  conflict.find("input", "已核对差异s1").props.onChange({ target: { checked: true } }); conflict.render(); await confirm(true, async () => { conflict.click("确认采纳此项修改"); await conflict.settle(); });
  assert.equal(conflict.find("button", "确认采纳此项修改").props.disabled, true); assert.equal(conflict.calls.filter(([path]) => path.endsWith("/adopt")).length, 1); assert.match(conflict.text(), /不自动 rebase/);
});

test("difference rejection confirms and records draft revision without changing formal goals", async () => {
  const suggestion = { id: "reject-me", operation: "add", node, before: null, target_goal_id: null, expected_goal_revision: null, status: "pending" }, generated = { ...draft, regenerates_id: "previous", suggestions: [] }; generated.suggestions = [suggestion];
  const h = await drafts(async (path, method, body) => path.endsWith("/reject") ? { ...generated, revision: 3, suggestions: [{ ...suggestion, status: "rejected" }] } : path === "/goal-drafts/draft%2F1" ? generated : requestFor(path, method, body));
  await confirm(false, async () => { h.click("拒绝此项新增"); await h.settle(); assert.ok(!h.calls.some(([p]) => p.endsWith("/reject"))); });
  await confirm(true, async () => { h.click("拒绝此项新增"); await h.settle(); }); assert.deepEqual(h.calls.find(([p]) => p.endsWith("/reject")), ["/goal-drafts/draft%2F1/reject", "POST", { suggestion_id: "reject-me", expected_revision: 2, confirmed: true }]); assert.match(h.text(), /已拒绝/); assert.equal(h.find("button", "拒绝此项新增").props.disabled, true); assert.ok(!h.calls.some(([p]) => p.startsWith("/goals")));
});

test("teaching dismissal is user-role-topic-version scoped, reopenable, and never executes or confirms operations", async () => {
  const previous = globalThis.localStorage, storage = new Map(); globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
  try {
    const h = harness("Guide.tsx"); h.setComponent("SafetyLesson"); const props = { user: owner, topic: "review" }; h.render(props); assert.doesNotMatch(h.text(), /送审的是指定版本/); h.click("重看送审教学说明"); assert.match(h.text(), /送审的是指定版本/); h.click("关闭教学说明"); assert.equal(h.calls.length, 0);
    assert.deepEqual([...storage], [["sciloam-lesson-v1:owner:owner:review", "dismissed"]]); h.click("重看送审教学说明"); assert.match(h.text(), /关闭说明不会确认、跳过或执行任何操作/);
    h.render({ user: { ...owner, role: "member" }, topic: "deletion" }); await h.settle(); assert.doesNotMatch(h.text(), /删除、撤回与独立副本/); h.click("重看删除教学说明"); assert.match(h.text(), /删除、撤回与独立副本/);
    const d = harness("Documents.tsx", async () => []); d.render({ user: owner, space: { ...basis, kind: "collaboration", role: "owner" }, spaces: [] }); await d.settle(); d.click("删除知识库"); assert.ok(d.find("button", "确认删除知识库")); assert.ok(d.nodes().some(n => typeof n.type === "function" && n.type.name === "SafetyLesson")); assert.ok(!d.calls.some(([, method]) => method === "DELETE"));
  } finally { if (previous === undefined) delete globalThis.localStorage; else globalThis.localStorage = previous; }
});

test("official goal details render immutable source snapshots and human confirmation", async () => {
  const goal = { id: "g", parent_id: null, title: "正式目标", description: "正式说明", acceptance_criteria: "人工标准", due_date: null, status: "not_started", position: 0, revision: 1, can_manage: true, can_contribute: true, path_titles: ["正式目标"], path: "正式目标", archived_at: null };
  const detail = { ...goal, knowledge: [], updates: [], status_history: [], generation_sources: [{ id: "provenance", goal_id: goal.id, draft_id: draft.id, node_id: node.id, node, generation: { method: "rule" }, confirmation: { actor_id: "owner", confirmed: true, draft_revision: 2, confirmed_at: "2026-10-26T12:00:00Z" }, operation: "add" }] };
  const h = harness("Goals.tsx", async path => path === "/goals" ? [goal] : detail); h.render({ user: owner }); await h.settle(); h.nodes().find(n => n.type === "button" && n.props.className === "rw-phase6-node").props.onClick(); await h.settle();
  assert.match(h.text(), /正式目标来源与确认详情/); assert.match(h.text(), /确认人：owner/); assert.match(h.text(), /草稿 r2/); assert.match(h.text(), /研究报告/); assert.match(h.text(), /字符 \[10, 14\)/);
});

test("binary import sends original bytes, encoded unicode headers, current CSRF and credentials", async () => {
  const previousFetch = globalThis.fetch, previousDocument = globalThis.document;
  try {
    setCsrf("old"); globalThis.document = { cookie: "research_csrf=live%2Fcsrf" };
    const file = new File([new Uint8Array([0, 255, 1, 80])], "课题 笔记.docx");
    globalThis.fetch = async (url, options) => { assert.equal(url, "/api/spaces/basis%2F1/imports"); assert.equal(options.body, file); assert.equal(options.credentials, "include"); assert.equal(options.headers["Content-Type"], "application/octet-stream"); assert.equal(options.headers["X-Filename"], encodeURIComponent(file.name)); assert.equal(options.headers["X-Knowledge-Title"], encodeURIComponent("研究标题")); assert.equal(options.headers["X-Source"], encodeURIComponent("中文出处 / 1")); assert.equal(options.headers["X-CSRF-Token"], "live/csrf"); return new Response(JSON.stringify({ id: "doc", import_report: { warnings: ["无分页"] } })); };
    assert.equal((await importKnowledge("basis/1", file, { title: "研究标题", source: "中文出处 / 1" })).id, "doc");
    globalThis.fetch = async () => new Response("<html>bad gateway</html>", { status: 502 });
    await assert.rejects(importKnowledge("s", file), e => e instanceof ApiError && e.status === 502 && /JSON/.test(e.message));
    await assert.rejects(importKnowledge("s", new File(["pdf"], "x.pdf")), /PDF 暂不可用/);
    await assert.rejects(importKnowledge("s", new File([new Uint8Array(8 * 1024 * 1024 + 1)], "x.docx")), /8 MB/);
  } finally { globalThis.fetch = previousFetch; if (previousDocument === undefined) delete globalThis.document; else globalThis.document = previousDocument; setCsrf(); }
});

test("real draft TSX hides member UI and does not request drafts; Goals entry paused for everyone", async () => {
  const h = harness("GoalDrafts.tsx"); h.render({ user: member }); await h.settle(); assert.equal(h.text(), ""); assert.equal(h.calls.length, 0);
  const g = harness("Goals.tsx"); g.render({ user: member }); await g.settle(); assert.ok(!g.nodes().some(n => n.type === "button" && /草稿/.test(n.props.children)));
  g.render({ user: owner }); await g.settle(); assert.ok(!g.nodes().some(n => n.type === "button" && /草稿|本地原文辅助提取/.test(n.props.children ?? "")));
});

test("real draft editing preserves quote, marks suggested, prevents parent cycles and sends revision", async () => {
  const h = await drafts(); assert.match(h.text(), /字符 \[10, 14\).*非页码/); assert.match(h.text(), /实验目标/);
  h.change("input", "草稿目标1标题", "人工目标"); h.click("保存草稿修改"); await h.settle();
  const patch = h.calls.find(([, method]) => method === "PATCH"); assert.equal(patch[2].expected_revision, 2); assert.equal(patch[2].nodes[0].origin, "suggested"); assert.deepEqual(patch[2].nodes[0].source, node.source);
  h.click("添加人工建议节点"); h.change("input", "草稿目标2标题", "子目标"); h.change("select", "草稿目标2上级", node.id);
  const firstParentValues = h.find("select", "草稿目标1上级").props.children.flat(Infinity).filter(n => n?.type === "option").map(n => n.props.value);
  assert.deepEqual(firstParentValues, [""], "self and descendant must both be excluded from parent choices");
  h.click("保存草稿修改"); await h.settle(); const last = h.calls.filter(([, method]) => method === "PATCH").at(-1)[2].nodes[1]; assert.equal(last.origin, "suggested"); assert.equal(last.source, null);
});

test("publication requires explicit human confirm and only appends with receipt; 409 never retries", async () => {
  const h = await drafts(async (path, method, body) => path.endsWith("/publish") ? { draft_id: draft.id, goal_ids: ["goal-a", "goal-b"] } : requestFor(path, method, body));
  assert.equal(h.find("button", "人工确认并追加发布").props.disabled, true);
  h.find("input", "已核对草稿目标1").props.onChange({ target: { checked: true } }); h.render();
  await confirm(false, async () => { h.click("人工确认并追加发布"); await h.settle(); assert.ok(!h.calls.some(([path]) => path.endsWith("/publish"))); });
  await confirm(true, async messages => { h.click("人工确认并追加发布"); await h.settle(); assert.deepEqual(h.calls.find(([path]) => path.endsWith("/publish")), ["/goal-drafts/draft%2F1/publish", "POST", { expected_revision: 2, confirmed: true }]); assert.match(messages[0], /只追加新目标，不覆盖已有目标/); assert.match(messages[0], /所有项目成员可读/); assert.match(h.text(), /goal-a/); });
  const conflict = await drafts(async (path, method, body) => { if (method === "PATCH") throw new ApiError(409, "Source changed"); return requestFor(path, method, body); });
  conflict.change("input", "草稿目标1标题", "保留编辑"); conflict.click("保存草稿修改"); await conflict.settle(); assert.equal(conflict.find("input", "草稿目标1标题").props.value, "保留编辑"); assert.equal(conflict.find("button", "人工确认并追加发布").props.disabled, true); assert.match(conflict.text(), /必须重新提取|重新提取并核对/); assert.equal(conflict.calls.filter(([, method]) => method === "PATCH").length, 1);
});

test("guide uses user and role local dismissal, can reopen, and buttons call real destination callbacks", async () => {
  const previous = globalThis.localStorage, previousDocument = globalThis.document, storage = new Map(), navigations = [];
  globalThis.document = { addEventListener() {}, removeEventListener() {} };
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
  try {
    const h = harness("Guide.tsx"); const props = { user: owner, reopen: 0, onNavigate: value => navigations.push(value) }; h.render(props); assert.equal(h.text(), ""); h.render({ ...props, reopen: 1 }); await h.settle(); assert.equal(h.nodes().find(n => n.props.role === "dialog").props["aria-modal"], "true"); h.click("打开项目知识"); assert.deepEqual(navigations, ["basis"]); h.render({ ...props, reopen: 2 }); await h.settle(); h.click("关闭指南"); assert.equal(h.text(), ""); assert.equal(storage.get("sciloam-guide-v1:owner:owner"), "dismissed");
    h.render({ ...props, reopen: 3 }); await h.settle(); assert.match(h.text(), /负责人使用指南/);
    h.render({ ...props, user: { ...owner, role: "member" }, reopen: 3 }); await h.settle(); assert.equal(h.text(), ""); h.render({ ...props, user: { ...owner, role: "member" }, reopen: 4 }); await h.settle(); assert.match(h.text(), /成员使用指南/); assert.doesNotMatch(h.text(), /目标草稿/); h.click("打开个人知识"); assert.equal(navigations.at(-1), "private");
    assert.ok([...storage.keys()].every(key => !key.includes("research")));
  } finally { if (previous === undefined) delete globalThis.localStorage; else globalThis.localStorage = previous; if (previousDocument === undefined) delete globalThis.document; else globalThis.document = previousDocument; }
});

test("draft source picker uses basis only, readable current versions and caps selection at five", async () => {
  const docs = Array.from({ length: 6 }, (_, i) => ({ ...doc, id: `d${i}`, title: `依据${i}`, active_version: { ...version, id: `v${i}` }, versions: [{ ...version, id: `v${i}` }, { id: `old${i}`, number: 0, is_current: false }, { id: `withdrawn${i}`, lifecycle_status: "withdrawn", is_current: true }] }));
  const h = harness("GoalDrafts.tsx", async (path, method, body) => path.endsWith("/documents") ? docs : requestFor(path, method, body)); h.render({ user: owner, onClose() {}, onPublished() {} }); await h.settle();
  assert.ok(!h.nodes().some(n => n.type === "option" && n.props.value === "private")); h.change("select", "草稿项目知识库", basis.id); await h.settle();
  for (let i = 0; i < 5; i++) { h.find("input", `依据${i} v1`).props.onChange({ target: { checked: true } }); h.render(); }
  assert.equal(h.find("input", "依据5 v1").props.disabled, true); assert.equal(h.nodes().filter(n => n.type === "input" && n.props.type === "checkbox").length, 6);
  h.click("本地原文辅助提取（需核对）"); await h.settle(); const request = h.calls.find(([path]) => path === "/goal-draft-runs")[2]; assert.deepEqual(request.version_ids, ["v0", "v1", "v2", "v3", "v4"]); assert.ok(request.request_id);
});

test("workspace guide callbacks navigate to real library pages but do not leave an active run", async () => {
  const old = globalThis.document; let running = true; globalThis.document = { querySelector: () => running ? {} : null };
  try {
    const h = harness("ResearchApp.tsx", async path => path === "/auth/me" ? { user: owner } : path === "/spaces" ? [basis] : []); h.render(); await h.settle(); await h.settle();
    const guide = () => h.nodes().find(n => typeof n.type === "function" && n.type.name === "Guide"); assert.ok(guide()); guide().props.onNavigate("basis"); h.render();
    assert.ok(h.nodes().some(n => n.type === "Chat")); assert.match(h.nodes().find(n => n.type === "Feedback").props.notice, /仍在运行/);
    running = false; guide().props.onNavigate("basis"); h.render(); assert.ok(h.nodes().some(n => n.type === "Documents" && n.props.space.id === basis.id));
    guide().props.onNavigate("goals"); h.render(); assert.ok(h.nodes().some(n => n.type === "Goals"));
  } finally { if (old === undefined) delete globalThis.document; else globalThis.document = old; }
});

test("draft detail responses cannot refill a changed user role", async () => {
  const pending = deferred(); const h = harness("GoalDrafts.tsx", async (path, method, body) => path === "/goal-drafts/draft%2F1" ? pending.promise : requestFor(path, method, body));
  h.render({ user: owner, onClose() {}, onPublished() {} }); await h.settle(); h.click("待核对 · — · r2"); h.render({ user: member, onClose() {}, onPublished() {} }); pending.resolve(draft); await h.settle(); assert.equal(h.text(), "");
});

test("reading layout folds version/share history and does not duplicate saved notices", async () => {
  const current = { ...version, content: "原文内容", filename: "note.md", lifecycle_status: "active" }, item = { ...doc, latest_version: current, active_version: current, versions: [current] };
  const writable = { ...basis, kind: "private", role: "owner" };
  const h = harness("Documents.tsx", async path => path.endsWith("/documents") ? [item] : path.startsWith("/versions/") ? current : []);
  h.render({ user: owner, space: writable, spaces: [writable] }); await h.settle();
  h.nodes().find(n => n.type === "button" && n.props.className?.includes("rw-doc-item")).props.onClick(); await h.settle();
  for (const label of ["版本历史 · 1 个版本", "送审与分享"]) {
    const summary = h.find("summary", label); const container = h.nodes().find(n => n.type === "details" && n.props.children?.includes?.(summary));
    assert.ok(container); assert.notEqual(container.props.open, true);
  }
  assert.ok(h.nodes().some(n => n.type === "p" && /负责人不提交送审/.test(n.props.children)) && !h.nodes().some(n => n.type === "button" && n.props.children === "送审当前所选版本")); assert.ok(!h.nodes().some(n => n.props.className === "rw-phase5-library-heading"));
  const submitter = harness("Documents.tsx", async path => path.endsWith("/documents") ? [item] : path.startsWith("/versions/") ? current : []);
  submitter.render({ user: member, space: writable, spaces: [writable] }); await submitter.settle();
  submitter.nodes().find(n => n.type === "button" && n.props.className?.includes("rw-doc-item")).props.onClick(); await submitter.settle();
  assert.ok(submitter.find("button", "送审当前所选版本"));
});

test("Documents import requires selection then explicit save, shows warnings, locks double clicks and ignores old scope", async () => {
  const pending = deferred();
  const h = harness("Documents.tsx", async path => path === "IMPORT" ? pending.promise : path.endsWith("/documents") ? [] : []);
  const writable = { ...basis, role: "owner" }; const props = { user: owner, space: writable, spaces: [writable] };
  h.render(props); await h.settle(); h.click("＋ 录入知识"); h.click("上传文件"); const file = new File(["bytes"], "研究.docx");
  h.find("input", "待导入文件").props.onChange({ target: { files: [file] } }); h.render(); assert.ok(!h.calls.some(([path]) => path === "IMPORT"));
  const handler = h.nodes().find(n => n.type === "form").props.onSubmit; handler({ preventDefault() {} }); handler({ preventDefault() {} }); await h.settle(); assert.equal(h.calls.filter(([path]) => path === "IMPORT").length, 1);
  h.render({ ...props, space: { ...writable, id: "other" } }); await h.settle(); pending.resolve({ id: "imported", import_report: { warnings: ["旧空间警告"], page_support: false } }); await h.settle(); assert.doesNotMatch(h.text(), /旧空间警告/);
  const report = harness("Documents.tsx", async path => path === "IMPORT" ? { id: "imported", import_report: { warnings: ["表格已转为纯文本，无分页"], format: "docx" } } : []); report.render(props); await report.settle(); report.click("＋ 录入知识"); report.click("上传文件"); report.find("input", "待导入文件").props.onChange({ target: { files: [file] } }); report.render(); report.nodes().find(n => n.type === "form").props.onSubmit({ preventDefault() {} }); await report.settle(); assert.match(report.text(), /表格已转为纯文本，无分页/);
  const denied = harness("Documents.tsx", async () => []); denied.render({ ...props, space: basis }); await denied.settle(); assert.ok(!denied.nodes().some(n => n.type === "button" && /导入/.test(n.props.children)));
});
