# DB Analyzer

A conversational agent that analyses a PostgreSQL database through a read-only login. See `docs/proposal.md` for the design, `docs/execution-plan.md` for the plan and `CONTEXT.md` for the domain glossary.

## Usage

Create the read-only login with `docs/setup/postgres_role.sql` (Azure notes in `docs/setup/azure.md`), then:

```sh
export DBX_DSN='postgresql://db_analyzer:<secret>@host:5432/app'
uv run dbx connect prod     # probe the database and show what the analyzer can see
```

The DSN stays in the environment; only the variable name is stored (in `~/.db-analyzer`, or `$DBX_HOME`).

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync                     # create the virtualenv
uv run pre-commit install   # lint and typecheck on commit
make check                  # lint, types, unit tests
make db-up                  # start Postgres 14–18 fixtures (ports 5414–5418; 14 tests refusal)
make test-integration       # integration tests against the fixtures
make db-down                # stop and remove the fixtures
```

### Synthetic dataset

`make db-seed PG=17` creates a `shop` database on that fixture: a multi-tenant schema with seeded problems (a hot tenant, unindexed foreign keys, duplicate/unused/invalid indexes, bloat, stale statistics, a tenant-partitioned table) and a replayed workload in `pg_stat_statements`. `tests/fixtures/dataset/ground_truth.json` lists the Findings it should produce. Integration tests seed it automatically at CI scale (about a second per version).

`make db-seed PG=17 SCALE=full` seeds roughly 5–10 GB for manual and scale testing; expect it to take several minutes.

Hot standbys for PG 15 and 16 (ports 5515/5516) are behind a compose profile: `make db-up PG="pg15 pg16 pg15-standby pg16-standby"`. Spikes (`tests/spikes`) are run by hand with `uv run pytest -m spike -s`.

Start a subset of fixtures with `make db-up PG="pg15 pg16"` and point the integration tests at them with `DBX_TEST_PG_VERSIONS=15,16`.
