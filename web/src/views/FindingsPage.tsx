import { useState } from "react";
import { Link } from "react-router-dom";
import type { Schemas } from "../api/client";
import { CopyButton, Empty, Evidence, Loading, SeverityBadge, StatusBadge, type Severity } from "../components/bits";
import { ErrorNotice } from "../components/ErrorNotice";
import { api, unwrap, useLoad } from "../lib/api";
import { shortId } from "../lib/format";
import { useConnection } from "./ConnectionLayout";

type FindingView = Schemas["FindingView"];
type FindingStatus = Schemas["Finding"]["status"];
/** The statuses an engineer can set; obsolete is the store's to set. */
type SettableStatus = Schemas["StatusIn"]["status"];

const SEVERITY_ORDER: Severity[] = ["high", "medium", "low", "info"];
const ALL_STATUSES = ["open", "acknowledged", "fixed", "obsolete"] as const satisfies readonly FindingStatus[];

/** A Finding a covering Run no longer saw: it asks "fixed?" until the engineer answers. */
function asksFixed(f: Schemas["Finding"]): boolean {
  return Boolean(f.unobserved_by) && (f.status === "open" || f.status === "acknowledged");
}

export function FindingsPage() {
  const { connection } = useConnection();
  const [showObsolete, setShowObsolete] = useState(false);
  const findings = useLoad(
    () =>
      unwrap(
        api.GET("/api/connections/{connection_id}/findings", {
          params: {
            path: { connection_id: connection.id },
            // Without a status the API leaves obsolete Findings out.
            query: showObsolete ? { status: [...ALL_STATUSES] } : {},
          },
        }),
      ),
    [connection.id, showObsolete],
  );
  const [category, setCategory] = useState("");
  const [severity, setSeverity] = useState("");
  const [status, setStatus] = useState("");
  const [open, setOpen] = useState<string>();
  const [error, setError] = useState<unknown>();

  if (findings.error) return <ErrorNotice error={findings.error} />;
  if (!findings.data) return <Loading what="Findings" />;
  const all = findings.data;

  const setFindingStatus = async (fingerprint: string, next: SettableStatus) => {
    setError(undefined);
    try {
      const updated = await unwrap(
        api.POST("/api/connections/{connection_id}/findings/status", {
          params: { path: { connection_id: connection.id } },
          body: { fingerprint, status: next },
        }),
      );
      findings.set((current) =>
  current?.map((v) => (v.finding.fingerprint === fingerprint ? { ...v, finding: updated } : v)),
);
    } catch (e) {
      setError(e);
    }
  };

  const shown = all
    .filter(
      (v) =>
        (!category || v.finding.category === category) &&
        (!severity || v.latest.severity === severity) &&
        (!status || v.finding.status === status),
    )
    .sort(
      (a, b) =>
        SEVERITY_ORDER.indexOf(a.latest.severity) - SEVERITY_ORDER.indexOf(b.latest.severity) ||
        a.finding.fingerprint.localeCompare(b.finding.fingerprint),
    );
  const fixedPrompts = all.filter((v) => asksFixed(v.finding));
  const categories = [...new Set(all.map((v) => v.finding.category))].sort();

  return (
    <div className="page stack">
      <div className="section-head">
        <h2>Findings</h2>
        <span className="muted">
          {shown.length} of {all.length}
        </span>
      </div>

      {fixedPrompts.length > 0 && (
        <section className="fixed-prompts" aria-label="Fixed?">
          <h3>Fixed? ({fixedPrompts.length})</h3>
          <p className="muted">A Run that covered these didn't see them again. Confirm, or reopen if they're still there.</p>
          <ul className="plain">
            {fixedPrompts.map((v) => (
              <li key={v.finding.fingerprint} className="fixed-prompt">
                <SeverityBadge severity={v.latest.severity} />
                <span className="grow">
                  {v.latest.title}{" "}
                  <span className="muted">
                    · not seen by{" "}
                    <Link to={`/c/${connection.id}/runs/${v.finding.unobserved_by}`}>Run {shortId(v.finding.unobserved_by ?? "")}</Link>
                  </span>
                </span>
                <button type="button" className="button-small" onClick={() => void setFindingStatus(v.finding.fingerprint, "fixed")}>
                  Confirm fixed
                </button>
                <button type="button" className="button-small button-secondary" onClick={() => void setFindingStatus(v.finding.fingerprint, "open")}>
                  Reopen
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="filters" role="group" aria-label="Filters">
        <Select label="Category" value={category} onChange={setCategory} options={categories} />
        <Select label="Severity" value={severity} onChange={setSeverity} options={SEVERITY_ORDER} />
        <Select label="Status" value={status} onChange={setStatus} options={showObsolete ? [...ALL_STATUSES] : ALL_STATUSES.slice(0, 3)} />
        <label className="check check-compact">
          <input
            type="checkbox"
            checked={showObsolete}
            onChange={(e) => {
              setShowObsolete(e.target.checked);
              if (!e.target.checked && status === "obsolete") setStatus("");
            }}
          />
          <span>Show obsolete</span>
        </label>
      </div>
      <ErrorNotice error={error} title="The status didn't change" />

      {all.length === 0 ? (
        <Empty>No Findings yet. Start a Run with <strong>Analyze</strong> to measure this database.</Empty>
      ) : shown.length === 0 ? (
        <Empty>No Findings match these filters.</Empty>
      ) : (
        <ul className="findings">
          {shown.map((v) => (
            <FindingRow
              key={v.finding.fingerprint}
              view={v}
              expanded={open === v.finding.fingerprint}
              onToggle={() => setOpen(open === v.finding.fingerprint ? undefined : v.finding.fingerprint)}
              onStatus={(next) => void setFindingStatus(v.finding.fingerprint, next)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function Select({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: readonly string[] }) {
  return (
    <label className="field field-inline">
      <span>{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">All</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o.replaceAll("_", " ")}
          </option>
        ))}
      </select>
    </label>
  );
}

function FindingRow({
  view,
  expanded,
  onToggle,
  onStatus,
}: {
  view: FindingView;
  expanded: boolean;
  onToggle: () => void;
  onStatus: (next: SettableStatus) => void;
}) {
  const { finding: f, latest } = view;
  const fixedPrompt = asksFixed(f);
  return (
    <li className={`finding${fixedPrompt ? " finding-asks" : ""}${expanded ? " is-open" : ""}`}>
      <button type="button" className="finding-summary" onClick={onToggle} aria-expanded={expanded}>
        <SeverityBadge severity={latest.severity} />
        <span className="finding-title">{latest.title}</span>
        <span className="finding-cat">{f.category.replaceAll("_", " ")}</span>
        <StatusBadge status={f.status} />
        {fixedPrompt && <span className="tag tag-warn">fixed?</span>}
      </button>
      {expanded && (
        <div className="finding-body stack">
          <p className="muted">
            <code className="fingerprint">{f.fingerprint}</code> · first seen in Run {shortId(f.first_seen_run)} · last seen in Run{" "}
            {shortId(f.last_seen_run)}
          </p>
          {latest.recommendation && (
            <div className="stack-sm">
              <div className="row-center">
                <h4>Recommendation</h4>
                <CopyButton text={latest.recommendation} label="Copy" />
              </div>
              <p>{latest.recommendation}</p>
            </div>
          )}
          {latest.ddl && (
            <div className="stack-sm">
              <div className="row-center">
                <h4>DDL</h4>
                <CopyButton text={latest.ddl} label="Copy DDL" />
              </div>
              <pre className="code-block">{latest.ddl}</pre>
            </div>
          )}
          <Evidence evidence={latest.evidence} />
          {f.status !== "obsolete" && (
            <div className="row-center">
              {f.status !== "acknowledged" && (
                <button type="button" className="button-small button-secondary" onClick={() => onStatus("acknowledged")}>
                  Acknowledge
                </button>
              )}
              {f.status !== "fixed" && (
                <button type="button" className="button-small button-secondary" onClick={() => onStatus("fixed")}>
                  Mark fixed
                </button>
              )}
              {(f.status !== "open" || fixedPrompt) && (
                <button type="button" className="button-small button-secondary" onClick={() => onStatus("open")}>
                  Reopen
                </button>
              )}
            </div>
          )}
          <History fingerprint={f.fingerprint} />
        </div>
      )}
    </li>
  );
}

/** One Observation per Run that saw the Finding, oldest first. */
function History({ fingerprint }: { fingerprint: string }) {
  const { connection } = useConnection();
  const observations = useLoad(
    () =>
      unwrap(
        api.GET("/api/connections/{connection_id}/observations", {
          params: { path: { connection_id: connection.id }, query: { fingerprint } },
        }),
      ),
    [connection.id, fingerprint],
  );
  if (observations.error) return <ErrorNotice error={observations.error} />;
  if (!observations.data) return <Loading what="Observations" />;
  return (
    <div className="stack-sm">
      <h4>Observations</h4>
      <ol className="observations">
        {observations.data.map((o) => (
          <li key={o.run_id}>
            <Link to={`/c/${connection.id}/runs/${o.run_id}`}>Run {shortId(o.run_id)}</Link> <SeverityBadge severity={o.severity} />{" "}
            {o.title}
            <details>
              <summary className="muted">Evidence</summary>
              <Evidence evidence={o.evidence} />
            </details>
          </li>
        ))}
      </ol>
    </div>
  );
}
