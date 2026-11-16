import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import ts from "typescript";

const directory = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const member = { id: "member", email: "member@example.org", role: "member" };
const owner = { id: "leader", email: "owner@example.org", role: "owner" };
const goal = { id: "goal/1", parent_id: null, title: "实验验证", description: "对齐评价口径", acceptance_criteria: "确认数据版本、样本范围和评价指标", due_date: null, status: "in_progress", position: 0, revision: 3, can_manage: false, can_contribute: true, path_titles: ["实验验证"], path: "实验验证", archived_at: null, knowledge: [], updates: [], status_history: [] };
const child = { ...goal, id: "child", parent_id: goal.id, title: "验收目标", path_titles: [goal.title, "验收目标"], path: "实验验证 / 验收目标" };
const privateSpace = { id: "private/1", name: "Private", kind: "private", owner_id: member.id, role: "owner" };
const version = { id: "uuid-version-v2", number: 2, lifecycle_status: "active", is_current: true };
const document = { id: "doc/1", title: "实验记录", versions: [{ id: "uuid-version-v1", number: 1 }, version, { id: "withdrawn", number: 3, lifecycle_status: "withdrawn" }], active_version: version };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

// Exercise real TSX effects and handlers without installing a DOM dependency.
function harness(filename = "Goals.tsx", request = async () => []) {
  let states = [], refs = [], effects = [], si = 0, ri = 0, ei = 0, pending = [], tree, props;
  const cache = new Map(), calls = [];
  const react = {
    useState(initial) { const i = si++; if (!(i in states)) states[i] = typeof initial === "function" ? initial() : initial; return [states[i], value => { states[i] = typeof value === "function" ? value(states[i]) : value; }]; },
    useRef(initial) { return refs[ri++] ??= { current: initial }; },
    useEffect(callback, dependencies) { const i = ei++, previous = effects[i]; if (!previous || !dependencies || dependencies.some((value, j) => !Object.is(value, previous.dependencies[j]))) pending.push(() => { previous?.cleanup?.(); effects[i] = { dependencies, cleanup: callback() }; }); },
    useCallback(callback) { return callback; },
  };
  const jsx = (type, props, key) => ({ type, props: props ?? {}, key });
  class ApiError extends Error { constructor(status, message) { super(message); this.status = status; } }
  const api = { async api(...args) { calls.push(args); return request(...args); }, ApiError, idPath: encodeURIComponent, errorText: error => error.message, listOf: (value, key) => Array.isArray(value) ? value : value[key], setCsrf() {} };
  const ui = { Feedback: "Feedback", PanelHeading: "PanelHeading", Empty: "Empty", SourceLink: "SourceLink", useAction() { const [error, setError] = react.useState(""); return { error, setError, setNotice() {}, busy: false, async run(fn) { try { await fn(); } catch (e) { setError(e.message); } } }; } };
  function load(file) {
    if (cache.has(file)) return cache.get(file).exports;
    const module = { exports: {} }; cache.set(file, module);
    const code = ts.transpileModule(readFileSync(file, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    const localRequire = name => name === "react" ? react : name === "react/jsx-runtime" ? { jsx, jsxs: jsx, Fragment: "Fragment" } : name === "./api" ? api : name === "./ui" ? ui : name === "./types" ? load(resolve(directory, "types.ts")) : name.endsWith(".css") ? {} : name.startsWith(".") ? { default: name.includes("MarkdownMessage") ? "MarkdownMessage" : name.slice(2) } : require(name);
    new Function("require", "module", "exports", code)(localRequire, module, module.exports); return module.exports;
  }
  const exports = load(resolve(directory, filename)), Component = exports.default;
  function render(next = props) { props = next; si = ri = ei = 0; tree = Component(props); const jobs = pending; pending = []; jobs.forEach(job => job()); return tree; }
  const nodes = () => { const result = []; const visit = node => { if (Array.isArray(node)) node.forEach(visit); else if (node && typeof node === "object") { result.push(node); visit(node.props?.children); } }; visit(tree); return result; };
  const textOf = node => Array.isArray(node) ? node.map(textOf).join("") : node && typeof node === "object" ? textOf(node.props?.children) : node === false || node == null ? "" : String(node);
  const button = label => { const found = nodes().find(node => node.type === "button" && textOf(node) === label); assert.ok(found, `missing button ${label}`); return found; };
  const find = (type, label) => { const found = nodes().find(node => node.type === type && (node.props["aria-label"] === label || node.props.id === label)); assert.ok(found, `missing ${type} ${label}`); return found; };
  const settle = async () => { await new Promise(resolve => setImmediate(resolve)); render(); };
  return { render, nodes, calls, button, find, settle, ApiError, exports, text: () => textOf(tree), textOf, hasButton: label => nodes().some(node => node.type === "button" && textOf(node) === label), feedback: () => nodes().filter(node => node.type === "Feedback").map(node => node.props), click(label) { const node = button(label); assert.ok(!node.props.disabled, `button disabled ${label}`); node.props.onClick(); render(); }, change(type, label, value) { find(type, label).props.onChange({ target: { value } }); render(); }, submit() { const form = nodes().find(node => node.type === "form"); assert.ok(form); form.props.onSubmit({ preventDefault() {} }); render(); }, open(index = 0) { const node = nodes().filter(node => node.type === "button" && node.props.className === "rw-phase6-node")[index]; assert.ok(node); node.props.onClick(); render(); } };
}
const requestFor = (detail = goal, list = [detail]) => async path => path === "/goals" ? list : path === "/spaces" ? [privateSpace] : path.endsWith("/documents") ? [document] : path.startsWith("/goals/") ? detail : [];
async function setup(detail = goal, user = member, request = requestFor(detail)) { const h = harness("Goals.tsx", request); h.render({ user }); await h.settle(); h.open(); await h.settle(); return h; }
async function chooseVersion(h, mode = "关联我的知识") { h.click(mode); await h.settle(); h.change("select", "目标知识库", privateSpace.id); await h.settle(); h.change("select", "目标知识", document.id); h.change("select", "目标知识版本", version.id); }
async function confirmed(fn, result = true) { const previous = globalThis.window, messages = []; globalThis.window = { confirm: text => { messages.push(text); return result; } }; try { await fn(messages); } finally { if (previous === undefined) delete globalThis.window; else globalThis.window = previous; } }

test("goal tree filters ancestors, preserves preorder, folds descendants, excludes cycles for reparenting", () => {
  const h = harness(), { visibleGoals, goalDescendants } = h.exports;
  const leaf = { ...child, id: "leaf", parent_id: child.id, title: "评价方法", path_titles: [goal.title, child.title, "评价方法"], path: `${child.path} / 评价方法` };
  assert.deepEqual(visibleGoals([goal, child, leaf], "", new Set([goal.id])).map(g => g.id), [goal.id]);
  assert.deepEqual(visibleGoals([goal, child, leaf], "评价方法", new Set([goal.id])).map(g => g.id), [goal.id, child.id, leaf.id]);
  assert.deepEqual([...goalDescendants([goal, child, leaf], child.id)], [child.id, leaf.id]);
  assert.deepEqual(visibleGoals([goal, child], "不存在", new Set()), []);
});

test("members read tree and contribute, never receive owner actions, assignment or AI controls", async () => {
  const h = await setup();
  assert.match(h.text(), /验收目标|确认数据版本/); assert.match(h.text(), /状态由负责人手动确认/);
  for (const label of ["＋ 添加目标", "编辑目标", "添加子目标", "确认阶段状态", "确认归档目标"]) assert.equal(h.hasButton(label), false);
  assert.equal(h.hasButton("关联我的知识"), true); assert.equal(h.hasButton("记录进展"), true);
  assert.ok(!h.nodes().some(n => n.type === "button" && /AI|生成草稿|自动完成|派单/.test(h.textOf(n))));
  assert.ok(!h.calls.some(([path]) => path.includes("tasks") || path === "/members"));
  assert.match(h.text(), /不附版本的进展正文对全体项目成员可读/);
});

test("owner management needs server capability; completed and archived goals stop contributions", async () => {
  const denied = await setup({ ...goal, can_manage: false }, owner); assert.equal(denied.hasButton("编辑目标"), false);
  for (const detail of [{ ...goal, status: "completed", can_contribute: false }, { ...goal, archived_at: 1780000000, can_contribute: false }]) {
    const h = await setup(detail); assert.equal(h.hasButton("关联我的知识"), false); assert.equal(h.hasButton("记录进展"), false); assert.match(h.text(), /已完成目标|已归档目标/);
  }
});

test("knowledge version picker uses authorized spaces/docs/vN and exact link body, no typed UUID", async () => confirmed(async messages => {
  const h = await setup(); await chooseVersion(h);
  assert.ok(!h.nodes().some(n => n.type === "input" && /version|uuid/i.test(n.props.placeholder ?? "")));
  const options = h.nodes().filter(n => n.type === "option"); assert.ok(options.some(n => /v2/.test(h.textOf(n)))); assert.ok(!options.some(n => n.props.value === "withdrawn"));
  assert.ok(!options.some(n => /uuid-version/.test(h.textOf(n))));
  h.click("确认关联所选版本"); await h.settle();
  assert.deepEqual(h.calls.find(([path, method]) => path.endsWith("/knowledge") && method === "POST"), ["/goals/goal%2F1/knowledge", "POST", { version_id: version.id }]);
  assert.match(messages[0], /个人知识库版本|不授予/); assert.match(h.text(), /关联不等于授予权限或完成目标/);
}));

test("updates are public by default, optional version comes from picker, double click is locked", async () => confirmed(async () => {
  const post = deferred();
  const h = await setup(goal, member, async (path, method) => path.endsWith("/updates") && method === "POST" ? post.promise : requestFor()(path));
  h.change("textarea", "新增目标进展", "已确认样本范围"); await chooseVersion(h, "选择参考知识版本（可选）");
  const submit = h.nodes().find(n => n.type === "form").props.onSubmit; submit({ preventDefault() {} }); submit({ preventDefault() {} }); await h.settle();
  assert.deepEqual(h.calls.filter(([, method]) => method === "POST"), [["/goals/goal%2F1/updates", "POST", { content: "已确认样本范围", version_id: version.id }]]);
  post.resolve({}); await h.settle(); assert.equal(h.find("textarea", "新增目标进展").props.value, "");
  h.change("textarea", "新增目标进展", "公开进展"); h.submit(); await h.settle();
  assert.deepEqual(h.calls.filter(([, method]) => method === "POST").at(-1)[2], { content: "公开进展" });
}));

test("explicit confirmations can cancel status, archive, contribution and unlink without mutations", async () => confirmed(async () => {
  const detail = { ...goal, status: "pending_acceptance", can_manage: true, knowledge: [{ id: "link", title: document.title, number: 2, linked_by: owner.id }] };
  const h = await setup(detail, owner);
  h.change("select", "目标阶段状态", "completed"); h.change("textarea", "状态确认依据", "已核验"); h.click("确认阶段状态");
  h.change("textarea", "归档原因", "结束研究"); h.click("确认归档目标"); h.click("取消关联");
  h.change("textarea", "新增目标进展", "进展"); h.submit(); await h.settle();
  assert.ok(!h.calls.some(([, method]) => method && method !== "GET"));
}, false));

test("status confirmation sends reason/revision, emphasizes human acceptance rather than counts", async () => confirmed(async messages => {
  const detail = { ...goal, status: "pending_acceptance", can_manage: true, status_history: [{ id: "history", from_status: "not_started", to_status: "in_progress", reason: "负责人核验立项依据", created_at: 1780000000 }] };
  const h = await setup(detail, owner);
  h.change("select", "目标阶段状态", "completed"); h.change("textarea", "状态确认依据", "样本、版本与评价指标已核验"); h.click("确认阶段状态"); await h.settle();
  assert.deepEqual(h.calls.find(([path, method]) => path.endsWith("/status") && method === "POST"), ["/goals/goal%2F1/status", "POST", { status: "completed", reason: "样本、版本与评价指标已核验", expected_revision: 3 }]);
  assert.match(messages[0], /不以关联知识数量|所有未归档子目标/); assert.match(h.text(), /负责人手动确认/); assert.match(h.text(), /负责人核验立项依据/);
  assert.ok(!h.calls.some(([, method, body]) => method === "PATCH" && body?.status));
}));

test("edit/create uses expected revision, acceptance criteria, parent and position without assignee", async () => confirmed(async () => {
  const detail = { ...goal, can_manage: true };
  const h = await setup(detail, owner, async (path, method) => method === "POST" ? { ...detail, id: "new-child", title: "子目标", parent_id: goal.id } : requestFor(detail, [detail, child])(path)); h.click("编辑目标");
  assert.ok(!h.nodes().some(n => n.type === "option" && [goal.id, child.id].includes(n.props.value)));
  h.change("input", "目标标题", "实验验收"); h.change("textarea", "验收标准", "明确数据范围"); h.change("input", "目标排序位置", "2"); h.submit(); await h.settle();
  const patch = h.calls.find(([, method]) => method === "PATCH");
  assert.equal(patch[2].expected_revision, 3); assert.equal(patch[2].acceptance_criteria, "明确数据范围"); assert.equal(patch[2].position, 2); assert.equal(patch[2].parent_id, null); assert.equal("assignee_id" in patch[2], false);
  h.click("添加子目标"); h.change("input", "目标标题", "子目标"); h.submit(); await h.settle();
  const create = h.calls.find(([path, method]) => path === "/goals" && method === "POST"); assert.equal(create[2].parent_id, goal.id); assert.equal("expected_revision" in create[2], false);
}));

test("409 preserves entered form, requests manual refresh and never retries or silently overwrites", async () => confirmed(async () => {
  let h;
  const detail = { ...goal, can_manage: true };
  h = await setup(detail, owner, async (path, method) => { if (method === "PATCH") throw new h.ApiError(409, "Revision conflict"); return requestFor(detail)(path); });
  h.click("编辑目标"); h.change("input", "目标标题", "未保存的新标题"); h.submit(); await h.settle();
  assert.equal(h.find("input", "目标标题").props.value, "未保存的新标题"); assert.equal(h.button("保存目标").props.disabled, true);
  assert.match(h.feedback().map(f => f.error).join(""), /手动刷新目标|不会自动覆盖/);
  assert.equal(h.calls.filter(([, method]) => method === "PATCH").length, 1);
  h.click("刷新目标"); await h.settle(); assert.equal(h.hasButton("编辑目标"), true);
}));

test("status/archive business 409 does not fake completion, archive sends expected_revision", async () => confirmed(async () => {
  let h;
  const detail = { ...goal, can_manage: true };
  h = await setup(detail, owner, async (path, method) => { if (method === "POST") throw new h.ApiError(409, "Active children"); return requestFor(detail)(path); });
  h.change("textarea", "归档原因", "结束阶段"); h.click("确认归档目标"); await h.settle();
  assert.deepEqual(h.calls.find(([, method]) => method === "POST"), ["/goals/goal%2F1/archive", "POST", { expected_revision: 3, reason: "结束阶段" }]);
  assert.match(h.feedback()[0].error, /未归档子目标|刷新目标/); assert.equal(h.calls.filter(([, method]) => method === "POST").length, 1);
}));

test("unlink shown only for current authorized link author or owner; no invisible metadata invented", async () => confirmed(async () => {
  const detail = { ...goal, knowledge: [{ id: "mine", title: "可读知识", number: 2, linked_by: member.id }, { id: "other", title: "其他可读知识", number: 1, linked_by: owner.id }] };
  const h = await setup(detail); assert.equal(h.nodes().filter(n => n.type === "button" && h.textOf(n) === "取消关联").length, 1);
  h.click("取消关联"); await h.settle(); assert.ok(h.calls.some(([path, method]) => path === "/goals/goal%2F1/knowledge/mine" && method === "DELETE"));
  assert.doesNotMatch(h.text(), /隐藏知识|无权访问.*条|uuid-version/);
}));

test("same-id selection and scope changes reject stale detail responses and late errors", async () => {
  const old = deferred(), fresh = deferred(); let reads = 0;
  const h = harness("Goals.tsx", async path => path === "/goals" ? [goal] : ++reads === 1 ? old.promise : fresh.promise);
  h.render({ user: member }); await h.settle(); h.open(); h.open(); fresh.resolve({ ...goal, description: "最新返回的详情" }); await h.settle();
  old.resolve({ ...goal, description: "过时的详情" }); await h.settle(); assert.match(h.text(), /最新返回/); assert.doesNotMatch(h.text(), /过时/);
  const late = deferred(); const other = harness("Goals.tsx", async path => path === "/goals" ? [goal] : late.promise);
  other.render({ user: member }); await other.settle(); other.open(); other.render({ user: { ...member, id: "new-user" } }); late.reject(new Error("旧成员敏感错误")); await other.settle();
  assert.doesNotMatch(other.text(), /旧成员敏感错误|对齐评价口径/); assert.equal(other.feedback()[0].error, "");
});

test("rapid space-picker switches reject stale authorized document lists", async () => {
  const first = deferred();
  const h = await setup(goal, member, async path => path === "/spaces" ? [privateSpace, { ...privateSpace, id: "other" }] : path === "/spaces/private%2F1/documents" ? first.promise : path === "/spaces/other/documents" ? [{ ...document, id: "new-doc", title: "新库知识" }] : requestFor()(path));
  h.click("关联我的知识"); await h.settle(); h.change("select", "目标知识库", privateSpace.id); h.change("select", "目标知识库", "other"); await h.settle(); first.resolve([{ ...document, title: "旧库敏感标题" }]); await h.settle();
  assert.match(h.text(), /新库知识/); assert.doesNotMatch(h.text(), /旧库敏感标题/);
});

test("Documents submission associates any selected goal path, never assignee/task filter", async () => {
  const h = harness("Documents.tsx", async (path, method) => path === "/goals" ? [child] : path.endsWith("/documents") ? [document] : path.startsWith("/versions/") ? { ...version, content: "正文" } : {});
  h.render({ user: member, space: privateSpace, spaces: [privateSpace] }); await h.settle();
  h.nodes().find(n => n.type === "button" && n.props.className?.includes("rw-doc-item")).props.onClick(); await h.settle(); h.click("送审当前所选版本");
  assert.ok(h.nodes().some(n => n.type === "option" && h.textOf(n) === child.path)); h.change("select", "送审关联目标", child.id); h.click("确认提交审核"); await h.settle();
  assert.deepEqual(h.calls.find(([path, method]) => path === "/reviews" && method === "POST"), ["/reviews", "POST", { version_id: version.id, goal_id: child.id }]);
  assert.ok(!h.calls.some(([path]) => path === "/tasks"));
});

test("collaboration creation sends goal_id selected by readable path", async () => {
  const h = harness("ResearchApp.tsx", async (path, method) => path === "/auth/me" ? { user: member } : path === "/goals" ? [child] : path === "/spaces" && method === "POST" ? privateSpace : []);
  h.render({}); await h.settle(); await h.settle(); h.click("我的知识库");
  h.nodes().find(n => n.type === "button" && n.props["aria-label"] === "创建协作知识库").props.onClick(); h.render(); await h.settle();
  const input = h.nodes().find(n => n.type === "input" && n.props.maxLength === 100); input.props.onChange({ target: { value: "评价协作库" } }); h.render();
  const select = h.nodes().find(n => n.type === "select"); select.props.onChange({ target: { value: child.id } }); h.render(); h.submit(); await h.settle();
  assert.deepEqual(h.calls.find(([path, method]) => path === "/spaces" && method === "POST")[2], { name: "评价协作库", kind: "collaboration", goal_id: child.id });
  assert.ok(!h.calls.some(([path]) => path === "/tasks"));
});

test("valid status transitions follow backend acceptance stages and require explicit reopening", async () => {
  const h = await setup({ ...goal, can_manage: true }, owner);
  const options = () => h.nodes().filter(n => n.type === "option");
  assert.equal(options().find(n => n.props.value === "completed").props.disabled, true);
  assert.equal(options().find(n => n.props.value === "pending_acceptance").props.disabled, false);
  h.change("select", "目标阶段状态", "completed"); h.change("textarea", "状态确认依据", "不能跳阶段");
  assert.equal(h.button("确认阶段状态").props.disabled, true);
  const completed = await setup({ ...goal, status: "completed", can_manage: true, can_contribute: false }, owner);
  const reopen = completed.nodes().find(n => n.type === "option" && n.props.value === "in_progress"); assert.equal(reopen.props.disabled, false);
  assert.equal(completed.hasButton("记录进展"), false);
});

test("archived goal displays manual reason and time without inventing a status transition", async () => {
  const h = await setup({ ...goal, archived_at: 1780000000, archived_reason: "本阶段范围已关闭", can_manage: false, can_contribute: false }, owner);
  assert.match(h.text(), /本阶段范围已关闭/); assert.match(h.text(), /归档是人工管理操作，不代表目标完成/); assert.match(h.text(), /尚无人工状态变更记录/);
  assert.equal(h.hasButton("确认归档目标"), false); assert.equal(h.hasButton("记录进展"), false);
});

test("goal idempotent POST allowlist extends existing contract and all mutations retain CSRF", async () => {
  const { api, setCsrf } = await import("./api.ts"); const originalFetch = globalThis.fetch, originalDocument = globalThis.document;
  const requests = [];
  try {
    globalThis.document = { cookie: "research_csrf=latest-token" }; setCsrf("old"); globalThis.fetch = async (path, request) => { requests.push({ path, ...request }); return new Response("{}"); };
    for (const path of ["/goals", "/goals/g/updates", "/goals/g/knowledge", "/spaces", "/tasks", "/reviews"]) await api(path, "POST", {});
    for (const request of requests) { assert.ok(request.headers["Idempotency-Key"]); assert.equal(request.headers["X-CSRF-Token"], "latest-token"); assert.equal(request.credentials, "include"); }
    assert.equal(new Set(requests.map(r => r.headers["Idempotency-Key"])).size, requests.length);
    for (const path of ["/goals/g/status", "/goals/g/archive"]) { await api(path, "POST", {}); assert.equal(requests.at(-1).headers["Idempotency-Key"], undefined); assert.equal(requests.at(-1).headers["X-CSRF-Token"], "latest-token"); }
  } finally { globalThis.fetch = originalFetch; if (originalDocument === undefined) delete globalThis.document; else globalThis.document = originalDocument; setCsrf(); }
});
