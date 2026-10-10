import { useState } from "react";
import { useParams } from "react-router-dom";
import type { ReportFormat, Schemas } from "../api/client";
import { Loading, SeverityBadge, StatusBadge } from "../components/bits";
import { ErrorNotice } from "../components/ErrorNotice";
import { api, download, unwrap, useLoad } from "../lib/api";
import { bytes, count, dateTime, duration, ms, percent, qualified, shortId, truncate } from "../lib/format";

/** Tables shown before "Show all". */
const LARGEST_SHOWN = 15;

/** One Run as the reports show it: ranked problems, facts, sizes, the workload, what was skipped. */
export function RunPage() {
  const { runId = "" } = useParams();
  const view = useLoad(() => unwrap(api.GET("/api/runs/{run_id}", { params: { path: { run_id: runId } } })), [runId]);

  if (view.error) return <ErrorNotice error={view.error} />;
  if (!view.data) return <Loading what="the Run" />;
  const { run, observations, storage, workload, skipped } = view.data;
  const problems = observations.filter((o) => o.severity !== "info");
  const facts = observations.filter((o) => o.severity === "info");
  // From the view, not the Run's scope: a workload that ranked nothing measured no collection.
  const sections = [...(storage ? ["inventory"] : []), ...(workload ? ["workload"] : [])];

  return (
    <div className="page stack-lg">
      <header className="section-head">
        <div>
          <p className="eyebrow">Run {shortId(run.id)}</p>
          <h2 className="row-center">
            {sections.join(" + ") || "Run"} <StatusBadge status={run.status} />
          </h2>
          <p className="muted">
            {dateTime(run.started_at)}
            {run.finished_at && ` → ${dateTime(run.finished_at)}`}
            {run.thread_id && " · started from chat"}
          </p>
        </div>
        <ExportButtons runId={run.id} />
      </header>

      {run.status === "failed" && (
        <div className="notice notice-danger">
          This Run failed part way. What it measured before failing is below; don't read it as a clean Run.
        </div>
      )}

      <section className="stack-sm">
        <h3>Problems ({problems.length})</h3>
        {problems.length === 0 ? (
          <p className="muted">No problems seen in this Run's scope.</p>
        ) : (
          <ol className="ranked">
            {problems.map((o) => (
              <li key={o.fingerprint}>
                <SeverityBadge severity={o.severity} /> <span>{o.title}</span>{" "}
                <code className="fingerprint">{o.fingerprint}</code>
              </li>
            ))}
          </ol>
        )}
      </section>

      {facts.length > 0 && (
        <section className="stack-sm">
          <h3>Facts</h3>
          <ul className="plain">
            {facts.map((o) => (
              <li key={o.fingerprint}>{o.title}</li>
            ))}
          </ul>
        </section>
      )}

      {storage && <Sizes storage={storage} />}
      {workload && <Workload workload={workload} />}

      {skipped.length > 0 && (
        <section className="stack-sm">
          <h3>Skipped</h3>
          <ul className="plain skipped">
            {skipped.map((s, i) => (
              <li key={i}>
                <code>{s.collection}</code>{" "}
                <span className="text-warn">{s.measurement ? s.measurement.replaceAll("_", " ") : "collection"} skipped</span>
                <span className="muted"> ({s.analyzer}): </span>
                {s.reason}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function ExportButtons({ runId }: { runId: string }) {
  const [error, setError] = useState<unknown>();
  const save = (format: ReportFormat) => {
    setError(undefined);
    download(runId, format).catch(setError);
  };
  return (
    <div className="stack-sm">
      <div className="row-center">
        <button type="button" className="button-secondary" onClick={() => save("md")}>
          Download Markdown
        </button>
        <button type="button" className="button-secondary" onClick={() => save("json")}>
          Download JSON
        </button>
      </div>
      <ErrorNotice error={error} />
    </div>
  );
}

function Sizes({ storage }: { storage: Schemas["StorageStats"][] }) {
  const [all, setAll] = useState(false);
  const shown = all ? storage : storage.slice(0, LARGEST_SHOWN);
  return (
    <section className="stack-sm">
      <h3>Largest of {storage.length} tables</h3>
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th className="num">#</th>
              <th>Table</th>
              <th className="num">Total</th>
              <th className="num">Heap</th>
              <th className="num">Indexes</th>
              <th className="num">TOAST</th>
              <th className="num">Rows</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((s, i) => (
              <tr key={qualified(s.ref)}>
                <td className="num muted">{i + 1}</td>
                <td>
                  <code>{qualified(s.ref)}</code>
                  {s.partitions ? <span className="muted"> · {s.partitions} partitions</span> : null}
                </td>
                <td className="num">{bytes(s.total_bytes)}</td>
                <td className="num">{bytes(s.data_bytes)}</td>
                <td className="num">{bytes(s.index_bytes)}</td>
                <td className="num">{bytes(s.toast_bytes)}</td>
                <td className="num">
                  {s.row_count === null ? "unknown" : count(s.row_count)}
                  {s.row_count !== null && <span className="muted"> {s.row_count_method}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {storage.length > LARGEST_SHOWN && (
        <button type="button" className="button-link" onClick={() => setAll(!all)}>
          {all ? `Show the largest ${LARGEST_SHOWN}` : `Show all ${storage.length}`}
        </button>
      )}
    </section>
  );
}

function Workload({ workload: w }: { workload: Schemas["WorkloadReport"] }) {
  return (
    <section className="stack-sm">
      <h3>
        Workload{" "}
        {w.source && (
          <span className="muted">
            · <code>{w.source}</code>
          </span>
        )}
      </h3>
      {w.warnings.map((warning) => (
        <div key={warning} className="notice notice-warn">
          {warning}
        </div>
      ))}
      {w.source === null && (
        <div className="panel stack-sm">
          <strong>To rank slow statements, enable pg_stat_statements:</strong>
          <ol>
            {w.enable_steps?.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
        </div>
      )}
      {w.refused && !w.warnings.includes(w.refused) && <div className="notice notice-warn">Not ranked: {w.refused}</div>}
      {w.source && (
        <p className="muted">
          {count(w.statements)} statements over {duration(w.window_seconds)}
          {w.stats_reset && `, since ${dateTime(w.stats_reset)}`}
          {Object.entries(w.excluded).map(([why, n]) => ` · ${n} left out with ${why}`)}
        </p>
      )}
      {w.items.length > 0 && (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th className="num">#</th>
                <th>Statement</th>
                <th className="num">Calls</th>
                <th className="num">Total</th>
                <th className="num">Mean</th>
                <th className="num">Share</th>
              </tr>
            </thead>
            <tbody>
              {w.items.map((item, rank) => (
                <tr key={item.fingerprint}>
                  <td className="num muted">{rank + 1}</td>
                  <td>
                    <details className="statement">
                      <summary>
                        <code>{truncate(item.text, 90)}</code>
                      </summary>
                      <pre>{item.text}</pre>
                      <p className="muted">
                        fingerprint <code>{item.fingerprint}</code> · ranked by {item.ranked_by.join(", ")} · {count(item.rows)} rows ·{" "}
                        {count(item.shared_blks_read)} blocks read · {count(item.temp_blks_written)} temp blocks written
                      </p>
                    </details>
                  </td>
                  <td className="num">{count(item.calls)}</td>
                  <td className="num">{ms(item.total_ms)}</td>
                  <td className="num">{ms(item.mean_ms)}</td>
                  <td className="num">{percent(item.share_of_time)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
