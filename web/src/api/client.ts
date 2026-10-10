// The API client. Every type comes from schema.d.ts, generated from the OpenAPI spec
// (`make api-client`): never write API types by hand.
import createClient from "openapi-fetch";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type AgentEvent = Schemas["AgentEvent"];
export type ErrorBody = Schemas["ErrorBody"];

export interface ClientOptions {
  /** The token `dbx serve` was given (DBX_API_TOKEN), if any. */
  token?: string;
  /** Where the API is; by default the page's origin (the Vite dev server proxies /api). */
  baseUrl?: string;
}

export function apiClient({ token, baseUrl = "" }: ClientOptions = {}) {
  const client = createClient<paths>({ baseUrl });
  if (token) {
    client.use({
      onRequest({ request }) {
        request.headers.set("Authorization", `Bearer ${token}`);
        return request;
      },
    });
  }
  return client;
}

/** A refused request, with the API's error body. */
export class ApiError extends Error {
  readonly status: number;
  readonly body: ErrorBody;

  constructor(status: number, body: ErrorBody) {
    super(body.message);
    this.status = status;
    this.body = body;
  }
}

/** The ApiError for a refused response; a body that isn't the API's (a proxy's 502) keeps the status. */
async function refused(response: Response): Promise<ApiError> {
  try {
    return new ApiError(response.status, (await response.json()) as ErrorBody);
  } catch {
    return new ApiError(response.status, {
      code: `http_${response.status}`,
      message: response.statusText || "request failed",
    });
  }
}

/**
 * Send a message to a Thread and yield the Turn's events as they arrive; the last is `done`.
 * Read with fetch, not EventSource, so the token travels in a header. Aborting `signal` closes
 * the stream, which cancels the Turn. Throws ApiError when the Turn is refused, e.g. 409
 * `turn_active` or 503 `chat_unavailable`.
 */
export async function* streamTurn(
  threadId: string,
  message: string,
  { token, baseUrl = "", signal }: ClientOptions & { signal?: AbortSignal } = {},
): AsyncGenerator<AgentEvent> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(
    `${baseUrl}/api/threads/${encodeURIComponent(threadId)}/messages`,
    { method: "POST", headers, body: JSON.stringify({ message }), signal },
  );
  if (!response.ok || !response.body) throw await refused(response);
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffered = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffered += value;
    let end: number;
    while ((end = buffered.indexOf("\n\n")) !== -1) {
      const block = buffered.slice(0, end);
      buffered = buffered.slice(end + 2);
      const data = block.split("\n").find((line) => line.startsWith("data: "));
      if (data) yield JSON.parse(data.slice("data: ".length)) as AgentEvent;
    }
  }
}

export type ReportFormat = NonNullable<
  NonNullable<paths["/api/runs/{run_id}/export"]["get"]["parameters"]["query"]>["format"]
>;

/** Save a Run's report as a file. Fetched, not linked, so the token travels in a header. */
export async function downloadReport(
  runId: string,
  format: ReportFormat,
  { token, baseUrl = "" }: ClientOptions = {},
): Promise<void> {
  const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  const response = await fetch(
    `${baseUrl}/api/runs/${encodeURIComponent(runId)}/export?format=${format}`,
    { headers },
  );
  if (!response.ok) throw await refused(response);
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? `run-${runId}.${format}`;
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}
