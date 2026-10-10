import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Empty, Loading, StatusBadge } from "../components/bits";
import { ErrorNotice } from "../components/ErrorNotice";
import { api, unwrap, useLoad } from "../lib/api";
import { dateTime, shortId } from "../lib/format";
import { useConnection } from "./ConnectionLayout";

export function RunsPage() {
  const { connection } = useConnection();
  const navigate = useNavigate();
  const runs = useLoad(
    () => unwrap(api.GET("/api/connections/{connection_id}/runs", { params: { path: { connection_id: connection.id } } })),
    [connection.id],
  );
  const [picked, setPicked] = useState<string[]>([]);

  if (runs.error) return <ErrorNotice error={runs.error} />;
  if (!runs.data) return <Loading what="Runs" />;
  const newestFirst = [...runs.data].reverse();

  const pick = (id: string) =>
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p.slice(-1), id]));
  const compare = () => {
    // Older first, whichever order they were picked in.
    const [before, after] = runs.data!.filter((r) => picked.includes(r.id)).map((r) => r.id);
    navigate(`/c/${connection.id}/runs/compare?before=${before}&after=${after}`);
  };

  return (
    <div className="page">
      <div className="section-head">
        <h2>Runs</h2>
        <button type="button" className="button-secondary" disabled={picked.length !== 2} onClick={compare}>
          Compare {picked.length === 2 ? "2 Runs" : "two Runs"}
        </button>
      </div>
      {newestFirst.length === 0 ? (
        <Empty>No Runs yet. Start one with <strong>Analyze</strong>, or ask in chat.</Empty>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th className="col-check">
                <span className="sr-only">Compare</span>
              </th>
              <th>Started</th>
              <th>Run</th>
              <th>Status</th>
              <th>Measured</th>
              <th>From</th>
            </tr>
          </thead>
          <tbody>
            {newestFirst.map((r) => (
              <tr key={r.id}>
                <td className="col-check">
                  <input
                    type="checkbox"
                    checked={picked.includes(r.id)}
                    onChange={() => pick(r.id)}
                    aria-label={`Compare Run ${shortId(r.id)}`}
                  />
                </td>
                <td>{dateTime(r.started_at)}</td>
                <td>
                  <Link to={r.id}>
                    <code>{shortId(r.id)}</code>
                  </Link>
                </td>
                <td>
                  <StatusBadge status={r.status} />
                </td>
                <td>
                  {Object.entries(r.scope)
                    .map(([analyzer, refs]) => `${analyzer}: ${refs.length} collections`)
                    .join(", ") || <span className="muted">nothing</span>}
                  {Object.values(r.skipped).some((s) => s.length > 0) && (
                    <span className="text-warn">
                      {" "}
                      · {Object.values(r.skipped).reduce((n, s) => n + s.length, 0)} skipped
                    </span>
                  )}
                </td>
                <td>
                  {r.thread_id ? (
                    <Link to={`/c/${connection.id}/chat/${r.thread_id}`}>Thread {shortId(r.thread_id)}</Link>
                  ) : (
                    <span className="muted">Analyze / CLI</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
