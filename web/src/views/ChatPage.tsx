import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, NavLink, useNavigate, useParams } from "react-router-dom";
import { Empty, Markdown } from "../components/bits";
import { ErrorNotice } from "../components/ErrorNotice";
import { ProgressPanel } from "../components/ProgressPanel";
import { api, unwrap, useLoad } from "../lib/api";
import { ago, count, shortId, usd } from "../lib/format";
import { cancel, isRunning, loadHistory, send, useThreadState, type Turn } from "../lib/turns";
import { useConnection } from "./ConnectionLayout";
import { useApp } from "./Shell";

export function ChatPage() {
  const { connection } = useConnection();
  const { health } = useApp();
  const { threadId } = useParams();
  const navigate = useNavigate();
  const threads = useLoad(
    () => unwrap(api.GET("/api/connections/{connection_id}/threads", { params: { path: { connection_id: connection.id } } })),
    [connection.id],
  );
  const [error, setError] = useState<unknown>();

  const startThread = async () => {
    setError(undefined);
    try {
      const thread = await unwrap(
        api.POST("/api/connections/{connection_id}/threads", { params: { path: { connection_id: connection.id } } }),
      );
      threads.reload();
      navigate(`/c/${connection.id}/chat/${thread.id}`);
    } catch (e) {
      setError(e);
    }
  };

  const chatOff = health !== undefined && !health.chat_available;

  return (
    <div className="chat">
      <aside className="threads" aria-label="Threads">
        <button type="button" className="button-wide" onClick={() => void startThread()}>
          New Thread
        </button>
        <ErrorNotice error={error ?? threads.error} />
        <ul className="thread-list">
          {threads.data?.map((t) => (
            <li key={t.id}>
              <NavLink to={`/c/${connection.id}/chat/${t.id}`}>
                <span className="thread-preview">{t.preview ?? <em className="muted">No messages yet</em>}</span>
                <span className="thread-time">{t.last_active_at ? ago(t.last_active_at) : `created ${ago(t.created_at)}`}</span>
              </NavLink>
            </li>
          ))}
        </ul>
      </aside>
      <div className="chat-body">
        {chatOff && (
          <div className="notice notice-warn">
            Chat is off: <code>dbx serve</code> has no <code>OPENAI_API_KEY</code>. Earlier Threads still
            open. Runs, Findings and reports don't need a model; start one with <strong>Analyze</strong>.
          </div>
        )}
        {threadId ? (
          <Conversation key={threadId} threadId={threadId} chatOff={chatOff} onTurnEnded={threads.reload} />
        ) : (
          <Empty>
            {threads.data?.length ? "Pick a Thread, or start a new one." : "No Threads yet. Start one to ask about this database."}
          </Empty>
        )}
      </div>
    </div>
  );
}

