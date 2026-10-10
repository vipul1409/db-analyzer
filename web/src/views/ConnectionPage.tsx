import { useState, type FormEvent } from "react";
import type { Schemas } from "../api/client";
import { DsnEnvMissing, ErrorNotice } from "../components/ErrorNotice";
import { api, unwrap } from "../lib/api";
import { ApiError } from "../api/client";
import { ago, dateTime } from "../lib/format";
import { useConnection } from "./ConnectionLayout";
import { useApp } from "./Shell";

type Probe = Schemas["ProbeResult"];

// As `dbx connect` lists them.
const WANTED_EXTENSIONS = ["pg_stat_statements", "hypopg", "pgstattuple", "pg_buffercache"];

export function hostLabel(p: Probe): string {
  return p.host_type === "azure_flexible" ? "Azure Flexible Server" : "self-managed";
}

export function serverRole(p: Probe): string {
  return p.in_recovery ? "replica" : "primary";
}

function queryStoreCapture(p: Probe): string {
  return p.settings["pg_qs.query_capture_mode"] ?? "none";
}

/** A Connection's overview: what the analyzer can see, and its settings. */
export function ConnectionPage() {
  const { connection, probe } = useConnection();
  const [probing, setProbing] = useState(false);
  const [error, setError] = useState<unknown>();

  const test = async () => {
    setProbing(true);
    setError(undefined);
    try {
      probe.set(
        await unwrap(
          api.POST("/api/connections/{connection_id}/probe", {
            params: { path: { connection_id: connection.id } },
          }),
        ),
      );
    } catch (e) {
      setError(e);
    } finally {
      setProbing(false);
    }
  };

  return (
    <div className="page split">
      <section className="stack">
        {!connection.dsn_env_set && (
          <div className="notice notice-warn">
            <DsnEnvMissing env={connection.dsn_env} />
          </div>
        )}
        <div className="section-head">
          <h2>What I can see</h2>
          <button type="button" onClick={() => void test()} disabled={probing}>
            {probing ? "Testing…" : "Test connection"}
          </button>
        </div>
        {/* A missing DSN variable is already explained above. */}
        {!(error instanceof ApiError && error.body.code === "dsn_env_missing" && !connection.dsn_env_set) && (
          <ErrorNotice error={error} title="The probe failed" />
        )}
        {probe.data ? (
          <ProbeView probe={probe.data} />
        ) : (
          !probe.loading && (
            <p className="muted">
              Not probed yet. Testing connects with the Connection's limits and reads the server's
              version, extensions, privileges and statistics age. Nothing is written.
            </p>
          )
        )}
      </section>
      <SettingsForm />
    </div>
  );
}

