import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { safeHref } from "../markdown";
import { errorText } from "./api";

export function useAction() {
  const lock = useRef(false);
  const mounted = useRef(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const run = useCallback(async (fn: () => Promise<void>, success = "") => {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(""); setNotice("");
    try { await fn(); if (mounted.current) setNotice(success); }
    catch (e) { if (mounted.current) setError(errorText(e)); }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  }, []);
  return { busy, error, notice, run, setError, setNotice };
}
export function Feedback({ error, notice }: { error?: string; notice?: string }) {
  return <>{error && <div className="rw-alert rw-error" role="alert">{error}</div>}{notice && <div className="rw-alert rw-success" role="status">{notice}</div>}</>;
}
export function Empty({ children }: { children: ReactNode }) { return <div className="rw-empty">{children}</div>; }
export function SourceLink({ source, children }: { source?: string; children?: ReactNode }) {
  if (!source) return null;
  const href = safeHref(source);
  return href ? <a href={href} target="_blank" rel="noopener noreferrer">{children ?? source} ↗</a> : <span className="rw-muted">{children ?? source}（链接不可用）</span>;
}
export function PanelHeading({ title, subtitle, children }: { title: string; subtitle?: string; children?: ReactNode }) {
  return <header className="rw-panel-heading"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div><div className="rw-actions">{children}</div></header>;
}
export function JsonDetails({ title, value }: { title: string; value: unknown }) {
  return <details className="rw-details"><summary>{title}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>;
}
