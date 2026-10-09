# DB Analyzer

A conversational agent that analyses a PostgreSQL database through a read-only login. See `docs/proposal.md` for the design, `docs/execution-plan.md` for the plan and `CONTEXT.md` for the domain glossary.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync                     # create the virtualenv
uv run pre-commit install   # lint and typecheck on commit
make check                  # lint, types, unit tests
make db-up                  # start Postgres 15–18 fixtures (ports 5415–5418)
make test-integration       # integration tests against the fixtures
make db-down                # stop and remove the fixtures
```

Start a subset of fixtures with `make db-up PG="pg15 pg16"` and point the integration tests at them with `DBX_TEST_PG_VERSIONS=15,16`.
