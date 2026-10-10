import { useState } from "react";
import { Loading } from "../components/bits";
import { ErrorNotice } from "../components/ErrorNotice";
import { api, unwrap, useLoad } from "../lib/api";
import { count, usd } from "../lib/format";

function thisMonth(): string {
  return new Date().toISOString().slice(0, 7);
}

/** Every Thread's tokens and estimated cost in one calendar month (UTC). The budget cap is paused. */
export function UsagePage() {
  const [month, setMonth] = useState(thisMonth);
  const usage = useLoad(() => unwrap(api.GET("/api/usage", { params: { query: { month } } })), [month]);
  const u = usage.data;

  return (
    <div className="page narrow stack">
      <div className="section-head">
        <h1>Usage</h1>
        <label className="field field-inline">
          <span>Month</span>
          <input type="month" value={month} max={thisMonth()} onChange={(e) => e.target.value && setMonth(e.target.value)} />
        </label>
      </div>
      <ErrorNotice error={usage.error} />
      {!u ? (
        !usage.error && <Loading what="usage" />
      ) : (
        <>
          <div className="readouts">
            <div className="readout">
              <span className="readout-value">{usd(u.cost_usd)}</span>
              <span className="readout-label">estimated cost</span>
            </div>
            <div className="readout">
              <span className="readout-value">{count(u.input_tokens)}</span>
              <span className="readout-label">input tokens ({count(u.cached_tokens)} cached)</span>
            </div>
            <div className="readout">
              <span className="readout-value">{count(u.output_tokens)}</span>
              <span className="readout-label">output tokens</span>
            </div>
          </div>
          {u.cost_usd === null && (
            <p className="muted">The cost is unknown because a model's price isn't known; it is never counted as zero.</p>
          )}
          <p className="muted">
            Across every Thread on every Connection. Each Thread's own total is in its chat header. There's
            no monthly budget cap in this version.
          </p>
        </>
      )}
    </div>
  );
}
