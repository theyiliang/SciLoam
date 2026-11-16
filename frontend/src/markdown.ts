// Deliberately small Markdown subset, not a full CommonMark/GFM implementation.
// Raw HTML is plain text; consumers must render nodes without HTML injection.
export type Inline =
  | { type: "text" | "code"; text: string }
  | { type: "strong"; children: Inline[] }
  | { type: "link"; href: string; children: Inline[] };

export type Block =
  | { type: "paragraph"; children: Inline[] }
  | { type: "heading"; level: number; children: Inline[] }
  | { type: "code"; text: string; language: string }
  | { type: "list"; ordered: boolean; start: number; items: Inline[][] }
  | { type: "table"; header: Inline[][]; rows: Inline[][][]; align: ("left" | "center" | "right" | undefined)[] };

export function safeHref(href: string): string | undefined {
  const value = href.trim();
  // Reject control/space-obfuscated schemes and permit only known safe schemes.
  if (/[\u0000- \u007f]/.test(value)) return undefined;
  const scheme = /^([a-z][a-z\d+.-]*):/i.exec(value);
  if (scheme && !/^(https?|mailto)$/i.test(scheme[1])) return undefined;
  return value;
}

export function parseInline(text: string, depth = 0): Inline[] {
  if (depth > 12) return [{ type: "text", text }];
  const nodes: Inline[] = [];
  let plain = "";
  const flush = () => {
    if (plain) nodes.push({ type: "text", text: plain });
    plain = "";
  };
  for (let index = 0; index < text.length;) {
    const rest = text.slice(index);
    const escaped = /^\\([\\`*{}[\]()#+.!_|>~-])/.exec(rest);
    if (escaped) {
      plain += escaped[1];
      index += escaped[0].length;
      continue;
    }
    const code = /^`([^`\n]+)`/.exec(rest);
    if (code) {
      flush();
      nodes.push({ type: "code", text: code[1] });
      index += code[0].length;
      continue;
    }
    const strong = /^(\*\*|__)(?=\S)(.+?)\1/.exec(rest);
    if (strong) {
      flush();
      nodes.push({ type: "strong", children: parseInline(strong[2], depth + 1) });
      index += strong[0].length;
      continue;
    }
    const link = /^\[([^\]\n]+)\]\(([^\s()]*)\)/.exec(rest);
    if (link) {
      const href = safeHref(link[2]);
      flush();
      if (href !== undefined) {
        nodes.push({ type: "link", href, children: parseInline(link[1], depth + 1) });
      } else {
        nodes.push({ type: "text", text: link[1] });
      }
      index += link[0].length;
      continue;
    }
    plain += text[index];
    index += 1;
  }
  flush();
  return nodes;
}

function cells(line: string): string[] {
  let source = line.trim();
  if (source.startsWith("|")) source = source.slice(1);
  if (source.endsWith("|") && !source.endsWith("\\|")) source = source.slice(0, -1);
  const result: string[] = [];
  let cell = "";
  let inCode = false;
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (char === "\\" && index + 1 < source.length) {
      cell += char + source[index + 1];
      index += 1;
    } else if (char === "`") {
      inCode = !inCode;
      cell += char;
    } else if (char === "|" && !inCode) {
      result.push(cell.trim());
      cell = "";
    } else {
      cell += char;
    }
  }
  result.push(cell.trim());
  return result;
}

function tableAlignment(header: string, delimiter: string) {
  if (!header.includes("|")) return undefined;
  const values = cells(delimiter);
  if (values.length !== cells(header).length || !values.every((value) => /^:?-{3,}:?$/.test(value))) {
    return undefined;
  }
  return values.map((value): "left" | "center" | "right" | undefined => {
    if (value.startsWith(":") && value.endsWith(":")) return "center";
    if (value.endsWith(":")) return "right";
    if (value.startsWith(":")) return "left";
    return undefined;
  });
}

const fencePattern = /^ {0,3}(`{3,}|~{3,})(.*)$/;
const headingPattern = /^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$/;
const listPattern = /^ {0,3}(?:([-+*])|(\d+)[.)])\s+(.+)$/;

export function parseMarkdown(text: string): Block[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let index = 0;
  const startsBlock = (at: number) =>
    fencePattern.test(lines[at]) || headingPattern.test(lines[at]) || listPattern.test(lines[at]) ||
    (at + 1 < lines.length && tableAlignment(lines[at], lines[at + 1]) !== undefined);

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const fence = fencePattern.exec(line);
    if (fence) {
      const closing = new RegExp(`^ {0,3}${fence[1][0]}{${fence[1].length},}\\s*$`);
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !closing.test(lines[index])) code.push(lines[index++]);
      if (index < lines.length) index += 1;
      blocks.push({ type: "code", language: fence[2].trim().split(/\s+/)[0], text: code.join("\n") });
      continue;
    }
    const heading = headingPattern.exec(line);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, children: parseInline(heading[2]) });
      index += 1;
      continue;
    }
    const align = index + 1 < lines.length ? tableAlignment(line, lines[index + 1]) : undefined;
    if (align) {
      const header = cells(line).map((cell) => parseInline(cell));
      const rows: Inline[][][] = [];
      index += 2;
      while (index < lines.length && lines[index].trim() && lines[index].includes("|") &&
        !fencePattern.test(lines[index]) && !headingPattern.test(lines[index]) && !listPattern.test(lines[index])) {
        const values = cells(lines[index++]);
        rows.push(header.map((_, column) => parseInline(values[column] ?? "")));
      }
      blocks.push({ type: "table", header, rows, align });
      continue;
    }
    const list = listPattern.exec(line);
    if (list) {
      const ordered = list[2] !== undefined;
      const items: Inline[][] = [];
      while (index < lines.length) {
        const item = listPattern.exec(lines[index]);
        if (!item || (item[2] !== undefined) !== ordered) break;
        items.push(parseInline(item[3]));
        index += 1;
      }
      blocks.push({ type: "list", ordered, start: ordered ? Number(list[2]) : 1, items });
      continue;
    }
    const paragraph = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !startsBlock(index)) {
      paragraph.push(lines[index++]);
    }
    blocks.push({ type: "paragraph", children: parseInline(paragraph.join("\n")) });
  }
  return blocks;
}
