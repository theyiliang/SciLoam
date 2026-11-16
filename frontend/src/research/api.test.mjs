import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { api, ApiError, listOf, setCsrf } from "./api.ts";

const originalFetch = globalThis.fetch;
const originalDocument = globalThis.document;
afterEach(() => { globalThis.fetch = originalFetch; if (originalDocument === undefined) delete globalThis.document; else globalThis.document = originalDocument; setCsrf(); });

test("writes use current cookie after another tab rotates CSRF", async () => {
  setCsrf("old-tab-token");
  globalThis.document = { cookie: "research_csrf=new-cookie-token" };
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.headers["X-CSRF-Token"], "new-cookie-token");
    return new Response("{}");
  };
  await api("/spaces", "POST", {});
});

test("reads use same-origin /api and HttpOnly session credentials", async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/spaces");
    assert.equal(options.credentials, "include");
    assert.equal(options.method, "GET");
    assert.equal(options.headers["X-CSRF-Token"], undefined);
    return new Response(JSON.stringify([{ id: "space" }]));
  };
  assert.deepEqual(await api("/spaces"), [{ id: "space" }]);
});

test("every write method includes CSRF including login and bodyless delete", async () => {
  setCsrf("csrf-token");
  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
    globalThis.fetch = async (url, options) => {
      assert.equal(url, "/api/auth/login");
      assert.equal(options.credentials, "include");
      assert.equal(options.method, method);
      assert.equal(options.headers["X-CSRF-Token"], "csrf-token");
      if (method !== "DELETE") assert.deepEqual(JSON.parse(options.body), { value: 1 });
      else assert.equal(options.body, undefined);
      return new Response(null, { status: 204 });
    };
    assert.equal(await api("/auth/login", method, method === "DELETE" ? undefined : { value: 1 }), undefined);
  }
});

test("preserves server answer and fallback without rewriting content", async () => {
  const answer = { answer: "原始摘录", citations: [], events: [], conversation_id: "c", fallback: true };
  globalThis.fetch = async () => new Response(JSON.stringify(answer));
  assert.deepEqual(await api("/chat", "POST", {}), answer);
});

test("surfaces server errors with status without returning fake data", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "空间访问被撤销" }), { status: 403 });
  await assert.rejects(api("/spaces"), error => error instanceof ApiError && error.status === 403 && error.message.includes("空间访问被撤销"));
});

test("non-JSON proxy responses get a useful configuration error", async () => {
  globalThis.fetch = async () => new Response("<!doctype html>");
  await assert.rejects(api("/spaces"), /JSON/);
});

test("supported POST routes receive fresh UUID keys without changing CSRF or bodies", async () => {
  const paths = [
    "/spaces", "/spaces/s/documents", "/documents/d/versions", "/versions/v/share",
    "/reviews", "/reviews/r/decision", "/tasks", "/tasks/t/progress", "/conversations",
    "/spaces/s%2Fencoded/documents", "/tasks?view=all",
  ];
  const keys = new Set();
  setCsrf("session-csrf");
  globalThis.document = { cookie: "other=value; research_csrf=current%2Fcsrf" };
  for (const path of paths) {
    for (const body of [{ value: 1 }, { value: 2 }, { value: 2 }]) {
      globalThis.fetch = async (url, options) => {
        assert.equal(url, `/api${path}`);
        const key = options.headers["Idempotency-Key"];
        assert.match(key, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
        assert.equal(keys.has(key), false, "each invocation must get its own key, even for an identical body");
        keys.add(key);
        assert.equal(options.headers["X-CSRF-Token"], "current/csrf");
        assert.equal(options.headers["Content-Type"], "application/json");
        assert.equal(options.credentials, "include");
        assert.deepEqual(JSON.parse(options.body), body);
        return new Response("{}");
      };
      await api(path, "POST", body);
    }
  }
});

test("allowlist excludes auth, invites, chat, reads and other write routes", async () => {
  const calls = [
    ["/auth/login", "POST"], ["/auth/register", "POST"], ["/auth/logout", "POST"],
    ["/invites", "POST"], ["/invites/accept", "POST"], ["/chat", "POST"],
    ["/spaces", "GET"], ["/spaces", "PUT"], ["/spaces/s", "DELETE"],
    ["/spaces/s/members", "POST"], ["/tasks/t", "PATCH"], ["/reviews/r", "POST"],
    ["/spaces/s/documents/d", "POST"], ["/documents/d/versions/v", "POST"],
    ["/versions/v/share/extra", "POST"], ["/tasks/t/progress/extra", "POST"],
    ["/reviews/r/decision/extra", "POST"], ["/conversations/c", "POST"],
  ];
  for (const [path, method] of calls) {
    globalThis.fetch = async (_url, options) => {
      assert.equal(options.headers["Idempotency-Key"], undefined, `${method} ${path}`);
      return new Response("{}");
    };
    await api(path, method, undefined, { idempotencyKey: "must-not-leak" });
  }
});

test("explicit retries reuse the caller's key and unchanged request body", async () => {
  const body = { name: "协作资料库" };
  const options = { idempotencyKey: "caller-chosen-key" };
  const requests = [];
  globalThis.fetch = async (_url, request) => {
    requests.push(request);
    if (requests.length === 1) throw new TypeError("connection lost");
    return new Response(JSON.stringify({ id: "space" }));
  };
  await assert.rejects(api("/spaces", "POST", body, options), /connection lost/);
  assert.equal(requests.length, 1, "network failures must not trigger automatic retries");
  assert.deepEqual(await api("/spaces", "POST", body, options), { id: "space" });
  assert.equal(requests.length, 2);
  for (const request of requests) {
    assert.equal(request.headers["Idempotency-Key"], options.idempotencyKey);
    assert.equal(request.body, JSON.stringify(body));
  }
});

test("automatic keys do not silently retry failed writes", async () => {
  let calls = 0;
  globalThis.fetch = async (_url, options) => {
    calls++;
    assert.ok(options.headers["Idempotency-Key"]);
    throw new TypeError("network unavailable");
  };
  await assert.rejects(api("/tasks", "POST", { title: "task" }), /network unavailable/);
  assert.equal(calls, 1);
});

test("same-key conflicts from the server remain errors rather than fake success", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "idempotency key body mismatch" }), { status: 409 });
  await assert.rejects(api("/spaces", "POST", { name: "different" }, { idempotencyKey: "used-key" }), error => error instanceof ApiError && error.status === 409);
});

test("list decoding accepts actual arrays and explicit wrappers, rejects malformed payload", () => {
  assert.deepEqual(listOf([], "spaces"), []);
  assert.deepEqual(listOf({ spaces: [{ id: "s" }] }, "spaces"), [{ id: "s" }]);
  assert.throws(() => listOf({ ok: true }, "spaces"), /列表格式/);
});
