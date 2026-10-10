// Small presentational pieces shared by several views.
import { useEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Schemas } from "../api/client";
import { cost } from "../lib/format";

export type Severity = Schemas["Observation"]["severity"];
export type FindingStatus = Schemas["Finding"]["status"];
export type RunStatus = Schemas["Run"]["status"];

export function SeverityBadge({ severity }: { severity: Severity }) {
  return <span className={`badge sev-${severity}`}>{severity}</span>;
}

export function StatusBadge({ status }: { status: FindingStatus | RunStatus }) {
  return <span className={`badge status-${status}`}>{status.replace("_", " ")}</span>;
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return <p className="muted loading">Loading {what}…</p>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/**
 * How close a statement's plan cost came to the EXPLAIN gate's limit: the progress trace's
 * signature. Past 100% the gate refuses the statement.
 */
export function GateMeter({ value, limit }: { value: number | null; limit: number | undefined }) {
  if (value === null || !limit) return null;
  const share = value / limit;
  const level = share > 1 ? "over" : share > 0.5 ? "near" : "ok";
  return (
    <span className={`gate gate-${level}`} title={`plan cost ${cost(value)} of the gate's ${cost(limit)}`}>
      <span className="gate-track">
        <span className="gate-fill" style={{ width: `${Math.min(share, 1) * 100}%` }} />
      </span>
      <span className="gate-label">
        {cost(value)} / {cost(limit)}
      </span>
    </span>
  );
}

/** A modal dialog on the platform's <dialog>: Escape and the close button both close it. */
export function Dialog({
  title,
  open,
  onClose,
  keepMounted = false,
  children,
}: {
  title: string;
  open: boolean;
  onClose: () => void;
  /** Keep the children (and their state) while closed. */
  keepMounted?: boolean;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);
  return (
    <dialog ref={ref} className="dialog" onClose={onClose} aria-label={title}>
      <header className="dialog-head">
        <h2>{title}</h2>
        <button type="button" className="button-ghost" onClick={onClose} aria-label="Close">
          ✕
        </button>
      </header>
      {(open || keepMounted) && children}
    </dialog>
  );
}

export function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="button-small button-secondary"
      onClick={() =>
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        })
      }
    >
      {copied ? "Copied" : label}
    </button>
  );
}

/** A Finding's or a table's evidence: whatever keys the analyzer recorded, as a definition list. */
export function Evidence({ evidence }: { evidence: Record<string, unknown> }) {
  const entries = Object.entries(evidence);
  if (entries.length === 0) return null;
  return (
    <dl className="evidence">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt>{key.replaceAll("_", " ")}</dt>
          <dd>{typeof value === "object" && value !== null ? JSON.stringify(value) : String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}
