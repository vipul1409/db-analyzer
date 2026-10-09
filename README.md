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

Start a subset of fixtures with `make db-up PG="pg15 pg16"` and point the integration tests at them with `DBX_TEST_PG_VERSIONS=15,16`.
