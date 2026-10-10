# Finding lifecycle: coverage, ranking facts and where obsolete comes from

**Status:** accepted (#12); coverage amended by ADR 0009 (Findings carry their collection, and index categories are covered) and by ADR 0011 (analyzers declare the categories they cover; facts are Observations of severity `info`)

The lifecycle rules are plain Python in `core/lifecycle.py`, applied by `AnalyzerService.run` after the Run is finished. A Run's outcome for each Finding is one of:

- **Observed:** it gets an Observation. An acknowledged Finding stays acknowledged. Any other status (open, fixed, obsolete) becomes open: a fixed problem seen again is open again, and an obsolete one whose subject came back exists again. Any "fixed?" prompt is withdrawn.
- **Subject gone:** obsolete, with no prompt, whatever its status.
- **Covered but not observed:** the Finding keeps its status and `Finding.unobserved_by` records the Run. That is the "fixed?" prompt. It stays until a covering Run observes the Finding again or the engineer sets a status (`set_finding_status`, `dbx ack/fixed/reopen`). Nothing sets fixed automatically.
- **Not covered:** nothing changes, and an earlier prompt stands.

**Coverage is per analyzer and collection.** A Run covers a Finding when the analyzer that evaluates its category measured its subject: the subject is in `Run.scope[analyzer]`. Skipped collections are outside the scope, so they are never covered. Only the categories whose subject is one collection (`size`, `bloat`, `stale_stats`, all inventory) are mapped today. Index, query and hotspot categories are left out until their analyzers say what their scope covers. Until then they never ask "fixed?" and never become obsolete, which is safe because a missing prompt is better than a false one.

This departs from the proposal (§ Finding lifecycle), which says an observed Finding "keeps its status". That still holds for open and acknowledged Findings. For fixed and obsolete ones it doesn't, because keeping the status would hide a regression or a re-created table.

**Size rankings never ask "fixed?".** The inventory analyzer records `size:<table>` (no rule) only for the ten largest collections, and only in a broad Run. Dropping out of the top ten is not a fix. A targeted Run ranks nothing, so it would ask "fixed?" about every ranked table it measured. Ranking `size` Findings are facts, so they are exempt from the prompt, and Run comparison doesn't report them as appeared or disappeared (size changes cover growth). The cost is that a table which drops out of the top ten keeps an open `size:<table>` Finding, with its last-seen Run, until the table is dropped. `size` Findings with a rule (`index_heavy`, `toast_oversized`) are problems and follow the normal rules.

**"Gone" comes from the Run's catalog listing, not the probe.** This departs from the proposal's "gone from the latest probe": the probe doesn't list collections. Every inventory Run lists all of them before it narrows to a targeted scope, so even a targeted Run knows which collections exist. A Run that doesn't list collections passes `existing=None` and marks nothing obsolete.

**Inventory never skips a whole collection.** Sizes come from the catalog, which needs no table privilege and is exempt from the gate. Its only gated measurements are skipped measurements (ADR 0007). The skipped-collection path (`Run.skipped` → not covered, Run partial) is exercised by the lifecycle unit tests. The first analyzer to use it end to end is hotspot, where a refused exact count per entity is the measurement itself.

**Run comparison is over shared scope only.** `compare_runs` puts the two Runs in order (oldest first) and compares only the collections both measured, per analyzer: their size changes, and problem Findings that appeared or disappeared on them. Collections only one Run measured, including any the other skipped, are listed as not compared.

**JSON export is laid out for diffs.** Like the Markdown report, it holds what the Run saw, not Finding statuses: those change after the Run, so including them would make re-exporting an old Run differ from its first export. Status lives in `findings` and `dbx findings`. Collections are sorted by name, Findings by fingerprint, keys sorted and indented one value per line, so two exports of similar Runs line up and a diff shows only changed values. Markdown keeps rank order, because it's meant to be read.

## Considered Options

- **One `size` Finding per measured collection, so rankings follow the normal rules:** rejected. On a 500-table database that's 500 Findings whose only content is a size, and it would still ask "fixed?" when a table shrinks.
- **Compute the "fixed?" prompt from history on read instead of storing it:** rejected. Runs are few and the check is cheap either way, but a stored `unobserved_by` makes "the engineer answered" (clear on set) explicit and keeps listing a single query.
- **Leave fixed Findings fixed when their subject is dropped:** rejected. Obsolete means the subject can't exist, which is true whatever was done about it earlier. Its Observations keep the history.
