// The app's one API client, the token it carries, and helpers around the generated client.
import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import {
  ApiError,
  apiClient,
  downloadReport,
  streamTurn,
  type AgentEvent,
  type ErrorBody,
  type ReportFormat,
} from "../api/client";

const TOKEN_KEY = "dbx.token";

function storedToken(): string | undefined {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? undefined;
  } catch {
    return undefined;
  }
}

let token = storedToken();
export let api = apiClient({ token });

/** Use `next` as the bearer token from now on (none when empty), and remember it in this browser. */
export function setToken(next: string): void {
  token = next || undefined;
  api = apiClient({ token });
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Private windows may refuse storage: the token then lasts until the page reloads.
  }
}

export function hasToken(): boolean {
  return token !== undefined;
}

type Result = { data?: unknown; error?: unknown; response: Response };

/** The response's data, or an ApiError with the API's error body. */
export async function unwrap<R extends Result>(request: Promise<R>): Promise<Exclude<R["data"], undefined>> {
  const { data, error, response } = await request;
  if (error !== undefined || !response.ok) {
    throw new ApiError(response.status, toErrorBody(error, response));
  }
  return data as Exclude<R["data"], undefined>;
}

function toErrorBody(error: unknown, response: Response): ErrorBody {
  if (error && typeof error === "object" && "code" in error && "message" in error) {
    return error as ErrorBody;
  }
  return { code: `http_${response.status}`, message: response.statusText || "request failed" };
}

/** A Turn's events, with this app's token. Closing the tab ends the stream, and the API then
cancels the Turn. */
export function turnEvents(threadId: string, message: string): AsyncGenerator<AgentEvent> {
  return streamTurn(threadId, message, { token });
}

export function download(runId: string, format: ReportFormat): Promise<void> {
  return downloadReport(runId, format, { token });
}

export interface Loaded<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => void;
  /** Change the data without a request, e.g. with what a mutation returned. */
  set: Dispatch<SetStateAction<T | undefined>>;
}

/** Load `fetcher` now and whenever `deps` change; `reload` loads it again. Data loaded for
other `deps` is dropped at once, so a page never shows one Connection's data under another. */
export function useLoad<T>(fetcher: () => Promise<T>, deps: unknown[]): Loaded<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);
  const [generation, setGeneration] = useState(0);
  const loadedFor = useRef<string>(undefined);

  useEffect(() => {
    let current = true;
    const key = JSON.stringify(deps);
    if (loadedFor.current !== key) {
      loadedFor.current = key;
      setData(undefined);
      setError(undefined);
    }
    setLoading(true);
    fetcher().then(
      (d) => {
        if (!current) return;
        setData(d);
        setError(undefined);
        setLoading(false);
      },
      (e: unknown) => {
        if (!current) return;
        setError(e);
        setLoading(false);
      },
    );
    return () => {
      current = false;
    };
  }, [...deps, generation]);

  const reload = useCallback(() => setGeneration((g) => g + 1), []);
  return { data, error, loading, reload, set: setData };
}
