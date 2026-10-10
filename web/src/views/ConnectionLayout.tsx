import { createContext, useContext, useState } from "react";
import { NavLink, Outlet, useParams } from "react-router-dom";
import type { Schemas } from "../api/client";
import { Loading } from "../components/bits";
import { ErrorNotice } from "../components/ErrorNotice";
import { api, unwrap, useLoad, type Loaded } from "../lib/api";
import { AnalyzeDialog } from "./AnalyzeDialog";
import { hostLabel, serverRole } from "./ConnectionPage";

interface ConnectionScope {
  connection: Schemas["ConnectionInfo"];
  probe: Loaded<Schemas["ProbeResult"] | null>;
  /** Load the Connection again, after its settings changed. */
  reload: () => void;
}

const ConnectionContext = createContext<ConnectionScope | null>(null);

export function useConnection(): ConnectionScope {
  const scope = useContext(ConnectionContext);
  if (!scope) throw new Error("useConnection outside a ConnectionLayout");
  return scope;
}

const TABS = [
  { to: "", label: "Overview", end: true },
  { to: "chat", label: "Chat", end: false },
  { to: "runs", label: "Runs", end: false },
  { to: "findings", label: "Findings", end: false },
  { to: "audit", label: "Audit", end: false },
];

/** One Connection's pages: its header, the primary banner, the tabs and the Analyze action. */
export function ConnectionLayout() {
  const { connectionId = "" } = useParams();
  const connection = useLoad(
    () => unwrap(api.GET("/api/connections/{connection_id}", { params: { path: { connection_id: connectionId } } })),
    [connectionId],
  );
  const probe = useLoad(
    () => unwrap(api.GET("/api/connections/{connection_id}/probe", { params: { path: { connection_id: connectionId } } })),
    [connectionId],
  );
  const [analyzing, setAnalyzing] = useState(false);
  const [runInProgress, setRunInProgress] = useState(false);

  if (connection.error) return <ErrorNotice error={connection.error} />;
  if (!connection.data) return <Loading what="the Connection" />;
  const c = connection.data;
  const p = probe.data;

  return (
    <ConnectionContext.Provider value={{ connection: c, probe, reload: connection.reload }}>
      {p && !p.in_recovery && (
        <div className="hazard" role="note">
          <span>Primary</span> {c.name} is a writable primary. Every statement is still read-only and
          gated, but this is a production writer.
        </div>
      )}
      <header className="conn-head">
        <div>
          <p className="eyebrow">Connection</p>
          <h1>{c.name}</h1>
          <p className="muted conn-meta">
            DSN from <code>{c.dsn_env}</code>
            {!c.dsn_env_set && <span className="tag tag-danger">not set</span>}
            {p && (
              <>
                {" · "}PostgreSQL {p.server_version}
                {" · "}
                {serverRole(p)}
                {" · "}
                {hostLabel(p)}
              </>
            )}
          </p>
        </div>
        <button type="button" onClick={() => setAnalyzing(true)}>
          {runInProgress ? "Run in progress…" : "Analyze"}
        </button>
      </header>
      <nav className="tabs" aria-label="Connection">
        {TABS.map((t) => (
          <NavLink key={t.label} to={t.to} end={t.end}>
            {t.label}
          </NavLink>
        ))}
      </nav>
      {/* Keyed: a page's own state (a form, picked Runs, filters) never carries over to another Connection. */}
      <Outlet key={`page-${c.id}`} />
      <AnalyzeDialog key={`analyze-${c.id}`} open={analyzing} onClose={() => setAnalyzing(false)} onRunning={setRunInProgress} />
    </ConnectionContext.Provider>
  );
}
