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
  if (!response.ok || !response.body) {
    throw new ApiError(response.status, (await response.json()) as ErrorBody);
  }
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
