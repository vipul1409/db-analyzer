# Privacy filter: a parse-time output check, a last-line gateway check, and word-level aliasing

**Status:** accepted (#10)

Proposal §3.5 says the LLM sees metadata, aggregates and entity keys only. #10 builds that for SQL the agent writes (`run_readonly_sql`), adds the LLMGateway re-check, and adds optional identifier aliasing.

**Output check by name, without the catalog.** `safety/privacy.py` parses every SELECT the agent writes and resolves each output column through FROM items, joins, subqueries and CTEs by name, like the guard (ADR 0004). Each output must be one of:
- a count, over anything;
- another aggregate (`sum`, `avg`, `min`, `max`, `stddev`, percentiles, ...) of lengths or sizes, or of something itself allowed. Over a single row or a group of one, these return the row's own value: `avg(amount) … GROUP BY id` lists every amount, and `sum(ascii(substr(email, 1, 1)))` spells out an email one character at a time;
- a catalog column;
- a confirmed entity key;
- or an expression built only from these.

Where a name might come from a user table that isn't an entity key, it counts as row data. Some legitimate queries are refused as a result. `SELECT status, count(*) … GROUP BY status` is refused because `status` is a row value, and `SELECT sum(amount)` is refused because a total of amounts is still amounts. WHERE, GROUP BY and ORDER BY aren't checked, and neither are CASE conditions inside an aggregate. Anything a filter reveals reaches the output only as a count or a size.

**Sampled values per row.** Most catalog columns are metadata, but a few hold row values:
- `pg_stats` most-common values and histogram bounds;
- extended statistics and `pg_statistic` values;
- `pg_stat_activity.query`, which has literals;
- `pg_stat_statements.query` for agent SQL, because utility statements keep their literals there;
- password and option columns.

The `pg_stats` value columns pass only as bare columns of a lone `pg_stats`, selected next to `schemaname`, `tablename` and `attname`. After execution, SafeExecutor replaces them with `[withheld: not an entity key]` on every row whose column isn't an entity key. The other value-bearing columns never pass. Entity maps don't exist yet (#19), so no column is an entity key and every value is withheld.

**What the gateway re-checks.** The gateway can't tell row data from metadata in free text. The output check is the structural guarantee. The gateway is a last line against bugs in it or in a tool, and it looks for personal data it can recognise: email addresses and phone numbers.
- A tool result containing either is replaced with an error before it enters the conversation, so the Thread stays usable.
- A model request whose tool results still contain either is refused.
- The user's own messages aren't checked. What the user types is theirs to send, and refusing it would break the Thread for good.

**Aliasing.** With a Connection's `alias_identifiers` setting on, the gateway replaces every schema, relation, column and constraint name with a stable alias such as `table_3`. It does this by whole word, in the user's messages and in tool results; JSON is walked so names behind escapes are caught. In the model's tool arguments it puts the real names back. Matching whole words also catches names inside catalog values, such as index definitions and `pg_class.relname`. Restoring whole words in the agent's SQL also covers names inside string literals (`WHERE relname = 'table_3'`). The alias map is kept in the local SQLite store, so aliases stay stable across Turns and restarts. Every Turn adds new names to it from a catalog template. `Translator` restores real names in every event, so the CLI and any later front end show them. The proposal called the setting `privacy.redact_identifiers`; it is `alias_identifiers` here.

## Considered Options

- **Resolving output columns against the live catalog:** rejected, as for the guard (ADR 0004). It couples the check to a connection, and conservative name resolution only refuses more.
- **Filtering result rows after execution:** rejected. A row value would already have been read and logged, and a value-shaped aggregate can't be told from a row value afterwards.
- **Sealing tool results (signing each and checking the signature in the gateway):** rejected. deepagents' own tools (`read_file` of evicted results, `task`) produce results that no seal covers.
- **Aliasing by rewriting the parse tree:** rejected. Names also appear inside catalog values and string literals, which a parse tree doesn't cover.

## Consequences

- Entity keys of an unqualified table are assumed to be in `public`. Text entity keys pass in clear until hashing (#20, ADR 0001) lands. Neither matters until entity maps exist.
- A filter is still an oracle: `count(*) … WHERE email = '…'` says whether that email exists. Only counts and sizes come out, but stopping that would mean refusing filters on anything but entity keys.
- The gateway catches only personal data it recognises. A leak of other row data relies on the output check and the templates' declared columns.
- With aliasing on:
  - an English word that is also a table or column name is aliased wherever it appears, including JSON keys of tool results;
  - names that aren't plain identifiers aren't aliased;
  - a restored name that needs quoting gives invalid SQL, which fails rather than leaks;
  - function, type and trigger names aren't aliased.
- Adding a tool changes the tools offered, so every cassette must be re-recorded.
