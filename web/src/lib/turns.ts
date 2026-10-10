// Turns since the page loaded, per Thread, outside any component: a Turn keeps streaming while
// the engineer looks at another page, and stops only when cancelled or when the tab closes.
import { useEffect, useSyncExternalStore } from "react";
import { ApiError, type AgentEvent, type Schemas } from "../api/client";
import { api, turnEvents, unwrap } from "./api";

type EventOf<T extends AgentEvent["type"]> = Extract<AgentEvent, { type: T }>;

/** One line of a Turn's progress trace. `depth` is 1 inside a subagent. */
export type TraceItem =
  | { kind: "tool"; callId: string; name: string; args: Record<string, unknown>; depth: number; finished?: { ok: boolean; summary: string } }
  | { kind: "subagent"; callId: string; name: string; depth: number; finished?: { ok: boolean } }
  | { kind: "sql"; event: EventOf<"sql_executed">; depth: number }
  | { kind: "rejected"; event: EventOf<"sql_rejected">; depth: number }
  | { kind: "run"; runId: string; status: string; depth: number }
  | { kind: "limit"; event: EventOf<"limit_reached"> };

export interface Turn {
  message: string;
  answer: string;
  trace: TraceItem[];
  state: "running" | "answered" | "cancelled" | "failed";
  error?: string;
  usage?: Schemas["Usage"];
  /** Tool-call ids of the subagents running now, innermost last: what a new trace item nests under. */
  subagentCalls: string[];
}

export function startedTurn(message: string): Turn {
  return { message, answer: "", trace: [], state: "running", subagentCalls: [] };
}

/** The Turn after one more event. Pure: the store and nothing else applies it. */
export function applyEvent(turn: Turn, event: AgentEvent): Turn {
  const depth = turn.subagentCalls.length;
  const add = (item: TraceItem): Turn => ({ ...turn, trace: [...turn.trace, item] });
  const finishTool = (callId: string, finished: { ok: boolean; summary: string }): Turn => ({
    ...turn,
    trace: turn.trace.map((t) => (t.kind === "tool" && t.callId === callId ? { ...t, finished } : t)),
  });
  const finishSubagent = (callId: string, ok: boolean): Turn => ({
    ...turn,
    trace: turn.trace.map((t) => (t.kind === "subagent" && t.callId === callId ? { ...t, finished: { ok } } : t)),
    subagentCalls: turn.subagentCalls.filter((id) => id !== callId),
  });
  switch (event.type) {
    case "token":
      return { ...turn, answer: turn.answer + event.text };
    case "tool_started":
      return add({ kind: "tool", callId: event.call_id, name: event.name, args: event.args, depth });
    case "tool_finished":
      return finishTool(event.call_id, { ok: event.ok, summary: event.summary });
    case "subagent_started":
      return {
        ...add({ kind: "subagent", callId: event.call_id, name: event.name, depth }),
        subagentCalls: [...turn.subagentCalls, event.call_id],
      };
    case "subagent_finished":
      return finishSubagent(event.call_id, event.ok);
    case "sql_executed":
      return add({ kind: "sql", event, depth });
    case "sql_rejected":
      return add({ kind: "rejected", event, depth });
    case "run_finished":
      return add({ kind: "run", runId: event.run_id, status: event.status, depth });
    case "limit_reached":
      return add({ kind: "limit", event });
    case "usage":
      return { ...turn, usage: event };
    case "error":
      return { ...turn, error: event.message };
    case "done":
      return {
        ...turn,
        answer: event.answer || turn.answer,
        state: event.cancelled ? "cancelled" : event.ok ? "answered" : "failed",
        subagentCalls: [],
      };
  }
}

export interface ThreadState {
  /** The Thread's history as of the first visit since the page loaded; Turns since are in `turns`. */
  history?: Schemas["ThreadMessage"][];
  historyError?: unknown;
  turns: Turn[];
}

const threads = new Map<string, ThreadState>();
const listeners = new Set<() => void>();
const EMPTY: ThreadState = { turns: [] };

function update(threadId: string, change: (s: ThreadState) => ThreadState): void {
  threads.set(threadId, change(threads.get(threadId) ?? EMPTY));
  listeners.forEach((l) => l());
}

function updateLastTurn(threadId: string, change: (t: Turn) => Turn): void {
  update(threadId, (s) => ({ ...s, turns: s.turns.map((t, i) => (i === s.turns.length - 1 ? change(t) : t)) }));
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** A Thread's history and its Turns since the page loaded; loads the history on first use. */
export function useThreadState(threadId: string): ThreadState {
  useEffect(() => {
    if (threads.has(threadId)) return;
    threads.set(threadId, EMPTY);
    void loadHistory(threadId);
  }, [threadId]);
  return useSyncExternalStore(subscribe, () => threads.get(threadId) ?? EMPTY);
}

/** Load the Thread's history, again after a failure. */
export async function loadHistory(threadId: string): Promise<void> {
  update(threadId, (s) => ({ ...s, historyError: undefined }));
  try {
    const history = await unwrap(api.GET("/api/threads/{thread_id}/history", { params: { path: { thread_id: threadId } } }));
    update(threadId, (s) => ({ ...s, history }));
  } catch (e) {
    update(threadId, (s) => ({ ...s, historyError: e }));
  }
}

export function isRunning(thread: ThreadState): boolean {
  return thread.turns.at(-1)?.state === "running";
}

/** Send `message` as a new Turn and stream it into the store. */
export async function send(threadId: string, message: string): Promise<void> {
  update(threadId, (s) => ({ ...s, turns: [...s.turns, startedTurn(message)] }));
  try {
    for await (const event of turnEvents(threadId, message)) {
      updateLastTurn(threadId, (t) => applyEvent(t, event));
    }
  } catch (e) {
    const error = e instanceof ApiError ? refusal(e) : `The stream broke off: ${String(e)}`;
    updateLastTurn(threadId, (t) => ({ ...t, state: "failed", error }));
    return;
  }
  // A stream that ends without `done` was cut off by the server.
  updateLastTurn(threadId, (t) => (t.state === "running" ? { ...t, state: "failed", error: "The stream ended before the Turn did." } : t));
}

function refusal(e: ApiError): string {
  if (e.body.code === "turn_active") return "This Thread already has a Turn running (perhaps in another tab or the CLI).";
  return e.body.message;
}

/** Ask the API to stop the Thread's Turn; its stream then ends with `done`, cancelled. */
export async function cancel(threadId: string): Promise<void> {
  await unwrap(api.POST("/api/threads/{thread_id}/cancel", { params: { path: { thread_id: threadId } } }));
}
