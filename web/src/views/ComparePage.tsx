import { Link, useSearchParams } from "react-router-dom";
import { Loading } from "../components/bits";
import { ErrorNotice } from "../components/ErrorNotice";
import { api, unwrap, useLoad } from "../lib/api";
import { bytes, shortId, signedBytes } from "../lib/format";
import { useConnection } from "./ConnectionLayout";

/** Two Runs, over the scope both measured: partial Runs never look like regressions. */
export function ComparePage() {
  const { connection } = useConnection();
  const [params] = useSearchParams();
  const before = params.get("before") ?? "";
  const after = params.get("after") ?? "";
  const comparison = useLoad(
    () => unwrap(api.GET("/api/runs/compare", { params: { query: { before, after } } })),
    [before, after],
  );

  if (comparison.error) return <ErrorNotice error={comparison.error} />;
  if (!comparison.data) return <Loading what="the comparison" />;
  const c = comparison.data;
  const notCompared = Object.entries(c.not_compared).filter(([, names]) => names.length > 0);

  return (
    <div className="page stack-lg">
      <header>
        <p className="eyebrow">Comparison</p>
        <h2>
          <Link to={`/c/${connection.id}/runs/${c.before}`}>
            Run {shortId(c.before)}
          </Link>{" "}
          →{" "}
          <Link to={`/c/${connection.id}/runs/${c.after}`}>
            Run {shortId(c.after)}
          </Link>
        </h2>
        <p className="muted">
          Over what both measured:{" "}
          {Object.entries(c.shared)
            .map(([analyzer, names]) => `${names.length} collections (${analyzer})`)
            .join(", ") || "nothing in common"}
          .
        </p>
      </header>

      <div className="split-even">
        <section className="stack-sm">
          <h3>New problems ({c.appeared.length})</h3>
          {c.appeared.length === 0 ? <p className="muted">None.</p> : <Fingerprints list={c.appeared} />}
        </section>
        <section className="stack-sm">
          <h3>Gone ({c.disappeared.length})</h3>
          {c.disappeared.length === 0 ? <p className="muted">None.</p> : <Fingerprints list={c.disappeared} />}
        </section>
      </div>

      <section className="stack-sm">
        <h3>Size changes</h3>
        {c.size_changes.length === 0 ? (
          <p className="muted">No size changes in the shared scope.</p>
        ) : (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>Table</th>
                  <th className="num">Before</th>
                  <th className="num">After</th>
                  <th className="num">Change</th>
                </tr>
              </thead>
              <tbody>
                {c.size_changes.map((s) => (
                  <tr key={s.collection}>
                    <td>
                      <code>{s.collection}</code>
                    </td>
                    <td className="num">{bytes(s.before_bytes)}</td>
                    <td className="num">{bytes(s.after_bytes)}</td>
                    <td className={`num ${s.delta_bytes > 0 ? "text-warn" : s.delta_bytes < 0 ? "text-ok" : "muted"}`}>{signedBytes(s.delta_bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {notCompared.length > 0 && (
        <section className="stack-sm">
          <h3>Not compared</h3>
          <p className="muted">Measured by only one of the two Runs, so left out above.</p>
          {notCompared.map(([analyzer, names]) => (
            <p key={analyzer}>
              <span className="muted">{analyzer}: </span>
              {names.map((n) => (
                <code key={n} className="chip">
                  {n}
                </code>
              ))}
            </p>
          ))}
        </section>
      )}
    </div>
  );
}

function Fingerprints({ list }: { list: string[] }) {
  return (
    <ul className="plain">
      {list.map((f) => (
        <li key={f}>
          <code className="fingerprint">{f}</code>
        </li>
      ))}
    </ul>
  );
}
