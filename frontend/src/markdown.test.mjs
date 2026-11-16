import assert from "node:assert/strict";
import test from "node:test";
import { parseInline, parseMarkdown, safeHref } from "./markdown.ts";

test("table preserves native table data, alignment and inline formatting", () => {
  const [table] = parseMarkdown("| 书名 | 数量 |\n| :--- | ---: |\n| **图书** | 2 |\n| [目录](https://example.org) | 3 |");
  assert.equal(table.type, "table");
  assert.equal(table.header.length, 2);
  assert.equal(table.rows.length, 2);
  assert.deepEqual(table.align, ["left", "right"]);
  assert.equal(table.rows[0][0][0].type, "strong");
  assert.equal(table.rows[1][0][0].type, "link");
});

test("table handles escaped pipes and inline code pipes", () => {
  const [table] = parseMarkdown("A | B\n--- | ---\none\\|two | `a|b`");
  assert.deepEqual(table.rows[0], [
    [{ type: "text", text: "one|two" }],
    [{ type: "code", text: "a|b" }],
  ]);
});

test("table rows are padded and excess cells discarded", () => {
  const [table] = parseMarkdown("| A | B |\n| --- | --- |\n| one |\n| two | three | extra |");
  assert.deepEqual(table.rows[0][1], []);
  assert.equal(table.rows[1].length, 2);
});

test("invalid table delimiters remain ordinary paragraph text", () => {
  assert.equal(parseMarkdown("A | B\n-- | ---")[0].type, "paragraph");
  assert.equal(parseMarkdown("A | B\n---")[0].type, "paragraph");
});

test("headings, paragraphs, lists and fenced code render as distinct blocks", () => {
  const blocks = parseMarkdown("# 标题\n\n第一行\n第二行\n\n- 条目一\n- 条目二\n\n3. 第三项\n4. 第四项\n\n```js\nconst x = '<script>';\n```\n\n结束");
  assert.deepEqual(blocks.map((block) => block.type), ["heading", "paragraph", "list", "list", "code", "paragraph"]);
  assert.equal(blocks[0].level, 1);
  assert.equal(blocks[1].children[0].text, "第一行\n第二行");
  assert.equal(blocks[2].items.length, 2);
  assert.equal(blocks[3].start, 3);
  assert.equal(blocks[4].language, "js");
  assert.equal(blocks[4].text, "const x = '<script>';");
});

test("unterminated fence consumes remaining input as literal code", () => {
  assert.deepEqual(parseMarkdown("~~~html\n<script>alert(1)</script>"), [
    { type: "code", language: "html", text: "<script>alert(1)</script>" },
  ]);
});

test("raw HTML is text, never an executable AST node", () => {
  const source = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
  assert.deepEqual(parseMarkdown(source), [{ type: "paragraph", children: [{ type: "text", text: source }] }]);
});

test("unsafe URL schemes and obfuscated schemes are rejected", () => {
  for (const url of ["javascript:alert", "JaVaScRiPt:alert", "data:text/html,payload", "vbscript:payload", "java\tscript:alert", "java\nscript:alert", "file:///etc/passwd"]) {
    assert.equal(safeHref(url), undefined, url);
  }
  assert.deepEqual(parseInline("[危险](javascript:alert)"), [{ type: "text", text: "危险" }]);
});

test("safe external, internal, fragment and email links retain their href", () => {
  for (const href of ["https://example.org/a", "http://example.org", "//example.org", "/catalog", "../help", "#借阅", "mailto:library@example.org"]) {
    assert.equal(safeHref(href), href);
    assert.equal(parseInline(`[链接](${href})`)[0].href, href);
  }
});

test("CRLF input and malformed inline syntax remain readable", () => {
  assert.deepEqual(parseMarkdown("第一段\r\n\r\n第二段").map((block) => block.type), ["paragraph", "paragraph"]);
  assert.deepEqual(parseInline("**未闭合 [链接]( `代码"), [{ type: "text", text: "**未闭合 [链接]( `代码" }]);
});
