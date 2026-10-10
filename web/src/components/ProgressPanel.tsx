import { Link } from "react-router-dom";
import { count, ms, shortId, truncate, usd } from "../lib/format";
import type { TraceItem, Turn } from "../lib/turns";
import { GateMeter } from "./bits";

/** Each Turn limit as the noun in "the … limit". */
const LIMIT_NOUNS = { tokens: "token", tool_calls: "tool-call", queries: "query" } as const;

/** What one Turn did, live: tools, subagents, every statement and its gate cost, Runs, limits, usage. */
export function ProgressPanel({
  turn,
  connectionId,
  gateLimit,
  threadId,
}: {
  turn: Turn | undefined;
  connectionId: string;
  gateLimit: number | undefined;
  threadId: string;
}) {
  return (
    <aside className="progress" aria-label="Progress" aria-live="polite">
      <h2 className="progress-title">
        Progress
        {turn?.state === "running" && <span className="pulse" aria-label="running" />}
      </h2>
      {!turn ? (
        <p className="muted">
          A Turn's tools, subagents and SQL appear here as they happen. Earlier Turns' statements are in
          the <Link to={`/c/${connectionId}/audit?thread=${threadId}`}>audit log</Link>.
        </p>
      ) : (
        <ol className="trace">
          {turn.trace.map((item, i) => (
            <TraceLine key={i} item={item} connectionId={connectionId} gateLimit={gateLimit} />
          ))}
          {turn.usage && (
            <li className="trace-usage">
              {count(turn.usage.input_tokens)} in ({count(turn.usage.cached_tokens)} cached) ·{" "}
              {count(turn.usage.output_tokens)} out · {usd(turn.usage.cost_usd)}
              <span className="muted"> · {turn.usage.model}</span>
            </li>
          )}
        </ol>
      )}
    </aside>
  );
}

function TraceLine({ item, connectionId, gateLimit }: { item: TraceItem; connectionId: string; gateLimit: number | undefined }) {
  switch (item.kind) {
    case "tool": {
      const state = !item.finished ? "running" : item.finished.ok ? "ok" : "failed";
      return (
        <li className={`trace-item trace-tool depth-${item.depth} is-${state}`}>
          <span className="trace-kind">tool</span>
          <code className="trace-name">{item.name}</code>
          <Args args={item.args} />
          {item.finished && !item.finished.ok && <p className="trace-detail text-danger">{item.finished.summary}</p>}
        </li>
      );
    }
    case "subagent": {
      const state = !item.finished ? "running" : item.finished.ok ? "ok" : "failed";
      return (
        <li className={`trace-item trace-subagent depth-${item.depth} is-${state}`}>
          <span className="trace-kind">subagent</span>
          <strong>{item.name}</strong>
          <span className="muted">{state === "running" ? " working" : state === "ok" ? " finished" : " failed"}</span>
        </li>
      );
    }
    case "sql":
      return (
        <li className={`trace-item trace-sql depth-${item.depth}`}>
          <span className="trace-kind">sql</span>
          <span className="trace-purpose">{item.event.purpose}</span>
          <span className="trace-stats">
            {count(item.event.row_count)} rows · {ms(item.event.duration_ms)}
          </span>
          <GateMeter value={item.event.plan_cost} limit={gateLimit} />
          <Sql sql={item.event.sql} />
        </li>
      );
    case "rejected":
      return (
        <li className={`trace-item trace-rejected depth-${item.depth}`}>
          <span className="trace-kind">refused</span>
          <span className="trace-purpose">{item.event.purpose}</span>
          <p className="trace-detail text-danger">{item.event.reason}</p>
          <Sql sql={item.event.sql} />
        </li>
      );
    case "run":
      return (
        <li className={`trace-item trace-run depth-${item.depth}`}>
          <span className="trace-kind">run</span>
          <Link to={`/c/${connectionId}/runs/${item.runId}`}>Run {shortId(item.runId)}</Link>{" "}
          <span className={`badge status-${item.status}`}>{item.status}</span>
        </li>
      );
    case "limit":
      return (
        <li className="trace-item trace-limit">
          Stopped at the {LIMIT_NOUNS[item.event.limit]} limit ({count(item.event.used)} of {count(item.event.max)}). The
          answer may be incomplete.
        </li>
      );
  }
}

function Args({ args }: { args: Record<string, unknown> }) {
  const entries = Object.entries(args);
  if (entries.length === 0) return null;
  return (
    <span className="trace-args">
      {entries.map(([k, v]) => (
        <span key={k}>
          {k}=<span className="trace-arg">{typeof v === "string" ? v : JSON.stringify(v)}</span>
        </span>
      ))}
    </span>
  );
}

function Sql({ sql }: { sql: string }) {
    return (
    <details className="trace-sql-text">
      <summary>
        <code>{truncate(sql, 80)}</code>
      </summary>
      <pre>{sql.trim()}</pre>
    </details>
  );
}
