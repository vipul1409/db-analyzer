import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Empty } from "../components/bits";
import { ErrorNotice } from "../components/ErrorNotice";
import { api, unwrap } from "../lib/api";
import { useApp } from "./Shell";

export function ConnectionsPage() {
  const { connections } = useApp();
  const list = connections.data ?? [];
  return (
    <div className="page narrow">
      <h1>Connections</h1>
      <p className="lede">
        A Connection points at one PostgreSQL database through a read-only login. Its DSN stays in
        the environment <code>dbx serve</code> runs in; here you only name the variable.
      </p>
      {list.length === 0 ? (
        <Empty>No Connections yet. Add one below, or with <code>dbx connect</code>.</Empty>
      ) : (
        <ul className="card-list">
          {list.map((c) => (
            <li key={c.id}>
              <Link to={`/c/${c.id}`} className="card-link">
                <strong>{c.name}</strong>
                <span className="muted">
                  <code>{c.dsn_env}</code> {c.dsn_env_set ? "is set" : <span className="tag tag-danger">not set</span>}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
      <AddConnection onAdded={connections.reload} />
    </div>
  );
}

function AddConnection({ onAdded }: { onAdded: () => void }) {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [dsnEnv, setDsnEnv] = useState("");
  const [error, setError] = useState<unknown>();
  const [saving, setSaving] = useState(false);

  const add = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(undefined);
    try {
      const c = await unwrap(api.POST("/api/connections", { body: { name: name.trim(), dsn_env: dsnEnv.trim() } }));
      onAdded();
      navigate(`/c/${c.id}`);
    } catch (err) {
      setError(err);
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="panel stack" onSubmit={add}>
      <h2>Add a Connection</h2>
      <div className="row">
        <label className="field">
          <span>Name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="shop-replica" required />
        </label>
        <label className="field">
          <span>Environment variable holding the DSN</span>
          <input
            value={dsnEnv}
            onChange={(e) => setDsnEnv(e.target.value)}
            placeholder="SHOP_DSN"
            pattern="[A-Za-z_][A-Za-z0-9_]*"
            title="An environment variable name, such as SHOP_DSN"
            required
          />
        </label>
      </div>
      <p className="hint">Never paste the DSN itself. Adding a Connection with an existing name updates it.</p>
      <ErrorNotice error={error} />
      <div>
        <button type="submit" disabled={saving}>
          {saving ? "Adding…" : "Add Connection"}
        </button>
      </div>
    </form>
  );
}
