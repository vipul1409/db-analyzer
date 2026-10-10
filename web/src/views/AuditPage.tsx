import { Link, useSearchParams } from "react-router-dom";
import { Empty, GateMeter, Loading } from "../components/bits";
import { ErrorNotice } from "../components/ErrorNotice";
import { api, unwrap, useLoad } from "../lib/api";
import { ago, count, dateTime, ms, shortId, truncate } from "../lib/format";
import { useConnection } from "./ConnectionLayout";

/** Every statement executed or rejected on the Connection, newest first; optionally one Thread's. */
export function AuditPage() {
  const { connection } = useConnection();
  const [params, setParams] = useSearchParams();
  const threadId = params.get("thread") ?? "";
  const audit = useLoad(
    () =>
      unwrap(
        api.GET("/api/connections/{connection_id}/audit", {
          params: { path: { connection_id: connection.id }, query: threadId ? { thread_id: threadId } : {} },
        }),
      ),
    [connection.id, threadId],
  );
  const threads = useLoad(
    () => unwrap(api.GET("/api/connections/{connection_id}/threads", { params: { path: { connection_id: connection.id } } })),
    [connection.id],
  );

  const entries = audit.data ? [...audit.data].reverse() : undefined;

  return (
    <div className="page stack">
      <div className="section-head">
        <h2>Audit</h2>
        <label className="field field-inline">
          <span>Thread</span>
          <select value={threadId} onChange={(e) => setParams(e.target.value ? { thread: e.target.value } : {})}>
            <option value="">All statements</option>
            {threads.data?.map((t) => (
              <option key={t.id} value={t.id}>
                {shortId(t.id)} · {t.preview ? t.preview.slice(0, 40) : "no messages"} · {ago(t.last_active_at ?? t.created_at)}
              </option>
            ))}
          </select>
        </label>
      </div>
      {threadId && (
        <p className="muted">
          Statements of <Link to={`/c/${connection.id}/chat/${threadId}`}>Thread {shortId(threadId)}</Link>.
        </p>
      )}
      <ErrorNotice error={audit.error} />
      {!entries ? (
        !audit.error && <Loading what="the audit log" />
      ) : entries.length === 0 ? (
        <Empty>Nothing has touched this database through the analyzer{threadId ? " in this Thread" : ""} yet.</Empty>
      ) : (
        <div className="table-scroll">
          <table className="table audit">
            <thead>
              <tr>
                <th>At</th>
                <th>Decision</th>
                <th>Purpose</th>
                <th>SQL</th>
                <th>Plan</th>
                <th className="num">Rows</th>
                <th className="num">Duration</th>
                <th>Thread</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e, i) => (
                <tr key={i} className={`decision-${e.decision}`}>
                  <td className="nowrap">{dateTime(e.at)}</td>
                  <td>
                    <span className={`badge decision decision-${e.decision}`}>{e.decision}</span>
                    {e.reason && <p className="audit-reason">{e.reason}</p>}
                  </td>
                  <td>{e.purpose}</td>
                  <td className="audit-sql">
                    <details>
                      <summary>
                        <code>{truncate(e.sql, 70)}</code>
                      </summary>
                      <pre>{e.sql}</pre>
                    </details>
                  </td>
                  <td className="nowrap">
                    <GateMeter value={e.plan_cost ?? null} limit={connection.gate?.max_total_cost} />
                    {e.plan_rows != null && <span className="muted block">{count(e.plan_rows)} rows planned</span>}
                  </td>
                  <td className="num">{count(e.row_count)}</td>
                  <td className="num">{ms(e.duration_ms)}</td>
                  <td>
                    {e.thread_id ? (
                      <Link to={`?thread=${e.thread_id}`}>{shortId(e.thread_id)}</Link>
                    ) : (
                      <span className="muted">Run</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