function ProbeView({ probe: p }: { probe: Probe }) {
  const gaps = probeGaps(p);
  return (
    <div className="stack">
      <dl className="facts">
        <dt>Server</dt>
        <dd>
          PostgreSQL {p.server_version} ({hostLabel(p)}, {serverRole(p)})
        </dd>
        <dt>Extensions</dt>
        <dd className="chips">
          {WANTED_EXTENSIONS.map((e) => (
            <span key={e} className={e in p.extensions ? "chip chip-on" : "chip chip-off"}>
              {e}
              {e in p.extensions ? ` ${p.extensions[e]}` : " · not installed"}
            </span>
          ))}
        </dd>
        <dt>pg_monitor</dt>
        <dd>{p.privileges.pg_monitor ? "yes" : <span className="text-danger">no</span>}</dd>
        <dt>Tables</dt>
        <dd>
          {p.privileges.readable_tables} readable
          {p.privileges.unreadable_tables.length > 0 && (
            <span className="text-warn">, {p.privileges.unreadable_tables.length} without SELECT</span>
          )}
        </dd>
        <dt>Statistics reset</dt>
        <dd>{ago(p.stats.database_stats_reset)}</dd>
        {"pg_stat_statements" in p.extensions && (
          <>
            <dt>Statement stats reset</dt>
            <dd>{ago(p.stats.statements_stats_reset)}</dd>
          </>
        )}
        {p.host_type === "azure_flexible" && (
          <>
            <dt>Query Store</dt>
            <dd>
              capture {queryStoreCapture(p)},{" "}
              {p.privileges.azure_sys_connect ? "CONNECT on azure_sys" : "no CONNECT on azure_sys"}
            </dd>
          </>
        )}
        <dt>Never analyzed tables</dt>
        <dd>{p.stats.never_analyzed_tables || "none"}</dd>
        <dt>Probed</dt>
        <dd>{dateTime(p.taken_at)}</dd>
      </dl>
      {gaps.length > 0 && (
        <div className="panel stack-sm">
          <h3>To see more</h3>
          <ul className="gaps">
            {gaps.map((g) => (
              <li key={g.what}>
                <strong>{g.what}</strong> {g.why}
                {g.fix && <pre className="code-line">{g.fix}</pre>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

interface Gap {
  what: string;
  why: string;
  fix?: string;
}

/** What the probe says the analyzer can't do yet, and how an admin turns each one on. */
function probeGaps(p: Probe): Gap[] {
  const gaps: Gap[] = [];
  const azure = p.host_type === "azure_flexible";
  const role = "<analyzer role>";
  if (!("pg_stat_statements" in p.extensions)) {
    const queryStore = azure && p.privileges.azure_sys_connect && queryStoreCapture(p) !== "none";
    gaps.push({
      what: "No pg_stat_statements.",
      why: queryStore
        ? "Workload Runs read Azure Query Store instead; pg_stat_statements gives fuller statistics."
        : "Workload Runs can't rank slow statements. Add it to shared_preload_libraries" +
          (azure ? " (Azure portal: Server parameters; allow it in azure.extensions)" : "") +
          ", restart the server, then in this database:",
      fix: queryStore ? undefined : "CREATE EXTENSION pg_stat_statements;",
    });
  }
  if (!p.privileges.pg_monitor) {
    gaps.push({
      what: "No pg_monitor.",
      why: "Statistics views, other roles' statements and the dead-tuple scan need it.",
      fix: `GRANT pg_monitor TO ${role};`,
    });
  }
  if (!("pgstattuple" in p.extensions)) {
    gaps.push({
      what: "No pgstattuple.",
      why: "Bloat is estimated from counters instead of measured with pgstattuple_approx.",
      fix: "CREATE EXTENSION pgstattuple;",
    });
  }
  const unreadable = p.privileges.unreadable_tables;
  if (unreadable.length > 0) {
    const shown = unreadable.slice(0, 5).join(", ") + (unreadable.length > 5 ? ", …" : "");
    gaps.push({
      what: `${unreadable.length} tables without SELECT (${shown}).`,
      why: "Their sizes are still measured; exact row counts are skipped. For each schema:",
      fix: `GRANT SELECT ON ALL TABLES IN SCHEMA <schema> TO ${role};`,
    });
  }
  if (azure && !p.privileges.azure_sys_connect) {
    gaps.push({
      what: "No CONNECT on azure_sys.",
      why: "Query Store can't be read as a workload source.",
      fix: `GRANT CONNECT ON DATABASE azure_sys TO ${role};`,
    });
  }
  if (azure && queryStoreCapture(p) === "none") {
    gaps.push({
      what: "Query Store isn't capturing.",
      why: "Set the server parameter pg_qs.query_capture_mode to top or all in the Azure portal.",
    });
  }
  if (p.stats.never_analyzed_tables > 0) {
    gaps.push({
      what: `${p.stats.never_analyzed_tables} tables never analyzed.`,
      why: "Their row estimates and plans are guesses until ANALYZE runs on them.",
    });
  }
  return gaps;
}

/** Session limits, EXPLAIN gate limits and identifier aliasing: saved by name, like `dbx connect`. */
function SettingsForm() {
  const { connection, reload } = useConnection();
  const { connections } = useApp();
  const [dsnEnv, setDsnEnv] = useState(connection.dsn_env);
  // What is saved now (reloaded after each save). The API always sends both; the fallbacks only
  // satisfy the schema, where they are optional.
  const savedLimits = connection.limits ?? { statement_timeout: "30s", lock_timeout: "1s", work_mem: "32MB" };
  const savedGate = connection.gate ?? { max_total_cost: 2_000_000, max_result_rows: 10_000, max_scan_rows: 50_000_000 };
  const [limits, setLimits] = useState<Schemas["SessionLimits"]>(savedLimits);
  const [gate, setGate] = useState<Schemas["GateLimits"]>(savedGate);
  const [alias, setAlias] = useState(connection.alias_identifiers);
  const [error, setError] = useState<unknown>();
  const [saved, setSaved] = useState(false);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setError(undefined);
    setSaved(false);
    try {
      // Only what changed: a setting left out keeps its current value.
      const changed = (a: object, b: object) => JSON.stringify(a) !== JSON.stringify(b);
      await unwrap(
        api.POST("/api/connections", {
          body: {
            name: connection.name,
            dsn_env: dsnEnv.trim(),
            ...(changed(limits, savedLimits) && { limits }),
            ...(changed(gate, savedGate) && { gate }),
            ...(alias !== connection.alias_identifiers && { alias_identifiers: alias }),
          },
        }),
      );
      setSaved(true);
      reload();
      connections.reload();
    } catch (err) {
      setError(err);
    }
  };

  const gateField = (key: keyof Schemas["GateLimits"], label: string) => (
    <label className="field">
      <span>{label}</span>
      <input
        type="number"
        min={1}
        step="any"
        value={gate[key]}
        onChange={(e) => setGate({ ...gate, [key]: Number(e.target.value) })}
        required
      />
    </label>
  );
  const limitField = (key: keyof Schemas["SessionLimits"], label: string, example: string) => (
    <label className="field">
      <span>{label}</span>
      <input value={limits[key]} onChange={(e) => setLimits({ ...limits, [key]: e.target.value })} placeholder={example} required />
    </label>
  );

  return (
    <form className="panel stack settings" onSubmit={save} onChange={() => setSaved(false)}>
      <h2>Settings</h2>
      <label className="field">
        <span>DSN environment variable</span>
        <input value={dsnEnv} onChange={(e) => setDsnEnv(e.target.value)} pattern="[A-Za-z_][A-Za-z0-9_]*" required />
      </label>
      <fieldset>
        <legend>Session limits</legend>
        <p className="hint">Set on every session the analyzer opens, as PostgreSQL values.</p>
        {limitField("statement_timeout", "Statement timeout", "30s")}
        {limitField("lock_timeout", "Lock timeout", "1s")}
        {limitField("work_mem", "work_mem", "32MB")}
      </fieldset>
      <fieldset>
        <legend>EXPLAIN gate</legend>
        <p className="hint">A statement whose plan exceeds any limit is refused before it runs.</p>
        {gateField("max_total_cost", "Max plan cost")}
        {gateField("max_result_rows", "Max result rows")}
        {gateField("max_scan_rows", "Max scanned rows")}
      </fieldset>
      <label className="check">
        <input type="checkbox" checked={alias} onChange={(e) => setAlias(e.target.checked)} />
        <span>
          Alias identifiers
          <span className="hint block">The model sees aliases instead of schema, table and column names. Answers show the real names.</span>
        </span>
      </label>
      <ErrorNotice error={error} />
      <div className="row-center">
        <button type="submit">Save settings</button>
        {saved && <span className="text-ok">Saved</span>}
      </div>
    </form>
  );
}
