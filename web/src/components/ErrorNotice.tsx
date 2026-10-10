import { ApiError } from "../api/client";

/** An API error as the engineer should read it: what happened, and how to fix it when we know. */
export function ErrorNotice({ error, title }: { error: unknown; title?: string }) {
  if (!error) return null;
  return (
    <div className="notice notice-danger" role="alert">
      {title && <strong className="notice-title">{title}</strong>}
      <ErrorText error={error} />
    </div>
  );
}

function ErrorText({ error }: { error: unknown }) {
  if (!(error instanceof ApiError)) {
    const message = error instanceof Error ? error.message : String(error);
    return <p>Can't reach the API ({message}). Is <code>dbx serve</code> running?</p>;
  }
  const { code, message, details } = error.body;
  switch (code) {
    case "dsn_env_missing":
      return <DsnEnvMissing env={String(details?.dsn_env ?? "")} />;
    case "connection_refused":
      return <p><strong>Refused to connect.</strong> {message}</p>;
    case "database_unreachable":
      return <p><strong>Can't reach the database.</strong> {message}</p>;
    case "unauthorized":
      return <p>The API wants a token. Set the one <code>dbx serve</code> was started with (DBX_API_TOKEN).</p>;
    case "chat_unavailable":
      return <p>Chat needs <code>OPENAI_API_KEY</code> where <code>dbx serve</code> runs. Runs work without it.</p>;
    case "invalid_request":
      return <p>{invalidRequest(details)}</p>;
    default:
      return <p>{message}</p>;
  }
}

function invalidRequest(details: Record<string, unknown> | null | undefined): string {
  const errors = details?.errors;
  if (!Array.isArray(errors) || errors.length === 0) return "The request was invalid.";
  return errors
    .map((e) => (e && typeof e === "object" && "msg" in e ? String(e.msg) : String(e)))
    .join("; ");
}

/** The DSN never goes through the browser: say exactly which line the API's environment lacks. */
export function DsnEnvMissing({ env }: { env: string }) {
  return (
    <div className="stack-sm">
      <p>
        <strong>{env}</strong> isn't set where <code>dbx serve</code> runs, so the API has no DSN for
        this Connection. Add it to that environment (or to <code>.env</code>), then restart the server:
      </p>
      <pre className="code-line">{`${env}='postgresql://analyzer@host:5432/dbname'`}</pre>
    </div>
  );
}
