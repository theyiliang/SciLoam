// Migrated from the removed library dual-channel helper to active research rendering.
// This tests render contracts only, not browser chat behavior or an event bus.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createRequire } from "node:module";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import { parseMarkdown } from "./markdown.ts";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("./components/MarkdownMessage.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 },
}).outputText;
const module = { exports: {} };
new Function("require", "module", "exports", compiled)(
  name => name === "../markdown" ? { parseMarkdown } : require(name), module, module.exports,
);
const MarkdownMessage = module.exports.default;
const render = text => renderToStaticMarkup(createElement(MarkdownMessage, { text }));

test("research answer renders a semantic table with focusable overflow container", () => {
  const html = render("| 方法 | 指标 |\n| --- | --- |\n| 检索 | 召回率 |");
  assert.equal((html.match(/<table>/g) ?? []).length, 1);
  assert.match(html, /role="region"/);
  assert.match(html, /tabindex="0"/i);
  assert.match(html, /scope="col"/);
  assert.match(html, /<td[^>]*>召回率<\/td>/);
});

test("raw answer HTML is escaped, not injected into the page", () => {
  const html = render('<img src=x onerror="alert(1)">');
  assert.doesNotMatch(html, /<img\b/);
  assert.match(html, /&lt;img/);
});

test("external research source links use safe new-window attributes", () => {
  const html = render("[课题来源](https://example.org/research)");
  assert.match(html, /target="_blank"/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.doesNotMatch(render("[不可信链接](javascript:alert(1))"), /href="javascript:/);
});

test("incomplete Markdown during answering remains renderable", () => {
  for (const text of ["| 指标 |", "**尚未结束", "```python\nprint(1)", "[来源](https://"]) {
    assert.doesNotThrow(() => render(text));
  }
});
