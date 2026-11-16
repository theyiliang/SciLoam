import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import ts from "typescript";

const directory = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const space = { id: "space-a", name: "实验评价协作库", kind: "collaboration", role: "editor", owner_id: "other" };
const other = { ...space, id: "space-b", name: "我的个人知识库", kind: "private" };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
// Transpile and execute actual TSX, including handlers, effects and request ordering.
// Child components are represented as nodes; Citations has its own actual-TSX tests.
function harness(filename, request = async () => []) {
  let state = [], refs = [], effects = [], si = 0, ri = 0, ei = 0, pending = [], props, tree;
  const calls = [], cache = new Map();
  const react = {
    useState(initial) { const i = si++; if (!(i in state)) state[i] = typeof initial === "function" ? initial() : initial; return [state[i], value => { state[i] = typeof value === "function" ? value(state[i]) : value; }]; },
    useRef(initial) { const i = ri++; return refs[i] ??= { current: initial }; },
    useEffect(callback, dependencies) { const i = ei++, previous = effects[i]; if (!previous || !dependencies || dependencies.some((value, j) => !Object.is(value, previous.dependencies[j]))) pending.push(() => { previous?.cleanup?.(); effects[i] = { dependencies, cleanup: callback() }; }); },
    useCallback(callback) { return callback; },
  };
  const jsx = (type, props, key) => ({ type, props: props ?? {}, key });
  const api = { async api(...args) { calls.push(args); return request(...args); }, errorText: error => error.message, idPath: encodeURIComponent, listOf: (value, key) => Array.isArray(value) ? value : value[key], ApiError: class extends Error {}, setCsrf() {}, trackEvent() {} };
  const ui = { Feedback: "Feedback", Empty: "Empty", JsonDetails: "JsonDetails", SourceLink: "SourceLink", useAction() { const [error, setError] = react.useState(""); return { error, setError, setNotice() {}, busy: false, run: async fn => { try { await fn(); } catch (error) { setError(error.message); } } }; } };
  function load(file) {
    if (cache.has(file)) return cache.get(file).exports;
    const module = { exports: {} }; cache.set(file, module);
    const output = ts.transpileModule(readFileSync(file, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    const localRequire = name => {
      if (name === "react") return react;
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "Fragment" };
      if (name === "./api") return api;
      // UI/history invariants run against the real fetch reader.
      if (name === "./stream") {
        let result;
        return { MAX_TENTATIVE_TEXT: 1024 * 1024, accessLost: error => [401, 403].includes(error?.status), replayExpired: () => false,
          startChatRun: async body => { result = await api.api("/chat/runs", "POST", body); return { ...body, run_id: "stage4-run", message_id: "a", status: "queued" }; },
          cancelChatRun: async () => ({}), watchChatRun: async identity => ({ run_id: identity.run_id, seq: 1, timestamp: "2026-01-01T00:00:00Z", type: "answer.final", data: { ...result, message_id: identity.message_id } }) };
      }
      if (name === "./ui") return ui;
      if (name === "./types") return load(resolve(directory, "types.ts"));
      if (name.endsWith(".css")) return {};
      if (name.startsWith(".")) return { default: name.includes("MarkdownMessage") ? "MarkdownMessage" : name.slice(2) };
      return require(name);
    };
    new Function("require", "module", "exports", output)(localRequire, module, module.exports); return module.exports;
  }
  const Component = load(resolve(directory, filename)).default;
  const render = (next = props) => { props = next; si = ri = ei = 0; tree = Component(props); const jobs = pending; pending = []; jobs.forEach(job => job()); return tree; };
  const nodes = () => { const result = []; const visit = node => { if (Array.isArray(node)) node.forEach(visit); else if (node && typeof node === "object") { result.push(node); visit(node.props?.children); } }; visit(tree); return result; };
  const textOf = node => Array.isArray(node) ? node.map(textOf).join("") : node && typeof node === "object" ? node.type === "MarkdownMessage" ? node.props.text : textOf(node.props?.children) : node === false || node == null ? "" : String(node);
  const button = label => { const found = nodes().find(node => node.type === "button" && textOf(node) === label); assert.ok(found, `missing button ${label}`); return found; };
  const form = () => nodes().find(node => node.type === "form");
  const input = () => nodes().find(node => node.type === "textarea");
  const settle = async () => { await new Promise(resolve => setImmediate(resolve)); render(); };
  return { render, nodes, calls, button, form, input, settle, nodeText: textOf, text: () => textOf(tree), feedback: () => nodes().find(node => node.type === "Feedback")?.props, change(value) { input().props.onChange({ target: { value } }); render(); }, submit() { form().props.onSubmit({ preventDefault() {} }); } };
}

async function chat(request, props = {}) { const h = harness("Chat.tsx", request); h.render({ space, ...props }); await h.settle(); return h; }

test("library picker is beside chat, respects visible spaces, and old single-space props work", async () => {
  const chosen = []; let knowledge = 0;
  const h = await chat(async () => [], { spaces: [space, other], onSelectSpace: id => chosen.push(id), onOpenKnowledge: () => knowledge++ });
  const picker = h.nodes().filter(node => node.type === "button" && node.props.className?.includes("rw-chat-library"));
  assert.equal(picker.length, 2); assert.ok(!h.text().includes("未授权的秘密库"));
  picker[1].props.onClick(); assert.deepEqual(chosen, [other.id]);
  h.button("知识库 / 创建协作库").props.onClick(); assert.equal(knowledge, 1);
  h.button("收起回答依据").props.onClick(); h.render(); assert.ok(!h.nodes().some(node => node.props.id === "rw-answer-evidence"));
  h.button("展开回答依据").props.onClick(); h.render(); assert.ok(h.nodes().some(node => node.props.id === "rw-answer-evidence"));
  const old = await chat(async () => []); assert.equal(old.nodes().filter(node => node.props.className?.includes("rw-chat-library")).length, 1);
});

test("backend status, missing evidence and safe fixed CTAs render, never model URLs/tools", async () => {
  for (const status of ["sufficient", "partial", "insufficient", "conflicting"]) {
    let opened = 0;
    const message = { role: "assistant", content: "记录B未注明数据版本。", answer_status: status, missing_evidence: ["记录B的数据版本", "样本范围"], citations: [], suggested_actions: ["switch_library", "add_knowledge", "rephrase_question", "https://evil.invalid", { action: "run_tool", label: "执行外部工具" }] };
    const h = await chat(async path => path.includes("?") ? [{ id: "c", title: "之前的问题" }] : { id: "c", space_id: space.id, messages: [message] }, { onOpenKnowledge: () => opened++ });
    h.button("之前的问题当前知识库会话").props.onClick(); await h.settle();
    assert.match(h.text(), status === "insufficient" ? /本次未形成可用回答/ : /没有可核验的引用依据/); assert.match(h.text(), /记录B的数据版本/);
    h.button("切换知识库"); h.button("修改问题"); h.button("补充相关知识").props.onClick(); assert.equal(opened, 1);
    assert.ok(!h.text().includes("evil.invalid")); assert.ok(!h.text().includes("执行外部工具"));
    assert.ok(h.nodes().some(node => node.props.className?.includes(`rw-chat-${status}`)));
  }
});

test("research QA explains knowledge enhancement without requiring a library hit", async () => {
  const h = await chat(async () => []);
  assert.match(h.text(), /基于知识库问答/); assert.match(h.text(), /可以讨论概念、研究方法与建议/);
  assert.doesNotMatch(h.text(), /通用问题不要求知识库命中|课题事实须有据|总结须明确覆盖范围|不虚构引用/);
  assert.doesNotMatch(h.text(), /回答只基于当前知识库|单库问答/);
  assert.deepEqual(h.nodes().filter(n => n.type === "option" && ["qa", "align", "progress"].includes(n.props.value)).map(n => n.props.value), ["qa", "align", "progress"]);
});

test("optional answer kinds distinguish ordinary discussion from missing grounded evidence", async () => {
  for (const kind of [undefined, "general", "mixed", "clarification", "grounded"]) {
    const message = { role: "assistant", content: "科研方法建议", answer_kind: kind, citations: [] };
    const h = await chat(async path => path.includes("?") ? [{ id: "c", title: "已存回答" }] : { id: "c", space_id: space.id, messages: [message] }, { onOpenKnowledge() {} });
    h.button("已存回答当前知识库会话").props.onClick(); await h.settle();
    assert.equal(h.nodes().some(n => n.props.className === "rw-chat-evidence-warning"), kind === "grounded");
    assert.equal(h.nodes().some(n => n.type === "button" && n.props.children === "补充相关知识"), kind === "grounded");
    if (kind === "general") assert.match(h.text(), /通识回答/);
    if (kind === "mixed") assert.match(h.text(), /混合回答/);
    if (kind === "clarification") assert.match(h.text(), /请补充具体信息/);
    if (kind !== "grounded") assert.doesNotMatch(h.text(), /不应将以下内容当作已证实|可切换知识库或补充/);
  }
});

test("ordinary answers lead with their body while kind explanations and success status stay collapsed", async () => {
  const outsideDetails = node => Array.isArray(node) ? node.map(outsideDetails).join("") : node && typeof node === "object" ? node.type === "details" ? "" : node.type === "MarkdownMessage" ? node.props.text : outsideDetails(node.props?.children) : node === false || node == null ? "" : String(node);
  for (const kind of ["general", "grounded", "mixed"]) {
    const message = { role: "assistant", content: "先固定研究问题，再确定样本范围。", answer_kind: kind, answer_status: "sufficient", citations: [{ version_id: "v", excerpt: "授权原文" }] };
    const h = await chat(async path => path.includes("?") ? [{ id: "c", title: "自然回答" }] : { id: "c", messages: [message] });
    h.button("自然回答当前知识库会话").props.onClick(); await h.settle();
    const article = h.nodes().find(n => n.props.className === "rw-chat-message rw-chat-assistant");
    const details = h.nodes().find(n => n.props.className === "rw-details rw-chat-answer-details");
    assert.equal(details.type, "details"); assert.equal(details.props.open, undefined);
    assert.match(h.nodeText(details), /回答说明/);
    assert.match(outsideDetails(article), /先固定研究问题/);
    assert.doesNotMatch(outsideDetails(article), /通识回答|证据回答|混合回答|回答已生成|概念、方法与建议来自|课题事实与文档总结应结合/);
    h.button("查看这条回答的 1 条依据").props.onClick(); h.render();
    assert.equal(h.nodes().find(n => n.type === "Citations").props.items[0].version_id, "v");
  }
});

test("historical failure retries only the original question manually and never invents read sources", async () => {
  for (const citations of [[], [{ version_id: "v", excerpt: "读取过的原文" }]]) {
    const h = await chat(async path => path.includes("?") ? [{ id: "c", title: "失败原问题" }] : { id: "c", messages: [{ role: "user", content: "总结这份实验记录" }, { role: "assistant", content: "服务处理超时，请稍后重试。", answer_kind: "failure", citations }] });
    h.button("失败原问题当前知识库会话").props.onClick(); await h.settle();
    let focused = 0; h.input().props.ref.current = { focus() { focused++; }, style: {}, scrollHeight: 44 };
    h.change("别的问题"); const before = h.calls.length;
    h.button("重试本问题").props.onClick(); h.render();
    assert.equal(h.input().props.value, "总结这份实验记录"); assert.equal(focused, 1);
    assert.equal(h.calls.length, before); assert.match(h.feedback().notice, /尚未重新提交/);
    assert.equal(h.nodes().filter(n => n.props.className === "rw-chat-show-citations").length, citations.length ? 1 : 0);
    assert.doesNotMatch(h.text(), /查看已读原文/);
  }
});

test("clarification titles identify the concrete missing information without boilerplate", async () => {
  for (const [kind, title] of [["document", "请选择要查看的文档"], ["scope", "请确认统计范围"], ["comparison", "请补充要比较的材料"], ["other", "请补充具体信息"], [undefined, "请补充具体信息"]]) {
    const message = { role: "assistant", content: "请说明本次讨论的具体对象。", answer_kind: "clarification", clarification_kind: kind };
    const h = await chat(async path => path.includes("?") ? [{ id: "c", title: "待确认" }] : { id: "c", messages: [message] });
    h.button("待确认当前知识库会话").props.onClick(); await h.settle();
    assert.match(h.text(), new RegExp(title)); assert.doesNotMatch(h.text(), /需要澄清|请补充问题中的对象或范围/);
    assert.equal(h.nodes().filter(n => n.type === "MarkdownMessage" && n.props.text === message.content).length, 1);
  }
});

test("explicit and legacy fallback failures are neutral, not missing-input clarifications", async () => {
  for (const metadata of [{ answer_kind: "failure" }, { answer_kind: "clarification", fallback: true }, { answer_kind: "clarification", fallback: "provider_timeout" }, { fallback: true, answer_status: "insufficient" }]) {
    const message = { role: "assistant", content: "本次处理超时，尚未生成完整回答，请稍后重试。", answer_status: "insufficient", missing_evidence: ["旧版误标的缺少输入"], suggested_actions: ["select_material", "rephrase_question", "add_knowledge"], ...metadata };
    const h = await chat(async path => path.includes("?") ? [{ id: "c", title: "失败记录" }] : { id: "c", messages: [message] }, { onOpenKnowledge() {} });
    h.button("失败记录当前知识库会话").props.onClick(); await h.settle();
    assert.match(h.text(), /本次回答未完成/); assert.match(h.text(), /本次处理超时/);
    assert.doesNotMatch(h.text(), /需要澄清|请补充具体信息|还缺少的依据|旧版误标|原文摘录 · 非 AI 总结/);
    assert.ok(!h.nodes().some(n => n.type === "button" && ["选择材料", "修改问题", "补充相关知识"].includes(n.props.children)));
  }
});

test("grounded insufficient answer still exposes the missing-citation warning", async () => {
  const h = await chat(async path => path.includes("?") ? [{ id: "c", title: "材料问题" }] : { id: "c", messages: [{ role: "assistant", content: "尚缺实验记录。", answer_kind: "grounded", answer_status: "insufficient", citations: [] }] });
  h.button("材料问题当前知识库会话").props.onClick(); await h.settle();
  assert.match(h.text(), /本次未形成可用回答/); assert.match(h.text(), /没有可核验的引用依据/);
});

test("general and mixed status metadata does not impose material-only warnings or CTAs", async () => {
  for (const kind of ["general", "mixed", "clarification"]) {
    const h = await chat(async path => path.includes("?") ? [{ id: "c", title: "通用回答" }] : { id: "c", messages: [{ role: "assistant", content: "材料缺失时先讨论方法。", answer_kind: kind, answer_status: "partial", citations: [] }] }, { onOpenKnowledge() {} });
    h.button("通用回答当前知识库会话").props.onClick(); await h.settle();
    assert.doesNotMatch(h.text(), /没有可核验的引用依据|补充相关知识|目前只能确认部分信息/);
  }
});

test("submit has synchronous ref gate and canonical GET renders one saved answer, not POST append", async () => {
  const pending = deferred(); let saved = false;
  const h = await chat(async (path, method) => {
    if (path.includes("?")) return saved ? [{ id: "c", title: "研究问题" }] : [];
    if (path === "/conversations" && method === "POST") return { id: "c" };
    if (path === "/chat/runs") { await pending.promise; saved = true; return { conversation_id: "c", answer: "已保存的回答", citations: [], answer_status: "insufficient" }; }
    return { id: "c", space_id: space.id, messages: [{ id: "u", role: "user", content: "核对版本" }, { id: "a", role: "assistant", content: "已保存的回答", citations: [] }] };
  }, { spaces: [space, other], onSelectSpace() {} });
  h.change("核对版本"); h.submit(); h.submit(); await h.settle();
  assert.equal(h.calls.filter(([path]) => path === "/chat/runs").length, 1);
  assert.equal(h.calls.filter(([path, method]) => path === "/conversations" && method === "POST").length, 1);
  assert.ok(h.nodes().filter(node => node.props.className?.includes("rw-chat-library")).every(node => node.props.disabled));
  const busyStatus = h.nodes().find(node => node.type === "p" && node.props.role === "status" && node.props.children?.startsWith?.("正在处理科研问题"));
  assert.equal(busyStatus?.props.children, "正在处理科研问题，按需读取授权资料并实时显示服务进度，运行期间暂不能切换知识库或历史。");
  pending.resolve(); await h.settle();
  assert.equal(h.nodes().filter(node => node.type === "MarkdownMessage" && node.props.text === "已保存的回答").length, 1);
  assert.equal(h.input().props.value, ""); assert.ok(h.calls.some(([path]) => path === "/conversations/c"));
  const user = h.nodes().find(node => node.props.className === "rw-chat-message rw-chat-user");
  assert.ok(user); assert.equal(user.props.children[0], false); // no user name or avatar
});

test("timeout preserves question, does not retry, and history recovers the persisted answer once", async () => {
  let saved = false;
  const h = await chat(async (path, method) => {
    if (path.includes("?")) return saved ? [{ id: "c", title: "超时问题" }] : [];
    if (path === "/conversations" && method === "POST") return { id: "c" };
    if (path === "/chat/runs") { saved = true; throw new Error("timeout"); }
    return { id: "c", space_id: space.id, messages: [{ role: "user", content: "问题保留" }, { role: "assistant", content: "服务端实际已保存", citations: [], answer_status: "insufficient" }] };
  });
  h.change("问题保留"); h.submit(); await h.settle();
  assert.equal(h.input().props.value, "问题保留"); assert.match(h.feedback().error, /先刷新并检查对话历史/);
  assert.equal(h.calls.filter(([path]) => path === "/chat/runs").length, 1);
  h.button("刷新历史").props.onClick(); await h.settle(); h.button("超时问题当前知识库会话").props.onClick(); await h.settle();
  assert.equal(h.nodes().filter(node => node.type === "MarkdownMessage" && node.props.text === "服务端实际已保存").length, 1);
  assert.equal(h.calls.filter(([path]) => path === "/chat/runs").length, 1);
});

test("space changes clear all per-space state and stale history or chat cannot refill it", async () => {
  const oldChat = deferred(), lateHistory = deferred();
  const h = await chat(async (path, method) => {
    if (path.includes("space_id=space-a")) return [{ id: "a", title: "旧历史" }];
    if (path.includes("space_id=space-b")) return lateHistory.promise;
    if (path === "/chat/runs") return oldChat.promise;
    if (path === "/conversations/a") return { id: "a", space_id: space.id, messages: [{ role: "assistant", content: "旧回答", citations: [{ version_id: "old" }] }] };
    if (method === "POST") return { id: "a" };
    return [];
  });
  h.button("旧历史当前知识库会话").props.onClick(); await h.settle(); h.change("旧问题"); h.submit(); await h.settle();
  h.render({ space: other }); h.render();
  assert.equal(h.input().props.value, ""); assert.doesNotMatch(h.text(), /旧历史|旧回答|旧问题/); assert.equal(h.feedback().error, "");
  oldChat.resolve({ conversation_id: "a", answer: "不应回填" }); await h.settle();
  assert.doesNotMatch(h.text(), /不应回填|旧回答/);
  lateHistory.resolve([{ id: "b", title: "新空间历史" }]); await h.settle(); assert.match(h.text(), /新空间历史/);
  assert.equal(h.calls.filter(([path]) => path === "/conversations/a").length, 1); // stale POST result must not trigger a canonical GET
});

test("out-of-order history selections never display a response from a prior space", async () => {
  const late = deferred();
  const h = await chat(async path => path.includes("space_id=space-a") ? [{ id: "a", title: "慢会话" }] : path === "/conversations/a" ? late.promise : []);
  h.button("慢会话当前知识库会话").props.onClick(); h.render({ space: other }); await h.settle();
  late.resolve({ id: "a", space_id: space.id, messages: [{ role: "assistant", content: "迟来的原空间回答" }] }); await h.settle();
  assert.doesNotMatch(h.text(), /迟来的原空间回答|慢会话/);
});

test("citations use authorized version title/number, snippet and character position", async () => {
  const h = harness("Citations.tsx", async () => ({ id: "uuid-hidden", title: "真实实验记录", number: 3, content: "开始证据片段结束", source: "https://example.org/original" }));
  h.render({ items: [{ version_id: "uuid-hidden", title: "旧标题", snippet: "证据片段", chunk_start: 2, chunk_end: 6 }] }); await h.settle();
  assert.match(h.text(), /真实实验记录 · v3/); assert.match(h.text(), /证据片段/); assert.match(h.text(), /\[2, 6\)/);
  h.button("查看引用原文").props.onClick(); h.render(); assert.match(h.text(), /开始证据片段结束/);
  const technical = h.nodes().find(node => node.type === "details"); assert.ok(technical); assert.ok(!h.nodes().some(node => node.type === "h4" && JSON.stringify(node).includes("uuid-hidden")));
  assert.equal(h.nodes().find(node => node.type === "SourceLink").props.source, "https://example.org/original");
});

test("citation request race cannot put A's version body under B", async () => {
  const a = deferred(), b = deferred();
  const h = harness("Citations.tsx", path => path.endsWith("/a") ? a.promise : b.promise);
  h.render({ items: [{ version_id: "a", title: "记录A" }] }); h.button("查看引用原文").props.onClick(); h.render();
  h.render({ items: [{ version_id: "b", title: "记录B" }] }); h.render(); h.button("查看引用原文").props.onClick(); h.render();
  b.resolve({ id: "b", number: 2, title: "记录B", content: "B正确原文" }); await h.settle();
  a.resolve({ id: "a", number: 9, title: "记录A", content: "A过期原文" }); await h.settle();
  assert.match(h.text(), /B正确原文/); assert.doesNotMatch(h.text(), /A过期原文|v9/);
});

test("workspace passes only visible spaces to Chat and knowledge create entry survives", async () => {
  const h = harness("ResearchApp.tsx", async path => path === "/auth/me" ? { user: { id: "u", role: "member", email: "u@example.org" } } : path === "/spaces" ? [space, other] : []);
  h.render({}); await h.settle(); await h.settle();
  const chat = h.nodes().find(node => node.type === "Chat"); assert.ok(chat); assert.deepEqual(chat.props.spaces, [space, other]);
  assert.equal(h.nodes().some(node => node.props.className === "rw-space-list"), false);
  chat.props.onSelectSpace(other.id); h.render(); assert.equal(h.nodes().find(node => node.type === "Chat").props.space.id, other.id);
  chat.props.onOpenKnowledge(); h.render(); assert.ok(h.nodes().some(node => node.type === "Documents")); assert.ok(h.nodes().find(node => node.type === "Documents").props.libraryNavigation);
  const sidebar = h.nodes().find(node => node.type === "aside" && node.props.className.includes("rw-sidebar")); assert.ok(!JSON.stringify(sidebar).includes("rw-space-list"));
});

test("same-id permission epoch change hides message, question, citations and feedback before effects rerender", async () => {
  const h = await chat(async path => path.includes("?") ? [{ id: "a", title: "旧会话" }] : { id: "a", space_id: space.id, messages: [{ role: "assistant", content: "权限变更前正文", citations: [{ version_id: "v" }], answer_status: "partial" }] });
  h.button("旧会话当前知识库会话").props.onClick(); await h.settle(); h.change("旧输入");
  h.render({ space: { ...space, epoch: 2, role: "reader" } });
  assert.doesNotMatch(h.text(), /权限变更前正文|旧会话/); assert.equal(h.input().props.value, "");
  assert.equal(h.nodes().some(node => node.type === "Citations"), false); assert.equal(h.feedback().error, "");
});

test("A-B-A navigation never revives an earlier A request", async () => {
  const late = deferred(); let aLoads = 0;
  const h = await chat(async path => {
    if (path.includes("space_id=space-a")) return ++aLoads === 1 ? [{ id: "c", title: "起始会话" }] : [{ id: "new", title: "最新A历史" }];
    if (path === "/conversations/c") return late.promise;
    return [];
  });
  h.button("起始会话当前知识库会话").props.onClick(); h.render({ space: other }); h.render({ space }); await h.settle();
  late.resolve({ id: "c", space_id: space.id, messages: [{ role: "assistant", content: "第一轮A陈旧正文" }] }); await h.settle();
  assert.match(h.text(), /最新A历史/); assert.doesNotMatch(h.text(), /第一轮A陈旧正文/); assert.equal(h.input().props.value, "");
});

test("same-version new citation excerpt resets original body and ignores mismatched version response", async () => {
  const first = deferred(), second = deferred(); let requests = 0;
  const h = harness("Citations.tsx", () => ++requests === 1 ? first.promise : second.promise);
  h.render({ items: [{ version_id: "v", excerpt: "旧片段" }] }); h.button("查看引用原文").props.onClick(); h.render();
  first.resolve({ id: "v", number: 1, content: "旧原文" }); await h.settle(); assert.match(h.text(), /旧原文/);
  h.render({ items: [{ version_id: "v", excerpt: "新片段" }] });
  assert.doesNotMatch(h.text(), /旧原文|旧片段/); assert.match(h.text(), /新片段/);
  h.render(); h.button("查看引用原文").props.onClick(); h.render();
  second.resolve({ id: "wrong-version", number: 8, content: "错误版本正文" }); await h.settle();
  assert.doesNotMatch(h.text(), /错误版本正文|v8/); assert.match(h.text(), /引用版本不一致/);
});

test("POST citation snippets merge into id-only persisted citations without appending a second answer", async () => {
  const h = await chat(async (path, method) => {
    if (path.includes("?")) return [];
    if (path === "/conversations" && method === "POST") return { id: "c" };
    if (path === "/chat/runs") return { conversation_id: "c", answer: "唯一回答", citations: [{ version_id: "v", excerpt: "本轮片段", number: 3, chunk_start: 1, chunk_end: 5 }] };
    return { id: "c", space_id: space.id, messages: [{ id: "a", role: "assistant", content: "唯一回答", citations: [{ version_id: "v" }] }] };
  });
  h.change("一个问题"); h.submit(); await h.settle();
  const citations = h.nodes().find(node => node.type === "Citations"); assert.equal(citations.props.items[0].excerpt, "本轮片段"); assert.equal(citations.props.items[0].number, 3);
  assert.equal(h.nodes().filter(node => node.type === "MarkdownMessage" && node.props.text === "唯一回答").length, 1);
});

test("safe source links reject executable protocols", async () => {
  const { safeHref } = await import("../markdown.ts");
  assert.equal(safeHref("javascript:alert(1)"), undefined); assert.equal(safeHref("data:text/html,<script>"), undefined);
  assert.equal(safeHref("https://example.org/paper"), "https://example.org/paper");
});


test("reading-first composer exposes optional searchable material, compact mode and help popovers", async () => {
  const h = await chat(async path => path.includes("/documents") ? [{ id: "doc", title: "测度说明", active_version: { id: "v", number: 2 } }] : []);
  assert.equal(h.input().props.rows, 2);
  const panel = () => h.nodes().find(n => n.props.id === "rw-composer-options");
  assert.equal(panel().props.hidden, true);
  const trigger = { focus() {} };
  h.button("＋ 材料").props.onClick({ currentTarget: trigger }); h.render();
  assert.equal(panel().props.hidden, false); assert.equal(panel().props["aria-label"], "选择当前材料");
  const search = h.nodes().find(n => n.props["aria-label"] === "搜索文档标题");
  search.props.onChange({ target: { value: "不存在" } }); h.render();
  assert.equal(h.nodes().filter(n => n.type === "option" && n.props.value === "v").length, 0);
  search.props.onChange({ target: { value: "测度" } }); h.render();
  h.nodes().find(n => n.props.id === "rw-material").props.onChange({ target: { value: "v" } }); h.render();
  assert.equal(panel().props.hidden, true); assert.equal(h.nodes().filter(n => n.props["aria-label"] === "当前材料").length, 1);
  h.button("基于知识库问答 ▾").props.onClick({ currentTarget: trigger }); h.render();
  assert.equal(panel().props["aria-label"], "选择问答模式");
  h.nodes().find(n => n.props.id === "rw-mode").props.onChange({ target: { value: "progress" } }); h.render();
  assert.equal(panel().props.hidden, true); assert.match(h.text(), /已选材料（进展模式不使用）/);
  h.button("范围与说明 ⓘ").props.onClick({ currentTarget: trigger }); h.render();
  assert.equal(panel().props["aria-label"], "范围与使用说明");
});

test("default library names show their kind once while custom names retain kind context", async () => {
  const { spaceKindLabel, spaceName } = await import("./types.ts");
  for (const [kind, legacyName, label] of [["basis", "Basis", "项目知识库"], ["private", "Private", "个人知识库"], ["results", "Results", "成果知识库"], ["collaboration", "协作知识库", "协作知识库"]]) {
    for (const name of [legacyName, label, "我的研究资料"]) {
      const library = { ...space, kind, name };
      const custom = name === "我的研究资料";
      assert.equal(spaceKindLabel(library), custom ? label : "");
      assert.equal(library.name, name); // Presentation never renames the persisted space.
      const h = harness("ResearchApp.tsx", async path => path === "/auth/me" ? { user: { id: "u", role: "member", email: "u@example.org" } } : path === "/spaces" ? [library] : []);
      h.render({}); await h.settle(); await h.settle();
      const title = h.nodes().find(n => n.props.className === "rw-topbar-title");
      assert.equal(h.nodeText(title), `☰工作台/${spaceName(library)}${custom ? label : ""}`);
      const c = await chat(async () => [], { space: library });
      const picker = c.nodes().find(n => n.props.className?.includes("rw-chat-library"));
      assert.equal(c.nodeText(picker), `${spaceName(library)}${custom ? `${label} · ` : ""}可编辑 · 当前`);
      const status = c.nodes().find(n => n.props.className === "rw-chat-status");
      assert.equal(c.nodeText(status), `${spaceName(library)}知识增强`);
    }
  }
});

test("reading-first chat keeps user questions right aligned", () => {
  const css = readFileSync(resolve(directory, "research.css"), "utf8");
  assert.match(css, /\.rw-reading-first \.rw-chat-messages > \.rw-chat-user \{ margin-left: auto; margin-right: 0;/);
});
