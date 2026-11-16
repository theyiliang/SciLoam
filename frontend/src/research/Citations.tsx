import { useEffect, useRef, useState } from "react";
import MarkdownMessage from "../components/MarkdownMessage";
import { api, errorText, idPath } from "./api";
import type { Citation, Version } from "./types";
import { SourceLink } from "./ui";

export default function Citations({ items }: { items: Citation[] }) {
  const [open, setOpen] = useState<number | null>(null);
  const [versions, setVersions] = useState<Record<string, Version>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const sequence = useRef(0);
  const currentItems = useRef(items);
  currentItems.current = items;
  const [loadedItems, setLoadedItems] = useState(items);
  useEffect(() => {
    const seq = ++sequence.current;
    setOpen(null); setVersions({}); setErrors({}); setLoadedItems(items);
    // Fetch the authorized version, never synthesize a version number from its UUID.
    for (const versionId of new Set(items.map(item => item.version_id).filter((id): id is string => !!id))) {
      void api<Version>(`/versions/${idPath(versionId)}`).then(result => {
        if (seq !== sequence.current || currentItems.current !== items) return;
        if (result.id !== versionId) throw new Error("服务返回的引用版本不一致。");
        setVersions(previous => ({ ...previous, [versionId]: result }));
      }).catch(e => { if (seq === sequence.current && currentItems.current === items) setErrors(previous => ({ ...previous, [versionId]: errorText(e) })); });
    }
    return () => { sequence.current++; };
  }, [items]);
  const ready = loadedItems === items;
  return <div className="rw-chat-citations"><p className="rw-chat-note">参考来源 · {items.length}</p>{items.map((item, index) => {
    const version = ready && item.version_id ? versions[item.version_id] : undefined;
    const error = ready && item.version_id ? errors[item.version_id] : undefined;
    const snippet = item.snippet ?? item.excerpt ?? item.content;
    const number = version?.number ?? item.version_number ?? item.number;
    const start = item.char_start ?? item.chunk_start;
    const end = item.char_end ?? item.chunk_end;
    const found = snippet && version?.content ? version.content.indexOf(snippet) : -1;
    const rangeStart = start ?? (found >= 0 ? found : undefined);
    const rangeEnd = end ?? (found >= 0 && snippet ? found + snippet.length : undefined);
    return <article className="rw-chat-citation" key={`${item.id ?? item.version_id ?? "source"}-${index}`}>
      <span className="rw-chat-citation-number">{index + 1}</span><div>
        <h4>{version?.title || item.title || item.document_title || version?.filename || item.filename || `资料 ${index + 1}`}{number != null ? <span> · v{number}</span> : <span className="rw-muted"> · {error ? "版本暂不可读取" : item.version_id ? "正在核对版本" : "版本未提供"}</span>}</h4>
        {item.page != null && <p className="rw-chat-note">第 {item.page} 页</p>}
        {snippet && <blockquote>{snippet}</blockquote>}
        <p className="rw-chat-note">{rangeStart != null && rangeEnd != null ? `原文字符位置 [${rangeStart}, ${rangeEnd})（从 0 起，末位不含）` : "原文字符位置未提供"}</p>
        <SourceLink source={version?.source ?? item.source ?? item.url} />
        {item.version_id && <button type="button" className="rw-chat-read" aria-expanded={ready && open === index} onClick={() => setOpen(open === index ? null : index)}>{ready && open === index ? "收起原文" : "查看引用原文"}</button>}
        {ready && open === index && <div className="rw-chat-original" aria-live="polite">{error ? <p role="alert">原文读取失败：{error}。请检查知识库权限或重新打开回答依据。</p> : version ? <><h4>{version.title ?? item.title ?? "引用原文"}{version.number != null && ` · v${version.number}`}</h4><MarkdownMessage text={version.content ?? "该版本未返回正文。"} /></> : <p role="status">正在读取此版本原文…</p>}</div>}
        {item.version_id && <details className="rw-chat-technical"><summary>引用技术详情</summary><p>版本 ID：{item.version_id}</p>{item.document_id && <p>知识 ID：{item.document_id}</p>}</details>}
      </div>
    </article>;
  })}</div>;
}
