import { createElement, type ReactNode } from "react";
import { parseMarkdown, type Inline } from "../markdown";

function renderInline(nodes: Inline[]): ReactNode {
  return nodes.map((node, index) => {
    switch (node.type) {
      case "text":
        return node.text;
      case "code":
        return <code key={index}>{node.text}</code>;
      case "strong":
        return <strong key={index}>{renderInline(node.children)}</strong>;
      case "link": {
        const external = /^(?:https?:)?\/\//i.test(node.href);
        return (
          <a
            key={index}
            href={node.href}
            target={external ? "_blank" : undefined}
            rel={external ? "noopener noreferrer" : undefined}
          >
            {renderInline(node.children)}
          </a>
        );
      }
    }
  });
}

// Safe, intentionally limited Markdown subset; no raw HTML or HTML injection.
export default function MarkdownMessage({ text }: { text: string }) {
  return (
    <div className="markdown-message">
      {parseMarkdown(text).map((block, index) => {
        switch (block.type) {
          case "paragraph":
            return <p key={index}>{renderInline(block.children)}</p>;
          case "heading":
            return createElement(`h${block.level}`, { key: index }, renderInline(block.children));
          case "code":
            return (
              <pre key={index}>
                <code className={block.language ? `language-${block.language}` : undefined}>{block.text}</code>
              </pre>
            );
          case "list": {
            const items = block.items.map((item, itemIndex) => <li key={itemIndex}>{renderInline(item)}</li>);
            return block.ordered
              ? <ol key={index} start={block.start}>{items}</ol>
              : <ul key={index}>{items}</ul>;
          }
          case "table":
            return (
              <div key={index} className="table-scroll" role="region" tabIndex={0} aria-label="可横向滚动的表格">
                <table>
                  <thead>
                    <tr>{block.header.map((cell, column) => (
                      <th key={column} scope="col" style={{ textAlign: block.align[column] }}>{renderInline(cell)}</th>
                    ))}</tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, rowIndex) => (
                      <tr key={rowIndex}>{row.map((cell, column) => (
                        <td key={column} style={{ textAlign: block.align[column] }}>{renderInline(cell)}</td>
                      ))}</tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
        }
      })}
    </div>
  );
}
