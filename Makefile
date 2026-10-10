COMPOSE := docker compose -f tests/fixtures/docker-compose.yml -p db-analyzer-fixtures
# Fixtures to start, e.g. `make db-up PG="pg15 pg16"`. Narrowed to pg17 during development.
PG ?= pg17

.PHONY: check lint typecheck test test-integration db-up db-down db-seed fmt api-client web-check web serve

check: lint typecheck test

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

# Exit code 5 = no tests collected; an empty suite counts as passing.
test:
	uv run pytest -m "not integration and not spike" || [ $$? -eq 5 ]

test-integration:
	uv run pytest -m integration

# The OpenAPI spec and the web UI's TypeScript client generated from it: run after changing the
# API, and commit both (CI fails when they drift). Needs `npm --prefix web ci` once.
api-client:
	uv run python -m db_analyzer.api.openapi
	npm --prefix web run gen

# Typecheck and build web/ (the build is the UI's only check: it has no component tests).
web-check:
	npm --prefix web run build

# The web UI's dev server on http://localhost:5173, proxying /api to `make serve` (run both).
web:
	npm --prefix web run dev

serve:
	uv run dbx serve

db-up:
	$(COMPOSE) up -d --build --wait $(PG)

# Seed the synthetic 'shop' database: `make db-seed [PG="pg15 17"] [SCALE=full]`.
SCALE ?= ci
db-seed:
	@for v in $(or $(subst pg,,$(PG)),17); do \
		uv run python -m tests.fixtures.dataset --pg $$v --scale $(SCALE) || exit 1; \
	done

db-down:
	$(COMPOSE) down -v
