COMPOSE := docker compose -f tests/fixtures/docker-compose.yml -p db-analyzer-fixtures
# Subset of fixtures to start, e.g. `make db-up PG="pg15 pg16"`. Empty = all four.
PG ?=

.PHONY: check lint typecheck test test-integration db-up db-down fmt

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
	uv run pytest -m "not integration" || [ $$? -eq 5 ]

test-integration:
	uv run pytest -m integration

db-up:
	$(COMPOSE) up -d --build --wait $(PG)

db-down:
	$(COMPOSE) down -v
