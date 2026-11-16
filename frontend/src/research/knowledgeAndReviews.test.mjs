import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import ts from "typescript";

const directory = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const member = { id: "member", email: "m@example.org", role: "member" };
const owner = { id: "leader", email: "l@example.org", role: "owner" };
const space = { id: "space-a", name: "实验协作库", kind: "collaboration", owner_id: member.id, role: "owner" };
const privateSpace = { ...space, id: "personal", name: "Private", kind: "private" };
const v1 = { id: "uuid-version-one", number: 1, lifecycle_status: "active", protected: false, created_at: 1780000000 };
const v2 = { id: "uuid-version-two", number: 2, lifecycle_status: "active", protected: false, is_current: true, created_at: 1780100000 };
const document = { id: "doc-a", title: "实验记录 A", created_by: member.id, latest_version: v2, active_version: v2, versions: [v2, v1] };
const receipt = { id: "review-a", version_id: v1.id, title: "实验记录快照", number: 1, submitted_by: member.id, status: "pending", source_space_kind: "private", mode: "snapshot", target_visibility: "results", can_review: true };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

// Runs actual TSX, real effects and event handlers; no DOM or external dependency.
function harness(filename, request = async () => []) {
  let state = [], refs = [], effects = [], si = 0, ri = 0, ei = 0, pending = [], tree, props;
  const calls = [], cache = new Map();
  const react = {
    useState(initial) { const i = si++; if (!(i in state)) state[i] = typeof initial === "function" ? initial() : initial; return [state[i], value => { state[i] = typeof value === "function" ? value(state[i]) : value; }]; },
    useRef(initial) { return refs[ri++] ??= { current: initial }; },
    useEffect(callback, dependencies) { const i = ei++, previous = effects[i]; if (!previous || !dependencies || dependencies.some((value, j) => !Object.is(value, previous.dependencies[j]))) pending.push(() => { previous?.cleanup?.(); effects[i] = { dependencies, cleanup: callback() }; }); },
    useCallback(callback) { return callback; },
  };
  const jsx = (type, props, key) => ({ type, props: props ?? {}, key });
  class ApiError extends Error { constructor(status, message) { super(message); this.status = status; } }
  const api = { async api(...args) { calls.push(args); return request(...args); }, errorText: error => error.message, idPath: encodeURIComponent, listOf: (value, key) => Array.isArray(value) ? value : value[key], ApiError, setCsrf() {} };
  const ui = { Feedback: "Feedback", Empty: "Empty", PanelHeading: "PanelHeading", SourceLink: "SourceLink", useAction() { const [error, setError] = react.useState(""); return { error, setError, setNotice() {}, busy: false, run: async fn => { try { await fn(); } catch (e) { setError(e.message); } } }; } };
  function load(file) {
    if (cache.has(file)) return cache.get(file).exports;
    const module = { exports: {} }; cache.set(file, module);
    const code = ts.transpileModule(readFileSync(file, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    const localRequire = name => name === "react" ? react : name === "react/jsx-runtime" ? { jsx, jsxs: jsx, Fragment: "Fragment" } : name === "./api" ? api : name === "./ui" ? ui : name === "./types" ? load(resolve(directory, "types.ts")) : name.endsWith(".css") ? {} : name.startsWith(".") ? { default: name.includes("MarkdownMessage") ? "MarkdownMessage" : name.slice(2) } : require(name);
    new Function("require", "module", "exports", code)(localRequire, module, module.exports); return module.exports;
  }
  const Component = load(resolve(directory, filename)).default;
  function render(next = props) { props = next; si = ri = ei = 0; tree = Component(props); const jobs = pending; pending = []; jobs.forEach(job => job()); return tree; }
  const nodes = () => { const result = []; const visit = node => { if (Array.isArray(node)) node.forEach(visit); else if (node && typeof node === "object") { result.push(node); visit(node.props?.children); } }; visit(tree); return result; };
  const textOf = node => Array.isArray(node) ? node.map(textOf).join("") : node && typeof node === "object" ? node.type === "MarkdownMessage" ? node.props.text : textOf(node.props?.children) : node === false || node == null ? "" : String(node);
  const button = label => { const found = nodes().find(node => node.type === "button" && textOf(node) === label); assert.ok(found, `missing button ${label}`); return found; };
  const find = (type, label) => { const found = nodes().find(node => node.type === type && (node.props["aria-label"] === label || node.props.id === label)); assert.ok(found, `missing ${type} ${label}`); return found; };
  const settle = async () => { await new Promise(resolve => setImmediate(resolve)); render(); };
  return { render, nodes, calls, button, find, settle, ApiError, text: () => textOf(tree), hasButton: label => nodes().some(node => node.type === "button" && textOf(node) === label), feedback: () => nodes().filter(node => node.type === "Feedback").map(node => node.props), click(label) { const node = button(label); assert.ok(!node.props.disabled, `button disabled ${label}`); node.props.onClick(); render(); }, change(type, label, value) { find(type, label).props.onChange({ target: { value } }); render(); } };
}
const docsRequest = (docs = [document]) => async path => path.endsWith("/documents") ? docs : path === "/goals" ? [] : path.startsWith("/versions/") ? { ...(path.endsWith(v1.id) ? v1 : v2), content: path.endsWith(v1.id) ? "旧版正文" : "当前正文", source: "https://example.org/paper", filename: "记录.md" } : {};
async function documents(request = docsRequest(), props = {}) { const h = harness("Documents.tsx", request); h.render({ space, spaces: [space], user: member, ...props }); await h.settle(); return h; }
async function choose(h) { const item = h.nodes().find(node => node.type === "button" && node.props.className?.includes("rw-doc-item")); assert.ok(item); item.props.onClick(); await h.settle(); }
async function reviews(request, user = owner) { const h = harness("Reviews.tsx", request ?? (async path => path === "/reviews" ? [receipt] : { ...receipt, content: "独立快照正文" })); h.render({ user }); await h.settle(); return h; }

test("document reader opens chat only with an explicitly chosen readable version", async () => {
  const selected = []; const h = await documents(docsRequest(), { onAskVersion: id => selected.push(id) });
  assert.equal(h.hasButton("以此版本提问"), false); await choose(h);
  h.change("select", "知识版本", v1.id); await h.settle(); h.click("以此版本提问");
  assert.deepEqual(selected, [v1.id]); assert.ok(!h.calls.some(([, method]) => method === "POST"));
  const inactive = { ...v1, lifecycle_status: "withdrawn" };
  const unavailable = await documents(docsRequest([{ ...document, versions: [v2, inactive] }]), { onAskVersion: id => selected.push(id) });
  await choose(unavailable); unavailable.change("select", "知识版本", v1.id); await unavailable.settle();
  assert.equal(unavailable.button("以此版本提问").props.disabled, true);
});

test("knowledge uses readable versions/dates and source only, no unsupported saved description", async () => {
  const h = await documents(); await choose(h);
  assert.match(h.text(), /当前正文|v2/); assert.match(h.text(), /版本历史/);
  assert.ok(!h.nodes().filter(n => ["h3", "strong", "option"].includes(n.type)).some(n => JSON.stringify(n.props.children).includes("uuid-version")));
  assert.equal(h.nodes().find(n => n.type === "SourceLink").props.source, "https://example.org/paper");
  h.click("新增版本"); assert.match(h.text(), /不支持更新说明/); assert.ok(!h.nodes().some(n => n.type === "input" && /description|说明/.test(n.props.id ?? "")));
  h.change("textarea", "knowledge-content", "新版本正文");
  h.nodes().find(n => n.type === "form").props.onSubmit({ preventDefault() {} }); await h.settle();
  const call = h.calls.find(([path, method]) => path === "/documents/doc-a/versions" && method === "POST");
  assert.deepEqual(call[2], { content: "新版本正文", filename: "记录.md", source: "https://example.org/paper" });
  assert.doesNotMatch(h.text(), /资料/);
});

test("one knowledge entry offers manual and file modes, saves only to the explicit writable library", async () => {
  const h = await documents(docsRequest([]), { spaces: [space, privateSpace, { ...space, id: "denied", role: "reader", owner_id: "other" }] });
  assert.equal(h.hasButton("导入文件（含 DOCX）"), false);
  h.click("＋ 录入知识"); h.click("上传文件");
  assert.ok(h.find("input", "待导入文件")); assert.equal(h.nodes().filter(n => n.type === "input" && n.props.type === "file").length, 1);
  assert.ok(!h.calls.some(([path, method]) => method === "POST"));
  const picker = h.find("select", "录入目标知识库");
  assert.ok(!JSON.stringify(picker).includes("denied")); h.change("select", "录入目标知识库", privateSpace.id);
  h.click("手动填写"); assert.ok(!h.nodes().some(n => n.type === "input" && n.props.type === "file"));
  h.change("input", "knowledge-title", "手动研究笔记"); h.change("textarea", "knowledge-content", "需要明确保存的正文");
  const submit = h.nodes().find(n => n.type === "form").props.onSubmit; submit({ preventDefault() {} }); submit({ preventDefault() {} }); await h.settle();
  assert.equal(h.calls.filter(([, method]) => method === "POST").length, 1);
  assert.deepEqual(h.calls.find(([, method]) => method === "POST"), ["/spaces/personal/documents", "POST", { title: "手动研究笔记", content: "需要明确保存的正文", filename: "", source: "" }]);
  assert.match(h.text(), /已保存至/); assert.ok(h.nodes().some(n => n.props["aria-label"] === "保存报告"));
});

test("private and writable collaboration submit directly after explicit visibility dialog, never share first", async () => {
  for (const source of [privateSpace, space]) {
    const post = deferred();
    const h = await documents(async (path, method) => path === "/reviews" && method === "POST" ? post.promise : docsRequest()(path), { space: source, spaces: [source] });
    await choose(h); h.click("送审当前所选版本");
    assert.ok(h.nodes().some(n => n.props.role === "dialog")); assert.match(h.text(), /不会获得源知识库|全体项目成员可读/);
    const confirm = h.button("确认提交审核"); confirm.props.onClick(); confirm.props.onClick(); await h.settle();
    assert.deepEqual(h.calls.filter(([path]) => path === "/reviews"), [["/reviews", "POST", { version_id: v2.id }]]);
    assert.ok(!h.calls.some(([path]) => path.endsWith("/share")));
    assert.equal(h.button("确认提交审核").props.disabled, true);
    post.resolve(receipt); await h.settle(); assert.ok(!h.nodes().some(n => n.props.role === "dialog"));
  }
});

test("basis/results/read-only completely hide forbidden edits, submission, deletion and task selects", async () => {
  for (const source of [{ ...space, kind: "basis" }, { ...space, kind: "results" }, { ...space, role: "reader", owner_id: "other" }]) {
    const h = await documents(docsRequest(), { space: source }); await choose(h);
    assert.equal(h.hasButton("送审当前所选版本"), false); assert.equal(h.hasButton("删除知识库"), false);
    if (source.kind === "results" || source.role === "reader") for (const label of ["＋ 录入知识", "新增版本", "删除版本", "撤回整份知识"]) assert.equal(h.hasButton(label), false);
    assert.ok(!h.nodes().some(n => n.props["aria-label"] === "送审关联任务"));
    assert.ok(!h.calls.some(([path]) => path === "/tasks"));
  }
});

test("inactive metadata selection never fetches version body or offers active actions", async () => {
  const inactive = { ...v1, lifecycle_status: "deleted" };
  const h = await documents(docsRequest([{ ...document, versions: [v2, inactive] }])); await choose(h);
  h.change("select", "知识版本", inactive.id); await h.settle();
  assert.equal(h.calls.filter(([path]) => path === `/versions/${inactive.id}`).length, 0);
  assert.match(h.text(), /此版本仅保留历史状态/); assert.doesNotMatch(h.text(), /当前正文/);
  assert.equal(h.hasButton("删除版本"), false); assert.equal(h.hasButton("送审当前所选版本"), false);
});

test("ordinary old version delete marks via DELETE; current version requires explicit replacement", async () => {
  const h = await documents(); await choose(h); h.click("删除版本");
  assert.equal(h.button("确认删除版本").props.disabled, true);
  h.change("select", "替换当前版本", v1.id); h.click("确认删除版本"); await h.settle();
  assert.ok(h.calls.some(([path, method, body]) => path === `/versions/${v2.id}` && method === "DELETE" && body.replacement_version_id === v1.id));
  await choose(h); h.change("select", "知识版本", v1.id); await h.settle(); h.click("删除版本"); h.click("确认删除版本"); await h.settle();
  assert.ok(h.calls.some(([path, method, body]) => path === `/versions/${v1.id}` && method === "DELETE" && !body.replacement_version_id));
});

test("protected old version uses withdraw reason; protected current cannot individually withdraw", async () => {
  const protectedDoc = { ...document, active_version: { ...v2, protected: true }, latest_version: { ...v2, protected: true }, versions: [{ ...v2, protected: true }, { ...v1, protected: true }] };
  const h = await documents(async path => path.startsWith("/versions/") ? { ...await docsRequest()(path), protected: true } : docsRequest([protectedDoc])(path)); await choose(h); h.click("撤回版本");
  assert.equal(h.button("确认撤回版本").props.disabled, true); assert.match(h.text(), /先新增版本/);
  h.click("取消"); h.change("select", "知识版本", v1.id); await h.settle(); h.click("撤回版本");
  assert.equal(h.button("确认撤回版本").props.disabled, true); h.change("textarea", "版本处理原因", "数据范围需修订"); h.click("确认撤回版本"); await h.settle();
  assert.ok(h.calls.some(([path, method, body]) => path === `/versions/${v1.id}/withdraw` && method === "POST" && body.reason === "数据范围需修订"));
  assert.ok(!h.calls.some(([path, method]) => path === `/versions/${v1.id}` && method === "DELETE"));
});

test("protection conflict never retries DELETE or auto-withdraws, explains explicit refresh/withdraw", async () => {
  let h;
  h = await documents(async (path, method) => { if (method === "DELETE") throw new h.ApiError(409, "Version protected"); return docsRequest()(path); });
  await choose(h); h.change("select", "知识版本", v1.id); await h.settle(); h.click("删除版本"); h.click("确认删除版本"); await h.settle();
  assert.match(h.feedback().map(f => f.error).join(""), /刷新版本状态后使用撤回/);
  assert.equal(h.calls.filter(([, method]) => method === "DELETE").length, 1); assert.ok(!h.calls.some(([path]) => path.endsWith("/withdraw")));
});

test("only collaboration true owner can delete library; editors cannot delete others knowledge", async () => {
  for (const source of [privateSpace, { ...space, kind: "basis" }, { ...space, kind: "results" }, { ...space, role: "editor" }, { ...space, role: "reader" }, { ...space, owner_id: "other" }]) {
    const h = await documents(docsRequest([{ ...document, created_by: "other" }]), { space: source, user: owner }); await choose(h);
    assert.equal(h.hasButton("删除知识库"), false);
    if (source.role === "editor" || source.role === "reader" || source.owner_id === "other") { assert.equal(h.hasButton("删除版本"), false); assert.equal(h.hasButton("撤回整份知识"), source.kind === "results"); }
  }
  let deleted = 0;
  const h = await documents(docsRequest(), { onSpaceDeleted: async () => { deleted++; } });
  h.click("删除知识库"); assert.match(h.text(), /1 份知识|全部已授权成员|不会级联删除/); assert.equal(h.calls.some(([, method]) => method === "DELETE"), false);
  h.click("确认删除知识库"); await h.settle(); assert.equal(deleted, 1); assert.ok(h.calls.some(([path, method]) => path === "/spaces/space-a" && method === "DELETE"));
});

test("version selection races, inactive selection and permission changes never retain stale bodies", async () => {
  const slow = deferred(), fast = deferred();
  const h = await documents(async path => path.endsWith("/documents") ? [document] : path === "/goals" ? [] : path.endsWith(v2.id) ? slow.promise : fast.promise);
  const item = h.nodes().find(n => n.props.className?.includes("rw-doc-item")); item.props.onClick(); h.render();
  h.change("select", "知识版本", v1.id); fast.resolve({ ...v1, content: "正确旧版正文" }); await h.settle();
  slow.resolve({ ...v2, content: "不该回填的当前正文" }); await h.settle(); assert.match(h.text(), /正确旧版正文/); assert.doesNotMatch(h.text(), /不该回填/);
  h.render({ space: { ...space, epoch: 2, role: "reader" }, spaces: [space], user: member });
  assert.doesNotMatch(h.text(), /正确旧版正文/); await h.settle(); assert.doesNotMatch(h.text(), /正确旧版正文/);
});

test("A-B-A navigation and stale version errors cannot refill another library", async () => {
  const late = deferred();
  const h = await documents(async path => path.endsWith("/documents") ? [document] : path === "/goals" ? [] : late.promise);
  const item = h.nodes().find(n => n.props.className?.includes("rw-doc-item")); item.props.onClick(); h.render();
  h.render({ space: privateSpace, spaces: [privateSpace], user: member }); h.render({ space, spaces: [space], user: member }); await h.settle();
  late.resolve({ ...v2, content: "第一轮A陈旧正文" }); await h.settle(); assert.doesNotMatch(h.text(), /第一轮A陈旧正文/);
});

test("reviews read only independent snapshot GET, never original versions or source space navigation", async () => {
  const h = await reviews(); await choose(h);
  assert.match(h.text(), /独立快照正文|全体项目成员可读/);
  assert.deepEqual(h.calls.filter(([path]) => path.includes("snapshot")), [["/reviews/review-a/snapshot"]]);
  assert.ok(!h.calls.some(([path]) => path.startsWith("/versions/") || path.startsWith("/spaces/")));
  assert.ok(!h.nodes().some(n => n.type === "a" && /源库|进入/.test(n.props.children)));
  assert.equal(h.hasButton("进入源知识库"), false);
});

test("members see 我的提交 and never review buttons or textarea; selfowner/lineage forbidden stays hidden", async () => {
  for (const [user, item] of [[member, receipt], [owner, { ...receipt, submitted_by: owner.id, can_review: true }], [owner, { ...receipt, can_review: false }]]) {
    const h = await reviews(async path => path === "/reviews" ? [item] : { ...item, content: "快照正文" }, user); await choose(h);
    assert.ok(!h.nodes().some(n => n.type === "textarea")); assert.equal(h.hasButton("通过并发布"), false); assert.equal(h.hasButton("退回补充"), false);
    if (user.role !== "owner") assert.equal(h.nodes().find(n => n.type === "PanelHeading").props.title, "我的提交");
  }
});

test("owner decisions preserve HTTP approved/rejected schema and reject requires comment", async () => {
  const original = globalThis.window; const confirms = [];
  globalThis.window = { confirm(text) { confirms.push(text); return true; } };
  try {
    const h = await reviews(); await choose(h); h.click("退回补充"); await h.settle(); assert.ok(!h.calls.some(([, method]) => method === "POST"));
    h.change("textarea", "review-comment", "请核对样本范围"); h.click("退回补充"); await h.settle();
    assert.ok(h.calls.some(([path, method, body]) => path === "/reviews/review-a/decision" && method === "POST" && body.decision === "rejected" && body.comment === "请核对样本范围"));
    h.click("通过并发布"); await h.settle(); assert.ok(h.calls.some(([, method, body]) => method === "POST" && body.decision === "approved"));
    assert.match(confirms.join(""), /全体项目成员可读/);
  } finally { if (original === undefined) delete globalThis.window; else globalThis.window = original; }
});

test("review snapshot async race and mismatched response never show another receipt's body", async () => {
  const a = deferred(), b = deferred(); const other = { ...receipt, id: "review-b", version_id: "other-version", title: "第二份提交" };
  const h = await reviews(async path => path === "/reviews" ? [receipt, other] : path.includes("review-a") ? a.promise : b.promise);
  const buttons = h.nodes().filter(n => n.type === "button" && n.props.className?.includes("rw-doc-item")); buttons[0].props.onClick(); h.render(); buttons[1].props.onClick(); h.render();
  b.resolve({ ...other, content: "B正确快照" }); await h.settle(); a.resolve({ ...receipt, content: "A迟到快照" }); await h.settle();
  assert.match(h.text(), /B正确快照/); assert.doesNotMatch(h.text(), /A迟到快照/);
  const wrong = await reviews(async path => path === "/reviews" ? [receipt] : { ...receipt, id: "wrong", content: "错误正文" }); await choose(wrong);
  assert.doesNotMatch(wrong.text(), /错误正文/); assert.match(wrong.feedback().map(f => f.error).join(""), /不一致/);
});

test("submission refresh updates selected version protection and goal association is exact", async () => {
  let submitted = false;
  const h = await documents(async (path, method) => {
    if (path === "/reviews" && method === "POST") { submitted = true; return receipt; }
    if (path === "/goals") return [{ id: "goal-one", title: "核对范围", path: "实验验证 / 核对范围" }, { id: "goal-other", title: "评价指标", path: "实验验证 / 评价指标" }];
    if (path.endsWith("/documents")) return [{ ...document, versions: [{ ...v2, protected: submitted }, v1], active_version: { ...v2, protected: submitted } }];
    return docsRequest()(path);
  });
  await choose(h); h.click("送审当前所选版本");
  assert.ok(h.nodes().some(n => n.type === "option" && n.props.value === "goal-other"));
  h.change("select", "送审关联目标", "goal-one"); h.click("确认提交审核"); await h.settle();
  assert.ok(h.calls.some(([path, method, body]) => path === "/reviews" && method === "POST" && body.goal_id === "goal-one" && !("task_id" in body)));
  assert.equal(h.hasButton("撤回版本"), true); assert.equal(h.hasButton("删除版本"), false);
});

test("owner cannot decide without explicit server capability or complete snapshot content", async () => {
  for (const patch of [{ can_review: undefined }, { content: undefined }]) {
    const item = { ...receipt, ...patch };
    const h = await reviews(async path => path === "/reviews" ? [item] : { ...item, content: "content" in patch ? patch.content : "快照" }); await choose(h);
    assert.equal(h.hasButton("通过并发布"), false); assert.ok(!h.nodes().some(n => n.type === "textarea"));
  }
});

test("workspace delete callback removes current library immediately and refreshes canonical spaces", async () => {
  let removed = false;
  const h = harness("ResearchApp.tsx", async path => path === "/auth/me" ? { user: member } : path === "/spaces" ? removed ? [privateSpace] : [space, privateSpace] : []);
  h.render({}); await h.settle(); await h.settle();
  h.nodes().find(n => n.type === "Chat").props.onOpenKnowledge(); h.render();
  const docs = h.nodes().find(n => n.type === "Documents"); assert.equal(typeof docs.props.onSpaceDeleted, "function");
  removed = true; await docs.props.onSpaceDeleted(); h.render();
  assert.equal(h.nodes().find(n => n.type === "Documents").props.space.id, privateSpace.id);
  assert.equal(h.nodes().find(n => n.type === "Documents").props.spaces.some(s => s.id === space.id), false);
  assert.ok(h.calls.filter(([path]) => path === "/spaces").length >= 2);
});
