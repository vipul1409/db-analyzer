import { createContext, useContext, useState, type FormEvent } from "react";
import { NavLink, Outlet } from "react-router-dom";
import type { Schemas } from "../api/client";
import { ErrorNotice } from "../components/ErrorNotice";
import { api, hasToken, setToken, unwrap, useLoad, type Loaded } from "../lib/api";
import { ApiError } from "../api/client";

interface App {
  health: Schemas["Health"] | undefined;
  connections: Loaded<Schemas["ConnectionInfo"][]>;
}

const AppContext = createContext<App | null>(null);

export function useApp(): App {
  const app = useContext(AppContext);
  if (!app) throw new Error("useApp outside the Shell");
  return app;
}

/** The rail (Connections, usage, server status) around every page. */
export function Shell() {
  const health = useLoad(() => unwrap(api.GET("/api/health")), []);
  const connections = useLoad(() => unwrap(api.GET("/api/connections")), []);
  const needsToken = connections.error instanceof ApiError && connections.error.status === 401;

  return (
    <AppContext.Provider value={{ health: health.data, connections }}>
      <div className="app">
        <nav className="rail" aria-label="Connections">
          <NavLink to="/" end className="brand">
            DB&nbsp;Analyzer
          </NavLink>
          <p className="rail-label">Connections</p>
          <ul className="rail-list">
            {connections.data?.map((c) => (
              <li key={c.id}>
                <NavLink to={`/c/${c.id}`}>
                  <span className={c.dsn_env_set ? "dot dot-ok" : "dot dot-missing"} aria-hidden />
                  {c.name}
                </NavLink>
              </li>
            ))}
            <li>
              <NavLink to="/" end className="rail-add">
                + Add a Connection
              </NavLink>
            </li>
          </ul>
          <div className="rail-foot">
            <NavLink to="/usage">Usage this month</NavLink>
            <ServerStatus health={health} />
            <TokenSetting onChange={() => {
              health.reload();
              connections.reload();
            }} />
          </div>
        </nav>
        <main className="main">
          {needsToken ? (
            <TokenForm onSaved={connections.reload} />
          ) : connections.error ? (
            <ErrorNotice error={connections.error} />
          ) : (
            <Outlet />
          )}
        </main>
      </div>
    </AppContext.Provider>
  );
}

function ServerStatus({ health }: { health: Loaded<Schemas["Health"]> }) {
  if (health.error) return <p className="rail-status rail-status-down">API unreachable</p>;
  if (!health.data) return null;
  return (
    <p className="rail-status">
      API {health.data.version}
      <br />
      {health.data.chat_available ? "Chat ready" : "Chat off: no OpenAI key"}
    </p>
  );
}

function TokenSetting({ onChange }: { onChange: () => void }) {
  const [editing, setEditing] = useState(false);
  if (!editing) {
    return (
      <button type="button" className="button-link rail-token" onClick={() => setEditing(true)}>
        {hasToken() ? "Change API token" : "Set API token"}
      </button>
    );
  }
  return (
    <TokenForm
      compact
      onSaved={() => {
        setEditing(false);
        onChange();
      }}
    />
  );
}

function TokenForm({ onSaved, compact = false }: { onSaved: () => void; compact?: boolean }) {
  const [value, setValue] = useState("");
  const save = (e: FormEvent) => {
    e.preventDefault();
    setToken(value.trim());
    onSaved();
  };
  return (
    <form className={compact ? "token-form compact" : "token-form panel"} onSubmit={save}>
      {!compact && (
        <>
          <h1>This API needs a token</h1>
          <p className="muted">
            Enter the token <code>dbx serve</code> was started with (<code>DBX_API_TOKEN</code>). It
            is kept in this browser only.
          </p>
        </>
      )}
      <label className="field">
        <span>API token</span>
        <input type="password" value={value} onChange={(e) => setValue(e.target.value)} autoComplete="off" />
      </label>
      <button type="submit">Use token</button>
    </form>
  );
}