function Conversation({ threadId, chatOff, onTurnEnded }: { threadId: string; chatOff: boolean; onTurnEnded: () => void }) {
  const { connection } = useConnection();
  const thread = useThreadState(threadId);
  const running = isRunning(thread);
  const [selected, setSelected] = useState<number>();
  const usage = useLoad(
    () => unwrap(api.GET("/api/threads/{thread_id}/usage", { params: { path: { thread_id: threadId } } })),
    [threadId],
  );

  // A Turn that just ended changes the Thread list (preview, activity) and the Thread's usage.
  const wasRunning = useRef(running);
  useEffect(() => {
    if (wasRunning.current && !running) {
      onTurnEnded();
      usage.reload();
    }
    wasRunning.current = running;
  }, [running, onTurnEnded, usage.reload]);

  const end = useRef<HTMLDivElement>(null);
  const last = thread.turns.at(-1);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [thread.turns.length, last?.answer.length, thread.history]);

  const shown = selected ?? thread.turns.length - 1;
  const traced = thread.turns[shown];

  return (
    <div className="chat-pane">
      <section className="conversation" aria-label="Conversation">
        <header className="conversation-head">
          <span className="muted">Thread {shortId(threadId)}</span>
          {usage.data && (
            <span className="muted" title="Tokens and estimated cost of this Thread so far">
              {count(usage.data.input_tokens + usage.data.output_tokens)} tokens · {usd(usage.data.cost_usd)}
            </span>
          )}
          <Link to={`/c/${connection.id}/audit?thread=${threadId}`}>Audit for this Thread</Link>
        </header>
        <div className="messages">
          {thread.historyError !== undefined && (
            <div className="stack-sm">
              <ErrorNotice error={thread.historyError} title="The history didn't load" />
              <div>
                <button type="button" className="button-small button-secondary" onClick={() => void loadHistory(threadId)}>
                  Load it again
                </button>
              </div>
            </div>
          )}
          {thread.history?.map((m, i) =>
            m.role === "user" ? (
              <UserMessage key={`h${i}`} text={m.text} />
            ) : (
              <div key={`h${i}`} className="message message-agent">
                <Markdown text={m.text} />
              </div>
            ),
          )}
          {thread.turns.map((t, i) => (
            <TurnMessages key={i} turn={t} selected={i === shown} onSelect={() => setSelected(i)} />
          ))}
          {thread.history?.length === 0 && thread.turns.length === 0 && (
            <Empty>Ask about this database: its largest tables, its slowest statements, or why one is slow.</Empty>
          )}
          <div ref={end} />
        </div>
        <Composer threadId={threadId} disabled={chatOff || (thread.history === undefined && thread.historyError === undefined)} running={running} />
      </section>
      <ProgressPanel turn={traced} connectionId={connection.id} gateLimit={connection.gate?.max_total_cost} threadId={threadId} />
    </div>
  );
}

function UserMessage({ text }: { text: string }) {
  return (
    <div className="message message-user">
      <p>{text}</p>
    </div>
  );
}

function TurnMessages({ turn, selected, onSelect }: { turn: Turn; selected: boolean; onSelect: () => void }) {
  return (
    <>
      <UserMessage text={turn.message} />
      <div
        className={`message message-agent${selected ? " message-selected" : ""}`}
        aria-busy={turn.state === "running"}
      >
        {turn.answer ? <Markdown text={turn.answer} /> : turn.state === "running" && <p className="muted thinking">Working…</p>}
        {turn.state === "cancelled" && <p className="turn-end turn-end-warn">Stopped. The Thread carries on from here.</p>}
        {turn.state === "failed" && (
          <p className="turn-end turn-end-danger">
            {turn.error ?? "The Turn failed."} The Thread is still usable: rephrase and send again.
          </p>
        )}
        {turn.trace.length > 0 && (
          <button type="button" className="button-link trace-toggle" onClick={onSelect} aria-pressed={selected}>
            {turn.trace.length} steps{selected ? " shown" : ""}
          </button>
        )}
      </div>
    </>
  );
}

function Composer({ threadId, disabled, running }: { threadId: string; disabled: boolean; running: boolean }) {
  const [text, setText] = useState("");
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<unknown>();

  useEffect(() => {
    if (!running) setStopping(false);
  }, [running]);

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const message = text.trim();
    if (!message || running || disabled) return;
    setText("");
    void send(threadId, message);
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Not while an input method is composing: Enter then confirms the composition.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) submit(e);
  };
  const stop = async () => {
    setStopping(true);
    setError(undefined);
    try {
      await cancel(threadId);
    } catch (err) {
      setError(err);
      setStopping(false);
    }
  };

  return (
    <form className="composer" onSubmit={submit}>
      <ErrorNotice error={error} />
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKey}
        placeholder={running ? "Wait for this Turn to finish, or stop it." : "Ask about this database"}
        disabled={disabled || running}
        rows={2}
        aria-label="Message"
      />
      {running ? (
        <button type="button" className="button-danger" onClick={() => void stop()} disabled={stopping}>
          {stopping ? "Stopping…" : "Stop"}
        </button>
      ) : (
        <button type="submit" disabled={disabled || !text.trim()}>
          Send
        </button>
      )}
    </form>
  );
}
