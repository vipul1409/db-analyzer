import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import type { Schemas } from "../api/client";
import { Dialog } from "../components/bits";
import { ErrorNotice } from "../components/ErrorNotice";
import { api, unwrap, useLoad } from "../lib/api";
import { qualified } from "../lib/format";
import { useConnection } from "./ConnectionLayout";

/** Start a deterministic Run: no model, no chat. Opens the finished Run. */
export function AnalyzeDialog({
  open,
  onClose,
  onRunning,
}: {
  open: boolean;
  onClose: () => void;
  onRunning: (running: boolean) => void;
}) {
  // The form stays mounted while closed: a Run started from it carries on, and opens when it finishes.
  return (
    <Dialog title="Analyze" open={open} onClose={onClose} keepMounted>
      <AnalyzeForm open={open} onDone={onClose} onRunning={onRunning} />
    </Dialog>
  );
}

function AnalyzeForm({
  open,
  onDone,
  onRunning,
}: {
  open: boolean;
  onDone: () => void;
  onRunning: (running: boolean) => void;
}) {
  const { connection, probe } = useConnection();
  const navigate = useNavigate();
  // Loaded again on each opening: the tables to pick from come from the latest Run.
  const runs = useLoad(() => listRuns(connection.id), [connection.id, open]);
  // Leaving the Connection drops the form: its Run then finishes unseen, and appears under Runs.
  useEffect(() => () => onRunning(false), [onRunning]);
  const known = useMemo(
    () => knownTables(runs.data ?? [], probe.data?.privileges.unreadable_tables ?? []),
    [runs.data, probe.data],
  );

  const [inventory, setInventory] = useState(true);
  const [workload, setWorkload] = useState(false);
  const [targeted, setTargeted] = useState(false);
  const [tables, setTables] = useState<string[]>([]);
  const [typed, setTyped] = useState("");
  const [exactCounts, setExactCounts] = useState(false);
  const [windowHours, setWindowHours] = useState("1");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<unknown>();

  const chosen = [...(inventory ? ["inventory"] : []), ...(workload ? ["workload"] : [])];
  const typedTables = typed.split(/[\s,]+/).filter(Boolean);
  const collections = [...new Set([...tables, ...typedTables])];

  const start = async (e: FormEvent) => {
    e.preventDefault();
    setRunning(true);
    onRunning(true);
    setError(undefined);
    // Only options the chosen analyzers take: the API refuses the others.
    const body: Schemas["RunIn"] = { analyzers: chosen, exact_counts: inventory && exactCounts };
    if (inventory && targeted) body.collections = collections;
    if (workload) body.min_stats_window_hours = Number(windowHours);
    try {
      const run = await unwrap(
        api.POST("/api/connections/{connection_id}/runs", {
          params: { path: { connection_id: connection.id } },
          body,
        }),
      );
      onDone();
      navigate(`/c/${connection.id}/runs/${run.id}`);
    } catch (err) {
      setError(err);
    } finally {
      setRunning(false);
      onRunning(false);
    }
  };

  const toggle = (table: string) =>
    setTables((t) => (t.includes(table) ? t.filter((x) => x !== table) : [...t, table]));

  return (
    <form className="stack analyze" onSubmit={start}>
      <p className="muted">Runs the analyzers now, without the model. Every statement is still gated and audited.</p>
      <fieldset disabled={running}>
        <legend>Analyzers</legend>
        <label className="check">
          <input type="checkbox" checked={inventory} onChange={(e) => setInventory(e.target.checked)} />
          <span>
            Inventory <span className="hint block">Table sizes, row counts, maintenance and index health.</span>
          </span>
        </label>
        <label className="check">
          <input type="checkbox" checked={workload} onChange={(e) => setWorkload(e.target.checked)} />
          <span>
            Workload <span className="hint block">Slow statements ranked, with plan rules.</span>
          </span>
        </label>
      </fieldset>

      {inventory && (
        <fieldset disabled={running}>
          <legend>Inventory</legend>
          <label className="check">
            <input type="checkbox" checked={targeted} onChange={(e) => setTargeted(e.target.checked)} />
            <span>Only some tables</span>
          </label>
          {targeted && (
            <div className="stack-sm">
              {known.length > 0 && (
                <div className="table-picker" role="group" aria-label="Tables from the latest inventory Run">
                  {known.map((t) => (
                    <label key={t} className="check check-compact">
                      <input type="checkbox" checked={tables.includes(t)} onChange={() => toggle(t)} />
                      <code>{t}</code>
                    </label>
                  ))}
                </div>
              )}
              <label className="field">
                <span>{known.length > 0 ? "Other tables" : "Tables"}</span>
                <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="public.orders, reference.links" />
              </label>
            </div>
          )}
          <label className="check">
            <input type="checkbox" checked={exactCounts} onChange={(e) => setExactCounts(e.target.checked)} />
            <span>
              Exact row counts
              <span className="hint block">Each count runs only where the EXPLAIN gate allows it; the others are skipped.</span>
            </span>
          </label>
        </fieldset>
      )}

      {workload && (
        <fieldset disabled={running}>
          <legend>Workload</legend>
          <label className="field field-inline">
            <span>Minimum stats window (hours)</span>
            <input type="number" min={0} step="any" value={windowHours} onChange={(e) => setWindowHours(e.target.value)} required />
          </label>
          <p className="hint">Statistics younger than this aren't ranked.</p>
        </fieldset>
      )}

      <ErrorNotice error={error} title="The Run didn't start" />
      <div className="row-center">
        <button type="submit" disabled={running || chosen.length === 0 || (inventory && targeted && collections.length === 0)}>
          {running ? "Running…" : "Start Run"}
        </button>
        {running && <span className="muted">The Run is in progress. It opens when it finishes.</span>}
      </div>
    </form>
  );
}

const listRuns = (connectionId: string) =>
  unwrap(api.GET("/api/connections/{connection_id}/runs", { params: { path: { connection_id: connectionId } } }));
type ListedRun = Awaited<ReturnType<typeof listRuns>>[number];

/** Tables to pick targets from: those the latest inventory Run measured or skipped, and those
the probe found without SELECT. */
function knownTables(runs: ListedRun[], unreadable: string[]): string[] {
  const latest = [...runs].reverse().find((r) => r.scope.inventory || r.skipped.inventory);
  const skipped = (latest?.skipped.inventory ?? []).map(([ref]) => ref).filter((ref) => typeof ref === "object");
  const names = [...(latest?.scope.inventory ?? []), ...skipped].map(qualified);
  return [...new Set([...names, ...unreadable])].sort();
}
