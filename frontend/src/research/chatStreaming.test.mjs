import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import ts from "typescript";

const directory = dirname(fileURLToPath(import.meta.url)), require = createRequire(import.meta.url);
const space = { id: "s", name: "研究库", kind: "collaboration", role: "editor", owner_id: "u" };
const other = { ...space, id: "other", name: "另一库" };
const identity = { run_id: "r", message_id: "message-r", space_id: "s", conversation_id: "c", request_id: "request" };
const event = (seq, type, data = {}, extra = {}) => ({ ...identity, seq, timestamp: "2026-10-26T12:34:56Z", type, data: { message_id: identity.message_id, ...data }, ...extra });
const frame = value => `id: ${value.seq}\r\nevent: ${value.type}\r\ndata: ${JSON.stringify(value)}\r\n\r\n`;
const encode = value => new TextEncoder().encode(value);
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; };
function channel() {
  let controller, closed = false;
  const body = new ReadableStream({ start(value) { controller = value; }, cancel() { closed = true; } });
  return { response: { ok: true, status: 200, headers: new Headers({ "Content-Type": "text/event-stream; charset=utf-8" }), body },
    push(value) { if (!closed) controller.enqueue(typeof value === "string" ? encode(value) : value); },
    close() { if (!closed) { closed = true; controller.close(); } },
    bind(signal) { signal?.addEventListener("abort", () => { if (!closed) { closed = true; controller.error(new DOMException("Aborted", "AbortError")); } }, { once: true }); } };
}
function modules(request = async () => [], fetcher = async () => { throw new Error("unexpected fetch"); }, react) {
  const cache = new Map(), calls = [], fetches = [];
  const api = { api: async (...args) => { calls.push(args); return request(...args); }, idPath: encodeURIComponent, errorText: e => e.message, listOf: (v, key) => Array.isArray(v) ? v : v[key], trackEvent() {} };
  const jsx = (type, props, key) => ({ type, props: props ?? {}, key });
  function load(filename) {
    const file = resolve(directory, filename);
    if (cache.has(file)) return cache.get(file).exports;
    const module = { exports: {} }; cache.set(file, module);
    const output = ts.transpileModule(readFileSync(file, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    const localRequire = name => name === "react" ? react : name === "react/jsx-runtime" ? { jsx, jsxs: jsx, Fragment: "Fragment" } : name === "./api" ? api : ["./stream", "./types"].includes(name) ? load(`${name.slice(2)}.ts`) : name === "./ui" ? { Feedback: "Feedback", Empty: "Empty", JsonDetails: "JsonDetails" } : name.startsWith(".") ? { default: name.includes("MarkdownMessage") ? "MarkdownMessage" : name.slice(2) } : require(name);
    new Function("require", "module", "exports", "fetch", output)(localRequire, module, module.exports, async (...args) => { fetches.push(args); return fetcher(...args); });
    return module.exports;
  }
  return { load, calls, fetches };
}
function harness(request, fetcher) {
  const state = [], refs = [], effects = []; let si = 0, ri = 0, ei = 0, pending = [], tree, props;
  const react = {
    useState(initial) { const i = si++; if (!(i in state)) state[i] = initial; return [state[i], value => { state[i] = typeof value === "function" ? value(state[i]) : value; }]; },
    useRef(initial) { const i = ri++; return refs[i] ??= { current: initial }; },
    useEffect(fn, dependencies) { const i = ei++, previous = effects[i]; if (!previous || dependencies.some((v, j) => !Object.is(v, previous.dependencies[j]))) pending.push(() => { previous?.cleanup?.(); effects[i] = { dependencies, cleanup: fn() }; }); },
  };
  const m = modules(request, fetcher, react), Chat = m.load("Chat.tsx").default;
  const render = (next = props) => { props = next; si = ri = ei = 0; tree = Chat(props); const jobs = pending; pending = []; jobs.forEach(fn => fn()); };
  const nodes = () => { const result = []; const visit = node => { if (Array.isArray(node)) node.forEach(visit); else if (node && typeof node === "object") { result.push(node); visit(node.props?.children); } }; visit(tree); return result; };
  const textOf = node => Array.isArray(node) ? node.map(textOf).join("") : node && typeof node === "object" ? node.type === "MarkdownMessage" ? node.props.text : textOf(node.props?.children) : node == null || node === false ? "" : String(node);
  const button = label => { const value = nodes().find(node => node.type === "button" && textOf(node) === label); assert.ok(value, `missing button ${label}`); return value; };
  return { ...m, render, nodes, button, text: () => textOf(tree), feedback: () => nodes().find(n => n.type === "Feedback").props,
    async settle() { await tick(); render(); }, unmount() { effects.forEach(effect => effect.cleanup?.()); },
    change(value) { nodes().find(n => n.type === "textarea").props.onChange({ target: { value } }); render(); },
    submit() { nodes().find(n => n.type === "form").props.onSubmit({ preventDefault() {} }); } };
}
async function chat(options = {}) {
  const wire = channel(); let requestId;
  const h = harness(async (path, method, body) => {
    if (path.includes("?")) return [];
    if (path === "/spaces/s/documents") return typeof options.documents === "function" ? options.documents() : options.documents ?? [];
    if (path === "/conversations" && method === "POST") return { id: "c" };
    if (path === "/chat/runs") { requestId = body.request_id; return { ...body, run_id: "r", message_id: "message-r", status: "queued" }; }
    if (path.endsWith("/cancel")) return { status: "cancelled" };
    if (path === "/conversations/c") return options.history?.() ?? { id: "c", space_id: "s", messages: [{ id: "message-r", role: "assistant", content: "最终保存回答", citations: [] }] };
    return [];
  }, async (path, init) => { wire.bind(init.signal); return options.fetcher ? options.fetcher(path, init, wire) : wire.response; });
  h.render({ space, spaces: [space, other], onSelectSpace() {}, ...options.props }); await h.settle();
  return { h, wire, emit: (seq, type, data = {}) => wire.push(frame(event(seq, type, data, { request_id: requestId }))) };
}

const materialDocs = [
  { id: "d1", title: "实验记录", active_version: { id: "v2", number: 2 }, versions: [{ id: "v1", number: 1 }, { id: "v2", number: 2 }, { id: "gone", number: 0, lifecycle_status: "deleted" }] },
  { id: "d2", title: "方案", active_version: { id: "v3", number: 1 }, versions: [{ id: "v3", number: 1 }, { id: "withdrawn", lifecycle_status: "withdrawn" }] },
];
const materialPicker = h => h.nodes().find(node => node.type === "select" && node.props.id === "rw-material");
test("multiple documents never select a first version; optional body stays omitted", async () => {
  const { h } = await chat({ documents: materialDocs });
  assert.equal(materialPicker(h).props.value, ""); assert.ok(!h.nodes().some(n => n.props["aria-label"] === "当前材料"));
  assert.deepEqual(h.nodes().filter(n => n.type === "option" && n.props.value.startsWith("v")).map(n => n.props.value), ["v1", "v2", "v3"]);
  h.change("通用问题"); h.submit(); await h.settle();
  assert.equal(Object.hasOwn(h.calls.find(([p]) => p === "/chat/runs")[2], "selected_version_id"), false); h.unmount();
});

test("explicit historical material is badged, sent exactly once and locked during run", async () => {
  const changes = []; const { h } = await chat({ documents: materialDocs, props: { onSelectVersion: value => changes.push(value) } });
  materialPicker(h).props.onChange({ target: { value: "v1" } }); h.render();
  assert.match(h.text(), /当前材料：《实验记录》 · v1 · 历史版本/); assert.deepEqual(changes, ["v1"]);
  h.change("总结所选版本"); h.submit(); await h.settle();
  assert.equal(h.calls.find(([p]) => p === "/chat/runs")[2].selected_version_id, "v1");
  assert.equal(materialPicker(h).props.disabled, true); assert.equal(h.nodes().find(n => n.props["aria-label"] === "清除当前材料").props.disabled, true); h.unmount();
});

test("clear badge and invalidated refreshed version never silently switch to another material", async () => {
  let docs = materialDocs; const { h } = await chat({ documents: () => docs, props: { initialSelectedVersionId: "v2" } });
  assert.equal(materialPicker(h).props.value, "v2");
  h.nodes().find(n => n.props["aria-label"] === "清除当前材料").props.onClick(); h.render();
  assert.equal(materialPicker(h).props.value, "");
  materialPicker(h).props.onChange({ target: { value: "v1" } }); h.render();
  docs = [materialDocs[1]]; h.button("刷新材料").props.onClick(); h.render(); await h.settle();
  assert.equal(materialPicker(h).props.value, ""); assert.match(h.text(), /不会自动改用其他版本/); h.unmount();
});

test("progress mode visibly suspends selected material and omits its request context", async () => {
  const { h } = await chat({ documents: materialDocs, props: { initialSelectedVersionId: "v2" } });
  const mode = h.nodes().find(n => n.type === "select" && n.props.id !== "rw-material");
  mode.props.onChange({ target: { value: "progress" } }); h.render();
  assert.match(h.text(), /已选材料（进展模式不使用）/); assert.equal(materialPicker(h).props.disabled, true);
  h.change("进展如何"); h.submit(); await h.settle();
  const body = h.calls.find(([p]) => p === "/chat/runs")[2]; assert.equal(body.mode, "progress"); assert.equal(Object.hasOwn(body, "selected_version_id"), false); h.unmount();
});

test("document read stage displays generic progress without trusting event titles", async () => {
  const { h, emit } = await chat({ documents: materialDocs, props: { initialSelectedVersionId: "v2" } });
  h.change("总结材料"); h.submit(); await h.settle();
  emit(1, "document.read", { title: "untrusted-secret-title", content: "untrusted-document-body", max_chars: 5000 }); await h.settle();
  assert.match(h.text(), /正在读取授权材料版本/); assert.doesNotMatch(h.text(), /untrusted-secret-title|untrusted-document-body/);
  emit(2, "document.read", { document_title: "授权记录标题", version_id: "v2" }); await h.settle(); assert.match(h.text(), /《授权记录标题》/); h.unmount();
});

test("document-to-chat initial selection is verified and unavailable selection is cleared", async () => {
  for (const [versionId, expected] of [["v2", "v2"], ["gone", ""]]) {
    const { h } = await chat({ documents: materialDocs, props: { initialSelectedVersionId: versionId } });
    assert.equal(materialPicker(h).props.value, expected); h.unmount();
  }
});

test("empty missing-evidence list does not render an empty warning panel", async () => {
  const { h, emit } = await chat({ history: () => ({ id: "c", space_id: "s", messages: [{ id: "message-r", role: "assistant", content: "最终保存回答", missing_evidence: [] }] }) });
  h.change("问题"); h.submit(); await h.settle(); emit(1, "answer.final", { answer: "最终保存回答", conversation_id: "c", missing_evidence: [] }); await h.settle();
  assert.ok(!h.nodes().some(n => n.props.className === "rw-chat-missing")); h.unmount();
});

test("Chat reconnect retains verified prefix while resuming cursor and displays only allowlisted real stage data", async () => {
  let requests = 0; const second = channel();
  const { h, emit, wire } = await chat({ fetcher: () => ++requests === 1 ? wire.response : second.response });
  h.change("问题"); h.submit(); await h.settle(); emit(1, "retrieval.degraded", { reason: "embedding_failed", strategy: "lexical", hit_count: 2, debug: "provider secret" }); emit(2, "rerank.skipped", { reason: "not_configured", candidate_count: 2 }); emit(3, "answer.delta", { text: "已验前缀" }); await h.settle(); wire.close(); await h.settle();
  assert.match(h.text(), /已验前缀/); assert.match(h.feedback().notice, /保留已校验片段/); assert.match(h.text(), /关键词检索.*命中 2.*向量编码不可用/); assert.match(h.text(), /重排已跳过.*未配置/); assert.doesNotMatch(h.text(), /provider secret/);
  second.push(frame(event(3, "answer.delta", { text: "重复" }, { request_id: h.calls.find(([p]) => p === "/chat/runs")[2].request_id })));
  second.push(frame(event(4, "answer.delta", { text: "及后缀" }, { request_id: h.calls.find(([p]) => p === "/chat/runs")[2].request_id }))); await h.settle();
  assert.equal(h.nodes().filter(n => n.type === "MarkdownMessage" && n.props.text === "已验前缀及后缀").length, 1); assert.match(h.fetches[1][0], /after_seq=3$/); h.unmount();
});

test("purged terminal may jump sequence and user identity changes clear same-space text immediately", async () => {
  const wire = channel(); wire.push(frame(event(9, "run.cancelled", { replace: true, answer: "" }))); const m = modules(undefined, async () => wire.response); const seen = [];
  const terminal = await m.load("stream.ts").watchChatRun(identity, new AbortController().signal, value => seen.push(value)); assert.equal(terminal.type, "run.cancelled"); assert.equal(seen.length, 1);
  const { h, emit } = await chat(); h.change("旧身份问题"); h.submit(); await h.settle(); emit(1, "answer.delta", { text: "旧身份正文" }); await h.settle(); h.render({ space, userId: "new-user" }); assert.doesNotMatch(h.text(), /旧身份正文/); assert.equal(h.nodes().find(n => n.type === "textarea").props.value, ""); await h.settle(); h.unmount();
});

test("stable message id selects this answer even if another saved answer is last and citations replace without duplication", async () => {
  const citation = { version_id: "v", char_start: 1, char_end: 3, excerpt: "片段1" }, another = { version_id: "v", char_start: 5, char_end: 7, excerpt: "片段2" };
  const { h, emit } = await chat({ history: () => ({ id: "c", space_id: "s", messages: [{ id: "message-r", role: "assistant", content: "最终保存回答", citations: [{ version_id: "v" }] }, { id: "message-r", role: "assistant", content: "最终保存回答" }, { id: "other-answer", role: "assistant", content: "另一条回答" }] }) });
  h.change("问题"); h.submit(); await h.settle(); emit(1, "answer.final", { conversation_id: "c", answer: "最终保存回答", citations: [citation, { ...citation }, another], replace: true }); await h.settle();
  assert.equal(h.nodes().filter(n => n.type === "MarkdownMessage" && n.props.text === "最终保存回答").length, 1); assert.match(h.text(), /已保存/); assert.equal(h.feedback().notice, ""); assert.deepEqual(h.nodes().find(n => n.type === "Citations").props.items, [citation, another]);
});

test("message-id mismatch clears current scope contents; a sequence gap cannot silently truncate preview", async () => {
  const { h, emit } = await chat(); h.change("私密输入"); h.submit(); await h.settle(); emit(1, "answer.delta", { text: "私密片段" }); await h.settle(); emit(2, "answer.delta", { text: "错误消息", message_id: "evil" }); await h.settle();
  assert.doesNotMatch(h.text(), /私密片段|错误消息/); assert.equal(h.nodes().find(n => n.type === "textarea").props.value, ""); assert.match(h.feedback().error, /权限已失效/);
  const wire = channel(); wire.push(frame(event(2, "answer.delta", { text: "缺前缀" }))); const m = modules(undefined, async () => wire.response); await assert.rejects(m.load("stream.ts").watchChatRun(identity, new AbortController().signal, () => {}), /序列有缺口/); assert.equal(m.fetches.length, 1);
});

test("actual SSE decoder handles one-byte UTF-8, CRLF, multi-line JSON and discards incomplete frame", () => {
  const { sseDecoder } = modules().load("stream.ts"), values = [], parser = sseDecoder(value => values.push(value));
  const bytes = encode(': heartbeat\r\ndata: {"text":\r\ndata: "知识🌱"}\r\n\r\ndata: {"partial":true}');
  for (const byte of bytes) parser.push(Uint8Array.of(byte));
  parser.push(new Uint8Array(), true); assert.deepEqual(values, [{ text: "知识🌱" }]);
});

test("bounded frame rejects huge JSON/comment buffers and malformed UTF-8", () => {
  const { sseDecoder, MAX_SSE_FRAME } = modules().load("stream.ts");
  assert.throws(() => sseDecoder(() => {}).push(encode(":" + "x".repeat(MAX_SSE_FRAME))), /过大/);
  assert.throws(() => sseDecoder(() => {}).push(Uint8Array.of(0xff)));
  assert.throws(() => sseDecoder(() => {}).push(encode("data: nope\n\n")), /JSON/);
});

test("real fetch GET uses cookies, Accept, abort signal; reconnect retains cursor and deduplicates seq", async () => {
  const first = channel(), second = channel(), seen = [], reconnects = [];
  first.push(frame(event(1, "run.started"))); first.push(frame(event(2, "answer.delta", { text: "已校验" }))); first.close();
  second.push(frame(event(2, "answer.delta", { text: "重复" }))); second.push(frame(event(3, "answer.final", { answer: "终稿", conversation_id: "c" }))); second.close();
  let requests = 0;
  const m = modules(undefined, async () => (++requests === 1 ? first : second).response), abort = new AbortController();
  const result = await m.load("stream.ts").watchChatRun(identity, abort.signal, value => seen.push(value), value => reconnects.push(value));
  assert.deepEqual(seen.map(v => v.seq), [1, 2, 3]); assert.equal(result.type, "answer.final"); assert.deepEqual(reconnects, [1]);
  assert.match(m.fetches[0][0], /after_seq=0$/); assert.match(m.fetches[1][0], /after_seq=2$/);
  assert.equal(m.fetches[0][1].credentials, "include"); assert.equal(m.fetches[0][1].headers.Accept, "text/event-stream"); assert.equal(m.fetches[0][1].signal, abort.signal);
});

test("stream rejects cross-scope/request events and caps reconnect, never POSTs", async () => {
  for (const extra of [{ run_id: "evil" }, { space_id: "other" }, { request_id: "other-request" }]) {
    const wire = channel(); wire.push(frame(event(1, "answer.delta", { text: "secret" }, extra))); wire.close();
    const m = modules(undefined, async () => wire.response), seen = [];
    await assert.rejects(m.load("stream.ts").watchChatRun(identity, new AbortController().signal, value => seen.push(value)), /作用域/); assert.equal(seen.length, 0);
  }
  const m = modules(undefined, async () => { const wire = channel(); wire.close(); return wire.response; });
  await assert.rejects(m.load("stream.ts").watchChatRun(identity, new AbortController().signal, () => {}), /中断/);
  assert.equal(m.fetches.length, 3); assert.equal(m.calls.length, 0);
});

test("actual Chat shows timestamped progress and tentative deltas once; final GET replaces rather than duplicates", async () => {
  const saved = deferred(); const { h, emit } = await chat({ history: () => saved.promise });
  h.change("研究问题"); h.submit(); h.submit(); await h.settle();
  assert.equal(h.calls.filter(([p]) => p === "/chat/runs").length, 1);
  const start = h.calls.find(([p]) => p === "/chat/runs")[2]; assert.match(start.request_id, /^[0-9a-f-]{36}$/); assert.equal(start.space_id, "s");
  assert.ok(h.nodes().filter(n => n.props.className?.includes("rw-chat-library")).every(n => n.props.disabled));
  emit(1, "retrieval.started"); emit(2, "agent.started", { agent: "KnowledgeQAAgent", url: "https://evil.invalid", action: "run_tool" }); emit(3, "answer.delta", { text: "临时中文🌱", tentative: true }); emit(3, "answer.delta", { text: "重复" }); await h.settle();
  assert.match(h.text(), /正在检索授权资料/); assert.match(h.text(), /基于知识库问答/); assert.match(h.text(), /临时回答 · 尚未保存/); assert.doesNotMatch(h.text(), /重复|evil.invalid|run_tool/);
  assert.equal(h.nodes().filter(n => n.type === "MarkdownMessage" && n.props.text === "临时中文🌱").length, 1);
  assert.equal(h.nodes().find(n => n.type === "time").props.dateTime, "2026-10-26T12:34:56Z");
  emit(4, "answer.final", { answer: "最终保存回答", conversation_id: "c", replace: true }); await h.settle(); assert.doesNotMatch(h.text(), /临时中文/);
  saved.resolve({ id: "c", space_id: "s", messages: [{ id: "message-r", role: "assistant", content: "最终保存回答", citations: [] }] }); await h.settle();
  assert.equal(h.nodes().filter(n => n.type === "MarkdownMessage" && n.props.text === "最终保存回答").length, 1); assert.match(h.text(), /已保存/); assert.equal(h.feedback().notice, "");
  assert.equal(h.calls.filter(([p]) => p === "/chat/runs").length, 1);
});

test("no-delta local extraction honestly shows only progress then canonical fallback", async () => {
  const { h, emit } = await chat({ history: () => ({ id: "c", space_id: "s", messages: [{ id: "message-r", role: "assistant", content: "本地原文", citations: [], fallback: true }] }) });
  h.change("问题"); h.submit(); await h.settle(); emit(1, "run.started"); await h.settle();
  assert.ok(!h.nodes().some(n => n.props.className === "rw-run-tentative")); assert.match(h.text(), /本地摘录不伪装/);
  emit(2, "answer.final", { answer: "本地原文", conversation_id: "c", fallback: true }); await h.settle();
  assert.match(h.text(), /原文摘录 · 非 AI 总结/); assert.match(h.text(), /非模型流式生成/); assert.equal(h.feedback().notice, "");
  assert.equal(h.nodes().filter(n => n.type === "MarkdownMessage" && n.props.text === "本地原文").length, 1);
});

test("stop uses cancel POST {}, removes preview, and never treats a partial as saved", async () => {
  const { h, emit } = await chat(); h.change("问题"); h.submit(); await h.settle();
  emit(1, "answer.delta", { text: "未完成片段" }); await h.settle(); h.button("停止回答").props.onClick(); await h.settle();
  assert.doesNotMatch(h.text(), /未完成片段/); assert.ok(h.calls.some(([p, method, body]) => p === "/chat/runs/r/cancel" && method === "POST" && JSON.stringify(body) === "{}"));
  assert.match(h.feedback().notice, /临时回答未作为完成答案/); assert.ok(!h.calls.some(([p]) => p === "/conversations/c")); assert.ok(h.fetches[0][1].signal.aborted);
});

test("run.completed persists without accessible body, clears scope and never fetches or resends", async () => {
  const { h, emit } = await chat(); h.change("问题"); h.submit(); await h.settle(); emit(1, "answer.delta", { text: "撤权前片段" }); await h.settle(); emit(9, "run.completed", { replace: true, answer: "", persisted: true, reason: "access_changed" }); await h.settle();
  assert.doesNotMatch(h.text(), /撤权前片段/); assert.match(h.feedback().notice, /回答已保存；请重新验证权限并查看历史/); assert.equal(h.feedback().error, ""); assert.equal(h.calls.filter(([p]) => p === "/chat/runs").length, 1); assert.ok(!h.calls.some(([p]) => p === "/conversations/c"));
});

test("run.failed and run.cancelled clear tentative answer and never trigger final GET", async () => {
  for (const kind of ["run.failed", "run.cancelled"]) {
    const { h, emit } = await chat(); h.change("问题"); h.submit(); await h.settle(); emit(1, "answer.delta", { text: "半答" }); await h.settle();
    emit(2, kind, { replace: true, answer: "", reason: "run_unavailable" }); await h.settle();
    assert.doesNotMatch(h.text(), /半答/); assert.match(h.feedback().notice, /未完成|未作为完成/); assert.ok(!h.calls.some(([p]) => p === "/conversations/c"));
  }
});

test("401/403/404 on reconnect clears preview and visible history without replaying revoked text", async () => {
  for (const status of [401, 403, 404]) {
    let requests = 0;
    const { h, emit, wire } = await chat({ fetcher: (_p, _init, wire) => ++requests === 1 ? wire.response : { ok: false, status } });
    h.change("私密问题"); h.submit(); await h.settle(); emit(1, "answer.delta", { text: "私密临时正文" }); await h.settle(); wire.close(); await h.settle();
    assert.doesNotMatch(h.text(), /私密临时正文/); assert.match(h.feedback().error, status === 404 ? /任务已过期或不可访问/ : /权限已失效/);
    assert.equal(h.nodes().find(n => n.type === "textarea").props.value, ""); assert.equal(h.fetches.length, 2); assert.ok(!h.calls.some(([p]) => p === "/conversations/c"));
  }
});

test("scope reset and unmount abort stream and best-effort cancel; stale final never loads prior history", async () => {
  const { h, emit } = await chat(); h.change("旧问题"); h.submit(); await h.settle(); emit(1, "answer.delta", { text: "旧库临时" }); await h.settle();
  h.render({ space: other }); h.render(); await h.settle(); assert.doesNotMatch(h.text(), /旧库临时|旧问题/);
  assert.ok(h.fetches[0][1].signal.aborted); assert.ok(h.calls.some(([p]) => p.endsWith("/cancel"))); assert.ok(!h.calls.some(([p]) => p === "/conversations/c"));
  const second = await chat(); second.h.change("卸载问题"); second.h.submit(); await second.h.settle(); second.h.unmount(); await tick();
  assert.ok(second.h.fetches[0][1].signal.aborted); assert.ok(second.h.calls.some(([p]) => p.endsWith("/cancel")));
});

test("late start after scope reset or stop cancels returned id without opening an old stream", async () => {
  for (const operation of ["scope", "stop"]) {
    const start = deferred(); let posted;
    const h = harness(async (path, method, body) => {
      if (path.includes("?")) return [];
      if (path === "/conversations" && method === "POST") return { id: "c" };
      if (path === "/chat/runs") { posted = body; return start.promise; }
      return {};
    });
    h.render({ space }); await h.settle(); h.change("启动中问题"); h.submit(); await h.settle();
    if (operation === "scope") { h.render({ space: other }); h.render(); }
    else h.button("停止回答").props.onClick();
    start.resolve({ ...posted, run_id: "late", message_id: "late-message", status: "queued" }); await h.settle();
    assert.equal(h.fetches.length, 0); assert.equal(h.calls.filter(([p]) => p === "/chat/runs/late/cancel").length, 1);
    assert.ok(!h.calls.some(([p]) => p === "/conversations/c"));
  }
});

test("saved history and citations are cleared when canonical final GET loses access", async () => {
  let requestId;
  const wire = channel();
  const h = harness(async (path, method, body) => {
    if (path.includes("?")) return [{ id: "c", title: "私密历史" }];
    if (path === "/chat/runs") { requestId = body.request_id; return { ...body, run_id: "r", message_id: "message-r" }; }
    if (path === "/conversations/c" && method !== "POST") {
      if (!requestId) return { id: "c", space_id: "s", messages: [{ id: "message-r", role: "assistant", content: "已保存私密正文", citations: [{ version_id: "v" }] }] };
      throw Object.assign(new Error("forbidden"), { status: 403 });
    }
    return {};
  }, async (_path, init) => { wire.bind(init.signal); return wire.response; });
  h.render({ space }); await h.settle(); h.button("私密历史当前知识库会话").props.onClick(); await h.settle();
  assert.match(h.text(), /已保存私密正文/); h.change("后续问题"); h.submit(); await h.settle();
  wire.push(frame(event(1, "answer.delta", { text: "新的私密片段" }, { request_id: requestId }))); await h.settle();
  wire.push(frame(event(2, "answer.final", { answer: "终稿", conversation_id: "c" }, { request_id: requestId }))); await h.settle();
  assert.doesNotMatch(h.text(), /已保存私密正文|新的私密片段|私密历史/); assert.ok(!h.nodes().some(n => n.type === "Citations"));
  assert.match(h.feedback().error, /权限已失效/);
});

test("two rounds keep each question before its own process and final answer without duplicates", async () => {
  const wires = [channel(), channel()], requests = [], messages = []; let round = 0;
  const h = harness(async (path, method, body) => {
    if (path.includes("?")) return [];
    if (path === "/conversations" && method === "POST") return { id: "c" };
    if (path === "/chat/runs") { requests.push(body); const n = ++round; return { ...body, run_id: `r${n}`, message_id: `a${n}` }; }
    if (path === "/conversations/c") return { id: "c", space_id: "s", messages: [...messages] };
    return {};
  }, async (_path, init) => { const wire = wires[round - 1]; wire.bind(init.signal); return wire.response; });
  h.render({ space }); await h.settle();
  const emitRound = (seq, type, data = {}) => wires[round - 1].push(frame(event(seq, type, { ...data, message_id: `a${round}` }, { run_id: `r${round}`, request_id: requests[round - 1].request_id })));
  const articleText = node => {
    if (Array.isArray(node)) return node.map(articleText).join("");
    if (!node || typeof node !== "object") return "";
    return node.type === "MarkdownMessage" ? node.props.text : articleText(node.props?.children);
  };
  const layout = () => h.nodes().filter(n => n.type === "article" || n.props.className === "rw-run-progress").map(n => n.type === "article" ? articleText(n) : "process");
  h.change("问题一"); h.submit(); await h.settle(); emitRound(1, "agent.started", { agent: "ResearchCoordinator" }); await h.settle();
  assert.deepEqual(layout(), ["问题一", "process"]);
  messages.push({ id: "u1", role: "user", content: "问题一" }, { id: "a1", role: "assistant", content: "回答一", citations: [] });
  emitRound(2, "answer.final", { answer: "回答一", conversation_id: "c", answer_kind: "general" }); await h.settle();
  assert.deepEqual(layout(), ["问题一", "process", "回答一"]); assert.match(h.text(), /通识回答/);
  h.change("问题二"); h.submit(); await h.settle(); emitRound(1, "retrieval.started"); await h.settle();
  assert.deepEqual(layout(), ["问题一", "process", "回答一", "问题二", "process"]);
  messages.push({ id: "u2", role: "user", content: "问题二" }, { id: "a2", role: "assistant", content: "回答二", answer_kind: "grounded", citations: [{ version_id: "v", excerpt: "来源片段" }] });
  emitRound(2, "answer.final", { answer: "回答二", conversation_id: "c", answer_kind: "grounded" }); await h.settle();
  assert.deepEqual(layout(), ["问题一", "process", "回答一", "问题二", "process", "回答二"]);
  for (const text of ["问题一", "回答一", "问题二", "回答二"]) assert.equal(h.nodes().filter(n => n.type === "MarkdownMessage" && n.props.text === text).length, 1);
  assert.equal(requests.length, 2); assert.ok(requests.every(body => !Object.hasOwn(body, "answer_kind")));
  h.unmount();
});

test("stopping a later round preserves earlier final answer and never promotes cancelled text", async () => {
  const second = channel(); let streams = 0;
  const { h, emit } = await chat({ fetcher: (_path, init, wire) => { const current = ++streams === 1 ? wire : second; current.bind(init.signal); return current.response; }, history: () => ({ id: "c", space_id: "s", messages: [{ id: "old-u", role: "user", content: "已有问题" }, { id: "old-a", role: "assistant", content: "已有完整回答", answer_kind: "general" }, { id: "message-r", role: "assistant", content: "最终保存回答" }] }) });
  h.change("第一问"); h.submit(); await h.settle(); emit(1, "answer.final", { answer: "最终保存回答", conversation_id: "c", answer_kind: "general" }); await h.settle();
  h.change("后续问题"); h.submit(); await h.settle();
  const request = h.calls.filter(([p]) => p === "/chat/runs").at(-1)[2];
  second.push(frame(event(1, "answer.delta", { text: "停止轮临时片段" }, { request_id: request.request_id }))); await h.settle();
  assert.match(h.text(), /停止轮临时片段/); h.button("停止回答").props.onClick(); await h.settle();
  assert.doesNotMatch(h.text(), /停止轮临时片段/); assert.match(h.feedback().notice, /未作为完成答案/);
  assert.equal(h.calls.filter(([p]) => p === "/conversations/c").length, 1);
  assert.match(h.text(), /已有完整回答/); assert.match(h.text(), /最终保存回答/);
  assert.equal(h.nodes().filter(n => n.type === "MarkdownMessage" && n.props.text === "最终保存回答").length, 1);
  h.unmount();
});

test("permission epoch change removes saved per-turn processes and stale pending question synchronously", async () => {
  const { h, emit } = await chat(); h.change("保密问题"); h.submit(); await h.settle(); emit(1, "agent.started", { agent: "ResearchCoordinator" }); emit(2, "answer.final", { answer: "最终保存回答", conversation_id: "c", answer_kind: "general" }); await h.settle();
  assert.match(h.text(), /科研主助手/); assert.match(h.text(), /保密问题/);
  h.render({ space: { ...space, epoch: 1, role: "reader" } });
  assert.doesNotMatch(h.text(), /保密问题|最终保存回答|科研主助手/);
  assert.ok(!h.nodes().some(n => n.props.className === "rw-run-progress"));
  await h.settle(); h.unmount();
});

test("retention miss explicitly requests history refresh, clears preview and never restarts", async () => {
  let requests = 0;
  const { h, emit, wire } = await chat({ fetcher: (_p, _init, wire) => ++requests === 1 ? wire.response : { ok: false, status: 409 } });
  h.change("问题"); h.submit(); await h.settle(); emit(1, "answer.delta", { text: "过期半答" }); await h.settle(); wire.close(); await h.settle();
  assert.doesNotMatch(h.text(), /过期半答/); assert.match(h.feedback().error, /过期.*刷新|实时记录无法重放/); assert.equal(h.calls.filter(([p]) => p === "/chat/runs").length, 1);
});


test("process defaults collapsed with latest stage and degradation retained in summary", async () => {
  const { h, emit } = await chat(); h.change("问题"); h.submit(); await h.settle();
  emit(1, "retrieval.degraded", { reason: "embedding_failed" }); emit(2, "agent.started", { agent: "KnowledgeQAAgent" }); await h.settle();
  const process = h.nodes().find(n => n.props.className === "rw-run-progress");
  assert.equal(process.type, "details"); assert.equal(process.props.open, undefined);
  const summary = JSON.stringify(process.props.children[0]);
  assert.match(summary, /研究处理开始/); assert.match(summary, /检索降级/); assert.match(summary, /向量编码不可用/);
  h.unmount();
});


test("final SSE clarification metadata survives canonical history merge and opens the actual material picker", async () => {
  const body = "你想了解哪份材料？可以点击“＋ 材料”选择文档，也可以告诉我文档名称。";
  const selected = [];
  const { h, emit } = await chat({ documents: materialDocs, props: { onSelectVersion: value => selected.push(value) }, history: () => ({ id: "c", space_id: "s", messages: [{ id: "message-r", role: "assistant", content: body }] }) });
  h.change("介绍一下文档"); h.submit(); await h.settle();
  emit(1, "answer.final", { answer: body, conversation_id: "c", answer_kind: "clarification", clarification_kind: "document", suggested_actions: ["select_material"] }); await h.settle();
  assert.match(h.text(), /请选择要查看的文档/); assert.doesNotMatch(h.text(), /需要澄清|请补充问题中的对象或范围/);
  assert.equal(h.nodes().filter(n => n.type === "MarkdownMessage" && n.props.text === body).length, 1);
  const action = h.button("选择材料"); assert.equal(action.props.type, "button");
  action.props.onClick({ currentTarget: { focus() {} } }); h.render();
  assert.equal(h.nodes().find(n => n.props.id === "rw-composer-options").props.hidden, false);
  assert.equal(materialPicker(h).props.value, ""); assert.deepEqual(selected, []);
  assert.equal(h.calls.filter(([path]) => path === "/chat/runs").length, 1);
  materialPicker(h).props.onChange({ target: { value: "v1" } }); h.render();
  assert.deepEqual(selected, ["v1"]); assert.equal(h.calls.filter(([path]) => path === "/chat/runs").length, 1);
  h.unmount();
});

test("canonical persisted clarification kind takes precedence over final SSE compatibility metadata", async () => {
  const { h, emit } = await chat({ history: () => ({ id: "c", space_id: "s", messages: [{ id: "message-r", role: "assistant", content: "最终保存回答", answer_kind: "clarification", clarification_kind: "scope" }] }) });
  h.change("问题"); h.submit(); await h.settle();
  emit(1, "answer.final", { answer: "最终保存回答", conversation_id: "c", answer_kind: "clarification", clarification_kind: "document" }); await h.settle();
  assert.match(h.text(), /请确认统计范围/); assert.doesNotMatch(h.text(), /请选择要查看的文档/); h.unmount();
});

test("final failure is not announced as a successful local extraction", async () => {
  const { h, emit } = await chat({ history: () => ({ id: "c", space_id: "s", messages: [{ id: "message-r", role: "assistant", content: "服务处理超时，请稍后重试。" }] }) });
  h.change("问题"); h.submit(); await h.settle();
  emit(1, "answer.final", { answer: "服务处理超时，请稍后重试。", conversation_id: "c", answer_kind: "failure", fallback: true, answer_status: "insufficient" }); await h.settle();
  assert.match(h.text(), /本次回答未完成/); assert.match(h.text(), /服务处理超时/);
  assert.match(h.feedback().notice, /本次回答未完成/); assert.doesNotMatch(h.feedback().notice, /本地原文摘录/);
  assert.doesNotMatch(h.text(), /需要澄清|还缺少的依据|原文摘录 · 非 AI 总结/);
  const process = h.nodes().find(n => n.props.className === "rw-run-progress");
  const summary = JSON.stringify(process.props.children[0]);
  assert.match(summary, /处理结束 · /); assert.match(summary, /本次回答未完成/);
  assert.doesNotMatch(summary, /已完成 · |回答生成完成/);
  assert.equal(h.nodes().find(n => n.type === "textarea").props.value, "问题");
  let focused = 0; const input = h.nodes().find(n => n.type === "textarea"); input.props.ref.current = { focus() { focused++; }, style: {}, scrollHeight: 44 };
  h.change("修改过的草稿"); const before = h.calls.length;
  h.button("重试本问题").props.onClick(); h.render();
  assert.equal(h.nodes().find(n => n.type === "textarea").props.value, "问题"); assert.equal(focused, 1);
  assert.equal(h.calls.length, before); assert.equal(h.calls.filter(([p]) => p === "/chat/runs").length, 1);
  assert.match(h.feedback().notice, /尚未重新提交/);
  assert.ok(!h.nodes().some(n => n.props.className === "rw-chat-show-citations")); h.unmount();
});

test("busy and scope changes close composer popovers without affecting stream locks", async () => {
  const { h } = await chat(); const trigger = { focus() {} };
  const panel = () => h.nodes().find(n => n.props.id === "rw-composer-options");
  h.button("＋ 材料").props.onClick({ currentTarget: trigger }); h.render(); assert.equal(panel().props.hidden, false);
  h.change("问题"); h.submit(); await h.settle(); assert.equal(panel().props.hidden, true);
  assert.equal(h.button("＋ 材料").props.disabled, true);
  h.render({ space: other }); await h.settle(); assert.equal(panel().props.hidden, true); h.unmount();
});
