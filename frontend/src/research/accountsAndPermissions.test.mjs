import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import ts from "typescript";
import { activeMember, canManageSpace, kinds, passwordError, roles, spaceName, spaceRoles } from "./types.ts";

const directory = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const member = { id: "member", email: "member@example.org", role: "member" };
const leader = { id: "leader", email: "leader@example.org", role: "owner" };
const makeSpace = (patch = {}) => ({ id: "space/1", name: "Private", kind: "private", owner_id: member.id, role: "owner", ...patch });

// Exercise the real TSX event handlers and effect/request branches without adding
// a DOM dependency or altering this project's Node test/build configuration.
function harness(filename, request = async () => ({})) {
  let state = [], refs = [], effects = [], stateIndex = 0, refIndex = 0, effectIndex = 0, pending = [], tree, props;
  const cache = new Map();
  const calls = [], csrf = [];
  const react = {
    useState(initial) { const index = stateIndex++; if (!(index in state)) state[index] = typeof initial === "function" ? initial() : initial; return [state[index], value => { state[index] = typeof value === "function" ? value(state[index]) : value; }]; },
    useRef(initial) { const index = refIndex++; refs[index] ??= { current: initial }; return refs[index]; },
    useEffect(callback, dependencies) { const index = effectIndex++; const previous = effects[index]; if (!previous || !dependencies || dependencies.some((value, i) => !Object.is(value, previous.dependencies[i]))) { pending.push(() => { previous?.cleanup?.(); effects[index] = { dependencies, cleanup: callback() }; }); } },
    useCallback(callback) { return callback; },
  };
  const jsx = (type, properties, key) => ({ type, props: properties ?? {}, key });
  const ui = {
    Feedback: "Feedback", PanelHeading: "PanelHeading", Empty: "Empty",
    useAction() {
      const [error, setError] = react.useState(""); const [notice, setNotice] = react.useState("");
      return { busy: false, error, notice, setError, setNotice, async run(fn, success = "") { setError(""); setNotice(""); try { await fn(); setNotice(success); } catch (error) { setError(error.message); } } };
    },
  };
  const api = {
    async api(...args) { calls.push(args); return request(...args); }, setCsrf(value) { csrf.push(value); },
    errorText: error => error.message, idPath: encodeURIComponent,
    listOf(value, key) { if (Array.isArray(value)) return value; if (Array.isArray(value?.[key])) return value[key]; throw new Error("bad list"); },
    ApiError: class extends Error { constructor(status, message) { super(message); this.status = status; } },
  };
  function load(file) {
    if (cache.has(file)) return cache.get(file).exports;
    const module = { exports: {} }; cache.set(file, module);
    const output = ts.transpileModule(readFileSync(file, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    const localRequire = name => {
      if (name === "react") return react;
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "Fragment" };
      if (name === "./api") return api;
      if (name === "./ui") return ui;
      if (name.endsWith(".css")) return {};
      if (name === "./types") return load(resolve(directory, "types.ts"));
      if (name.startsWith("./")) return { default: name.slice(2) };
      return require(name);
    };
    new Function("require", "module", "exports", output)(localRequire, module, module.exports);
    return module.exports;
  }
  const Component = load(resolve(directory, filename)).default;
  function render(nextProps = props) { props = nextProps; stateIndex = refIndex = effectIndex = 0; tree = Component(props); const jobs = pending; pending = []; jobs.forEach(job => job()); return tree; }
  const nodes = () => {
    const values = [];
    const visit = node => { if (Array.isArray(node)) node.forEach(visit); else if (node && typeof node === "object") { values.push(node); visit(node.props?.children); } };
    visit(tree); return values;
  };
  const textOf = node => Array.isArray(node) ? node.map(textOf).join("") : node && typeof node === "object" ? textOf(node.props?.children) : node === false || node == null ? "" : String(node);
  const button = label => { const value = nodes().find(node => node.type === "button" && textOf(node) === label); assert.ok(value, `missing button: ${label}`); return value; };
  const input = id => { const value = nodes().find(node => node.type === "input" && node.props.id === id); assert.ok(value, `missing input: ${id}`); return value; };
  const form = () => { const value = nodes().find(node => node.type === "form"); assert.ok(value); return value; };
  async function settle() { await new Promise(resolve => setImmediate(resolve)); render(); }
  async function submit(target = form()) { target.props.onSubmit({ preventDefault() {} }); await settle(); }
  const feedback = () => nodes().filter(node => node.type === "Feedback").map(node => node.props);
  return { render, settle, submit, nodes, button, input, form, calls, csrf, feedback, apiError: api.ApiError, text: () => textOf(tree), change(id, value) { input(id).props.onChange({ target: { value } }); render(); } };
}

test("Chinese kinds and default names preserve custom saved names and distinguish owners", () => {
  assert.deepEqual(kinds, { basis: "项目知识库", private: "个人知识库", collaboration: "协作知识库", results: "成果知识库" });
  for (const [kind, name] of [["basis", "Basis"], ["private", "Private"], ["results", "Results"]]) {
    const space = makeSpace({ kind, name }); assert.equal(spaceName(space), kinds[kind]); assert.equal(space.name, name);
    assert.equal(spaceName({ ...space, name: `${name} 实验` }), `${name} 实验`);
  }
  assert.equal(spaceName(makeSpace({ kind: "collaboration", name: "Private" })), "Private");
  assert.equal(spaceRoles.owner, "空间所有者"); assert.equal(roles.owner, "项目负责人");
  assert.equal(passwordError("short", "short"), "密码至少需要12个字符。");
  assert.match(passwordError("long-password-123", "different"), /不一致/);
  assert.equal(passwordError("long-password-123", "long-password-123"), "");
  assert.equal(activeMember({ disabled_at: 123 }), false); assert.equal(activeMember({ user: { ...member, active: false } }), false);
});

test("manage requires actual space ownership, never project role, and excludes fixed spaces", () => {
  for (const kind of ["private", "collaboration"]) {
    assert.equal(canManageSpace(makeSpace({ kind }), member), true);
    for (const role of ["reader", "editor"]) assert.equal(canManageSpace(makeSpace({ kind, owner_id: member.id, role }), leader), false);
    assert.equal(canManageSpace(makeSpace({ kind, role: "owner" }), leader), false);
  }
  for (const kind of ["basis", "results"]) assert.equal(canManageSpace(makeSpace({ kind }), member), false);
});

test("login has a single password, visibility toggle and honest recovery instructions", () => {
  const h = harness("Auth.tsx"); h.render({ onSession() {} });
  assert.equal(h.nodes().filter(node => node.type === "input" && node.props.autoComplete === "current-password").length, 1);
  assert.equal(h.nodes().some(node => node.props.id === "auth-confirmation"), false);
  assert.match(h.text(), /SciLoam/); assert.match(h.text(), /让项目知识有迹可循/); assert.match(h.text(), /不会发送密码重置邮件/);
  h.button("显示").props.onClick(); h.render(); assert.equal(h.input("auth-password").props.type, "text");
  h.button("隐藏").props.onClick(); h.render(); assert.equal(h.input("auth-password").props.type, "password");
});

test("registration validates confirmation locally and never sends it", async () => {
  const received = [];
  const h = harness("Auth.tsx", async () => ({ user: member, csrf_token: "new-csrf" }));
  h.render({ onSession: session => received.push(session) }); h.button("收到邀请？受邀注册").props.onClick(); h.render();
  h.change("auth-email", "member@example.org"); h.change("auth-password", "valid-password-123"); h.change("auth-confirmation", "mismatch"); h.change("auth-invite", "token");
  await h.submit(); assert.equal(h.calls.length, 0); assert.match(h.feedback()[0].error, /不一致/);
  h.change("auth-password", "short"); h.change("auth-confirmation", "short"); await h.submit(); assert.equal(h.calls.length, 0);
  h.change("auth-password", "valid-password-123"); h.change("auth-confirmation", "valid-password-123"); await h.submit();
  assert.deepEqual(h.calls[0], ["/auth/register", "POST", { email: member.email, password: "valid-password-123", invite_token: "token" }]);
  assert.equal(received.length, 1); assert.deepEqual(h.csrf, ["new-csrf"]);
  h.button("已有账号？返回登录").props.onClick(); h.render(); assert.equal(h.input("auth-password").props.value, "");
});

test("account validates all password fields, sends exactly old/new, handles failure and reauthentication", async () => {
  let changed = 0, failure = true;
  const h = harness("Account.tsx", async () => { if (failure) throw new Error("当前密码错误"); return { password_changed: true, reauthenticate: true }; });
  h.render({ user: member, onPasswordChanged: async () => { changed++; } });
  await h.submit(); assert.equal(h.calls.length, 0);
  h.change("account-current-password", "old-password"); h.change("account-new-password", "short"); h.change("account-confirmation", "short"); await h.submit(); assert.equal(h.calls.length, 0);
  h.change("account-new-password", "new-password-123"); h.change("account-confirmation", "different"); await h.submit(); assert.equal(h.calls.length, 0);
  h.change("account-confirmation", "new-password-123"); await h.submit(); assert.equal(changed, 0); assert.match(h.feedback()[0].error, /当前密码错误/);
  assert.deepEqual(h.calls[0], ["/auth/password", "POST", { current_password: "old-password", new_password: "new-password-123" }]);
  h.button("显示").props.onClick(); h.render(); assert.equal(h.input("account-current-password").props.type, "text");
  failure = false; await h.submit(); assert.equal(changed, 1);
  for (const id of ["account-current-password", "account-new-password", "account-confirmation"]) assert.equal(h.input(id).props.value, "");
});

test("account never reports success for an unconfirmed password response", async () => {
  let changed = false; const h = harness("Account.tsx", async () => ({ password_changed: false, reauthenticate: true }));
  h.render({ user: member, onPasswordChanged: async () => { changed = true; } });
  h.change("account-current-password", "old"); h.change("account-new-password", "new-password-123"); h.change("account-confirmation", "new-password-123"); await h.submit();
  assert.equal(changed, false); assert.match(h.feedback()[0].error, /未确认/);
});

test("reader/editor have no grant/revoke/list-management controls or management requests", async () => {
  for (const kind of ["private", "collaboration", "basis", "results"]) for (const role of ["reader", "editor"]) {
    const h = harness("Members.tsx"); h.render({ user: member, space: makeSpace({ kind, role, owner_id: leader.id }), onChanged: async () => {} }); await h.settle();
    assert.ok(!h.nodes().some(node => node.type === "button" && /添加授权|取消授权|刷新管理列表|生成邀请令牌|停用/.test(node.props.children)));
    assert.deepEqual(h.nodes().filter(node => node.type === "select").map(node => node.props["aria-label"]), ["授权知识库"]);
    assert.deepEqual(h.calls, []); assert.match(h.text(), /接受已有邀请/);
  }
});

test("project members and invitation remain available without any library", async () => {
  const h = harness("Members.tsx", async path => path === "/members" ? [leader, member] : []);
  h.render({ user: leader, spaces: [], onChanged: async () => {} }); await h.settle();
  assert.match(h.text(), /项目成员/); assert.match(h.text(), /生成邀请令牌/); assert.match(h.text(), /接受已有邀请/);
  assert.ok(h.calls.some(([path]) => path === "/members")); assert.ok(!h.calls.some(([path]) => path.startsWith("/spaces/")));
  assert.match(h.text(), /项目成员与邀请不依赖知识库/);
});

test("library authorization switches locally without reloading project people or exposing old grants", async () => {
  let resolveAccess; const pending = { promise: new Promise(resolve => { resolveAccess = resolve; }), resolve: value => resolveAccess(value) };
  const first = makeSpace(), second = { ...first, id: "second" };
  const h = harness("Members.tsx", async path => path === "/members" ? [member, leader] : path.includes("second") ? pending.promise : [{ user_id: "old-reader", email: "old-reader@example.org", role: "reader" }]);
  const props = { user: member, space: first, spaces: [first, second], onChanged: async () => {} };
  h.render(props); await h.settle(); assert.match(h.text(), /old-reader@example.org/);
  h.render({ ...props, space: second }); assert.doesNotMatch(h.text(), /old-reader@example.org/);
  pending.resolve([]); await h.settle();
  assert.equal(h.calls.filter(([path]) => path === "/members").length, 1);
});

test("project owner does not gain private/collaboration management from their project role", async () => {
  for (const kind of ["private", "collaboration"]) {
    const h = harness("Members.tsx", async () => [leader, member]);
    h.render({ user: leader, space: makeSpace({ kind, role: "reader" }), onChanged: async () => {} }); await h.settle();
    assert.deepEqual(h.nodes().filter(node => node.type === "select").map(node => node.props["aria-label"]), ["授权知识库"]);
    assert.ok(!h.nodes().some(node => node.type === "button" && /添加授权|取消授权|刷新管理列表/.test(node.props.children))); assert.match(h.text(), /生成邀请令牌/);
    assert.deepEqual(h.calls, [["/members"]]);
  }
});

test("space owner can grant all active people including project owner in private and collaboration", async () => {
  for (const kind of ["private", "collaboration"]) {
    const h = harness("Members.tsx", async path => path === "/members" ? [member, leader, { id: "inactive", disabled_at: 123 }, { id: "active-false", active: false }] : []);
    h.render({ user: member, space: makeSpace({ kind }), onChanged: async () => {} }); await h.settle();
    assert.doesNotMatch(h.text(), /生成邀请令牌|停用/); assert.match(h.text(), /空间所有者/);
    const options = h.nodes().filter(node => node.type === "option"); assert.ok(options.some(node => node.props.value === leader.id));
    assert.ok(!options.some(node => [member.id, "inactive", "active-false"].includes(node.props.value)));
    const select = h.nodes().find(node => node.type === "select" && node.props.required); select.props.onChange({ target: { value: leader.id } }); h.render();
    const grantForm = h.nodes().find(node => node.type === "form" && node.props.children?.some?.(node => node?.type === "button" && node.props.children === "添加授权"));
    await h.submit(grantForm);
    assert.ok(h.calls.some(([path, method, body]) => path === "/spaces/space%2F1/members/leader" && method === "PUT" && body.role === "reader"));
  }
});

test("project owner grant is editable and revocable; only owner_id is fixed", async () => {
  const originalWindow = globalThis.window; const confirmations = [];
  globalThis.window = { confirm: text => { confirmations.push(text); return true; } };
  try {
    const h = harness("Members.tsx", async path => path === "/members" ? [member, leader] : [{ user_id: member.id, role: "owner", email: member.email }, { user_id: leader.id, role: "reader", email: leader.email }]);
    h.render({ user: member, space: makeSpace(), onChanged: async () => {} }); await h.settle();
    assert.equal(h.nodes().filter(node => node.type === "button" && node.props.children === "取消授权").length, 1);
    const role = h.nodes().find(node => node.type === "select" && node.props["aria-label"]?.endsWith("的空间角色")); assert.equal(role.props.disabled, false);
    role.props.onChange({ target: { value: "editor" } }); await h.settle();
    assert.ok(h.calls.some(([, method, body]) => method === "PUT" && body.role === "editor"));
    h.button("取消授权").props.onClick(); await h.settle();
    assert.ok(h.calls.some(([path, method]) => path.endsWith("/leader") && method === "DELETE"));
    assert.match(confirmations[0], /旧会话失效/); assert.match(confirmations[0], /复制的内容无法收回/);
    assert.ok(!h.calls.some(([path, method]) => path.endsWith("/member") && ["PUT", "DELETE"].includes(method)));
  } finally { if (originalWindow === undefined) delete globalThis.window; else globalThis.window = originalWindow; }
});

test("fixed basis/results never display grant management even for their owner", async () => {
  for (const kind of ["basis", "results"]) {
    const h = harness("Members.tsx", async () => [leader]); h.render({ user: leader, space: makeSpace({ kind, owner_id: leader.id }), onChanged: async () => {} }); await h.settle();
    assert.doesNotMatch(h.text(), /添加授权|取消授权/); assert.deepEqual(h.nodes().filter(node => node.type === "select").map(node => node.props["aria-label"]), ["授权知识库"]);
    assert.ok(h.calls.every(([path]) => path === "/members"));
  }
});

test("workspace uses role-specific review labels and retains goals with account without spaces", async () => {
  for (const user of [member, leader]) {
    const h = harness("ResearchApp.tsx", async path => path === "/auth/me" ? { user } : []); h.render({}); await h.settle(); await h.settle();
    assert.match(h.text(), /SciLoam/); assert.match(h.text(), /基于知识库问答/); assert.match(h.text(), /任务管理/); assert.doesNotMatch(h.text(), /任务进展|让项目知识有迹可循|审计记录/);
    h.button(user.role === "owner" ? "审核管理" : "我的提交");
    h.button("账号与密码").props.onClick(); h.render(); assert.ok(h.nodes().some(node => node.type === "Account"));
    h.button("任务管理").props.onClick(); h.render(); assert.ok(h.nodes().some(node => node.type === "Goals"));
  }
});

test("password-change API keeps cookie credentials and current CSRF without leaking confirmation", async () => {
  const { api, setCsrf } = await import("./api.ts");
  const originalFetch = globalThis.fetch, originalDocument = globalThis.document;
  try {
    setCsrf("old-csrf"); globalThis.document = { cookie: "research_csrf=current-csrf" };
    globalThis.fetch = async (path, request) => {
      assert.equal(path, "/api/auth/password"); assert.equal(request.credentials, "include");
      assert.equal(request.headers["X-CSRF-Token"], "current-csrf");
      assert.deepEqual(JSON.parse(request.body), { current_password: "old-password", new_password: "new-password-123" });
      return new Response(JSON.stringify({ password_changed: true, reauthenticate: true }));
    };
    assert.deepEqual(await api("/auth/password", "POST", { current_password: "old-password", new_password: "new-password-123" }), { password_changed: true, reauthenticate: true });
  } finally { globalThis.fetch = originalFetch; if (originalDocument === undefined) delete globalThis.document; else globalThis.document = originalDocument; setCsrf(); }
});

test("expected post-password 401 is a signed-out success and stale space loads cannot restore old data", async () => {
  let changed = false, releaseSpaces;
  const delayedSpaces = new Promise(resolve => { releaseSpaces = resolve; });
  const h = harness("ResearchApp.tsx", async path => {
    if (path === "/auth/me") { if (changed) { const error = new Error("expired"); error.status = 401; Object.setPrototypeOf(error, h.apiError.prototype); throw error; } return { user: member }; }
    return path === "/spaces" ? delayedSpaces : [];
  });
  h.render({}); await h.settle();
  h.button("账号与密码").props.onClick(); h.render(); changed = true;
  await h.nodes().find(node => node.type === "Account").props.onPasswordChanged(); h.render();
  const auth = h.nodes().find(node => node.type === "Auth"); assert.ok(auth); assert.equal(auth.props.initialError, ""); assert.match(auth.props.initialNotice, /密码修改成功/);
  releaseSpaces([makeSpace({ name: "old-private-data" })]); await h.settle();
  assert.doesNotMatch(h.text(), /old-private-data/); assert.ok(h.nodes().some(node => node.type === "Auth"));
});

test("password-change callback clears session and active spaces, refreshes me, shows success then login", async () => {
  let changed = false;
  const h = harness("ResearchApp.tsx", async path => path === "/auth/me" ? changed ? { user: null, csrf_token: "fresh" } : { user: member } : path === "/spaces" ? [makeSpace()] : []);
  h.render({}); await h.settle(); await h.settle(); assert.match(h.text(), /个人知识库/); assert.doesNotMatch(h.text(), /Private/);
  h.button("账号与密码").props.onClick(); h.render(); changed = true;
  await h.nodes().find(node => node.type === "Account").props.onPasswordChanged(); h.render();
  const auth = h.nodes().find(node => node.type === "Auth"); assert.ok(auth); assert.match(auth.props.initialNotice, /密码修改成功.*重新登录/);
  assert.equal(h.calls.filter(([path]) => path === "/auth/me").length, 2); assert.ok(h.csrf.includes(undefined));
  auth.props.onSession({ user: { ...member, id: "next-user" } }); h.render(); assert.doesNotMatch(h.text(), /个人知识库/);
});
