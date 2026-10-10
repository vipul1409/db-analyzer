"""Synthetic multi-tenant 'shop' database with known problems, plus its ground truth.

    uv run python -m tests.fixtures.dataset --pg 17 [--scale ci|full]

Recreates the 'shop' database on that fixture, seeds it, then replays a workload so
pg_stat_statements holds the slow statements listed in ground_truth.json.
"""

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

GROUND_TRUTH: dict[str, Any] = json.loads((Path(__file__).parent / "ground_truth.json").read_text())
DATABASE: str = GROUND_TRUTH["database"]
HOT_TENANT: int = GROUND_TRUTH["hot_tenant_id"]
HOT_SHARE = 0.35


def fixture_dsn(major: int, role: str = "db_analyzer", database: str = "app") -> str:
    """DSN of a Docker fixture (port 5400 + major). Fixture roles use their name as password."""
    return f"postgresql://{role}:{role}@localhost:{5400 + major}/{database}"


@dataclass(frozen=True)
class Scale:
    tenants: int
    accounts: int
    bookings: int
    items: int
    events: int
    audit: int
    legacy: int
    usage: int
    links: int
    replay_calls: int

    def __post_init__(self) -> None:
        if self.tenants <= HOT_TENANT:
            raise ValueError(f"need more than {HOT_TENANT} tenants (hot tenant is {HOT_TENANT})")


SCALES = {
    # Seconds per PG version; used by CI and the integration tests.
    "ci": Scale(
        tenants=20,
        accounts=2_000,
        bookings=20_000,
        items=60_000,
        events=60_000,
        audit=20_000,
        legacy=50_000,
        usage=20_000,
        links=30_000,
        replay_calls=50,
    ),
    # Roughly 5-10 GB; for manual runs and the scale tests.
    "full": Scale(
        tenants=200,
        accounts=500_000,
        bookings=8_000_000,
        items=24_000_000,
        events=12_000_000,
        audit=4_000_000,
        legacy=5_000_000,
        usage=4_000_000,
        links=3_000_000,
        replay_calls=200,
    ),
}

# Tables only; foreign keys and secondary indexes are added after the bulk load (CONSTRAINTS).
TABLES = """
CREATE TABLE tenants (id bigint PRIMARY KEY, name text NOT NULL, plan text NOT NULL);
CREATE TABLE accounts (
  id bigint PRIMARY KEY, tenant_id bigint NOT NULL, email text NOT NULL,
  created_at timestamptz NOT NULL);
CREATE TABLE bookings (
  id bigint PRIMARY KEY, account_id bigint NOT NULL, tenant_id bigint NOT NULL,
  status text NOT NULL, starts_at timestamptz NOT NULL, amount numeric(12,2) NOT NULL,
  notes text);
CREATE TABLE booking_items (
  id bigint PRIMARY KEY, booking_id bigint NOT NULL, sku text NOT NULL, qty int NOT NULL,
  price numeric(10,2) NOT NULL);
CREATE TABLE events (
  id bigint PRIMARY KEY, tenant_id bigint NOT NULL, kind text NOT NULL, payload jsonb NOT NULL,
  created_at timestamptz NOT NULL);
CREATE TABLE audit_log (
  id bigint PRIMARY KEY, tenant_id bigint NOT NULL, actor text NOT NULL, action text NOT NULL,
  at timestamptz NOT NULL
) WITH (autovacuum_enabled = false);
CREATE TABLE legacy_imports (id bigint PRIMARY KEY, source text NOT NULL, raw text NOT NULL)
  WITH (autovacuum_enabled = false);
CREATE TABLE usage_records (
  tenant_id bigint NOT NULL, id bigint NOT NULL, metric text NOT NULL,
  quantity bigint NOT NULL, at timestamptz NOT NULL, PRIMARY KEY (tenant_id, id)
) PARTITION BY LIST (tenant_id);
-- A second schema, for the schema rollup. Index-heavy (seeded): a narrow link table whose
-- primary key and reversed unique index outweigh its heap.
CREATE SCHEMA reference;
CREATE TABLE reference.sku_categories (
  sku text NOT NULL, category_id int NOT NULL, PRIMARY KEY (sku, category_id),
  UNIQUE (category_id, sku));
"""

# Formatted with str.format (integers only); one multi-statement string. Row i is 1-based.
# Accounts belong to tenant 1 + (account % tenants); bookings inherit their account's tenant.
# pg_temp.cold_tenant(i) spreads rows over every tenant except the hot one.
DATA = """
CREATE FUNCTION pg_temp.cold_tenant(i bigint) RETURNS bigint LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN t >= {hot} THEN t + 1 ELSE t END FROM (SELECT 1 + i % ({tenants} - 1) AS t) x
$$;
INSERT INTO tenants SELECT i, 'tenant-' || i, (ARRAY['free','team','enterprise'])[1 + i % 3]
  FROM generate_series(1, {tenants}) i;
INSERT INTO accounts
  SELECT i, 1 + i % {tenants}, 'user' || i || '@example.test', now() - i * interval '1 minute'
  FROM generate_series(1, {accounts}) i;
INSERT INTO bookings
  SELECT i, a, 1 + a % {tenants}, CASE WHEN i % 10 = 0 THEN 'pending' ELSE 'done' END,
         now() + i * interval '1 minute', (i % 500) + 0.99, md5(i::text)
  FROM generate_series(1, {bookings}) i, LATERAL (SELECT 1 + i % {accounts} AS a) x;
INSERT INTO booking_items
  SELECT i, 1 + i % {bookings}, 'sku-' || (i % 997), 1 + i % 5, (i % 100) + 0.5
  FROM generate_series(1, {items}) i;

-- events: the hot tenant has HOT_SHARE of the rows and bigger payloads (~50% of bytes).
INSERT INTO events
  SELECT i, {hot}, 'kind-' || (i % 7),
         jsonb_build_object('k', md5(i::text), 'data', repeat(md5((-i)::text), 12)),
         now() - i * interval '1 second'
  FROM generate_series(1, {hot_events}) i;
INSERT INTO events
  SELECT {hot_events} + i, pg_temp.cold_tenant(i), 'kind-' || (i % 7),
         jsonb_build_object('k', md5(i::text), 'data', repeat(md5((-i)::text), 5)),
         now() - i * interval '1 second'
  FROM generate_series(1, {cold_events}) i;

INSERT INTO audit_log
  SELECT i, 1 + i % {tenants}, 'user' || (i % 50), 'login', now() - i * interval '1 second'
  FROM generate_series(1, {audit}) i;

INSERT INTO usage_records
  SELECT CASE WHEN i <= {hot_usage} THEN {hot} ELSE pg_temp.cold_tenant(i) END,
         i, 'api_calls', i % 1000, now() - i * interval '1 second'
  FROM generate_series(1, {usage}) i;

INSERT INTO reference.sku_categories
  SELECT 'sku-' || (i % 997), i / 997 FROM generate_series(0, {links} - 1) i;
"""

CONSTRAINTS = """
ALTER TABLE accounts ADD FOREIGN KEY (tenant_id) REFERENCES tenants;
ALTER TABLE bookings ADD FOREIGN KEY (account_id) REFERENCES accounts;      -- no index (seeded)
ALTER TABLE bookings ADD FOREIGN KEY (tenant_id) REFERENCES tenants;        -- no index (seeded)
ALTER TABLE booking_items ADD FOREIGN KEY (booking_id) REFERENCES bookings; -- no index (seeded)
ALTER TABLE events ADD FOREIGN KEY (tenant_id) REFERENCES tenants;
ALTER TABLE audit_log ADD FOREIGN KEY (tenant_id) REFERENCES tenants;       -- no index (seeded)
CREATE INDEX idx_accounts_tenant ON accounts (tenant_id);
CREATE INDEX idx_accounts_tenant_dup ON accounts (tenant_id);    -- duplicate (seeded)
CREATE INDEX idx_accounts_created_at ON accounts (created_at);   -- never used (seeded)
CREATE INDEX idx_events_tenant_created ON events (tenant_id, created_at);
"""

# Slow statements the ground truth expects, plus statements that use the indexes which are
# meant to look healthy (so only the seeded unused index stays at zero scans).
WORKLOAD = [
    ("SELECT id, status, amount FROM bookings WHERE account_id = %s", "account"),
    ("SELECT sum(qty * price) FROM booking_items WHERE booking_id = %s", "booking"),
    ("UPDATE bookings SET amount = amount WHERE tenant_id = %s AND status = 'pending'", "tenant"),
    ("SELECT count(*) FROM accounts WHERE tenant_id = %s", "tenant"),
    (
        "SELECT id FROM events WHERE tenant_id = %s AND created_at > now() - interval '1 minute'",
        "tenant",
    ),
    # One per remaining plan rule (issue #15): an inequality join the planner runs as a nested
    # loop over every booking; an ORDER BY ... LIMIT that walks a whole index and filters; and a
    # scan of the table whose statistics are stale.
    ("SELECT count(*) FROM bookings b JOIN tenants t ON b.tenant_id < t.id", "none"),
    ("SELECT id FROM events WHERE kind = %s ORDER BY tenant_id, created_at LIMIT 20", "kind"),
    ("SELECT count(*) FROM legacy_imports WHERE source = %s", "source"),
]
# Full sort of every event's key with tiny work_mem: spills to temp files. No literal in the
# ORDER BY, so pg_stat_statements' normalized text is stable for the ground-truth match.
SPILLING_SORT = "SELECT count(*) FROM (SELECT payload->>'k' AS k FROM events ORDER BY k OFFSET 0) s"


def seed(admin_dsn: str, scale: str = "ci") -> None:
    """Recreate and seed the 'shop' database. `admin_dsn` is a superuser DSN to any other DB."""
    s = SCALES[scale]
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {DATABASE}")
    shop_dsn = psycopg.conninfo.make_conninfo(admin_dsn, dbname=DATABASE)
    with psycopg.connect(shop_dsn, autocommit=True) as conn:
        for ext in ("pg_stat_statements", "pgstattuple", "hypopg"):
            conn.execute(f"CREATE EXTENSION {ext}")
        conn.execute(TABLES)
        for t in range(1, s.tenants + 1):
            conn.execute(
                f"CREATE TABLE usage_records_t{t} PARTITION OF usage_records FOR VALUES IN ({t})"
            )
        hot_events = round(s.events * HOT_SHARE)
        params = {
            **s.__dict__,
            "hot": HOT_TENANT,
            "hot_events": hot_events,
            "cold_events": s.events - hot_events,
            "hot_usage": round(s.usage * HOT_SHARE),
        }
        conn.execute(DATA.format(**params).encode())
        conn.execute(CONSTRAINTS)
        # Forget scans made by loading and FK validation, before the problems are seeded
        # (resetting later would also wipe their dead-tuple and last_analyze signals).
        _flush_stats(conn)
        conn.execute("SELECT pg_stat_reset()")
        _seed_problems(conn, s)
        conn.execute(f"GRANT CONNECT ON DATABASE {DATABASE} TO db_analyzer")
        for schema in ("public", "reference"):
            conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO db_analyzer")
            conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO db_analyzer")
        conn.execute("SELECT pg_stat_statements_reset()")
        _replay(conn, s)


def _seed_problems(conn: psycopg.Connection[Any], s: Scale) -> None:
    # Bloat: autovacuum is off, so two full updates leave ~2 dead tuples per live row.
    conn.execute("UPDATE audit_log SET action = 'logout'")
    conn.execute("UPDATE audit_log SET action = 'login'")
    # Invalid index: a concurrent unique build fails on duplicates and stays behind, invalid.
    with contextlib.suppress(psycopg.errors.UniqueViolation):
        conn.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY idx_bookings_status_unique ON bookings (status)"
        )
    _flush_stats(conn)  # so ANALYZE's live and dead counts are not added to afterwards
    # Fresh statistics everywhere except the stale-stats table, which is analyzed while tiny and
    # then bulk-loaded with autovacuum off.
    conn.execute(
        "ANALYZE tenants, accounts, bookings, booking_items, events, audit_log, usage_records,"
        " reference.sku_categories"
    )
    conn.execute(
        "INSERT INTO legacy_imports SELECT i, 'csv', md5(i::text) FROM generate_series(1, 100) i"
    )
    conn.execute("ANALYZE legacy_imports")
    conn.execute(
        "INSERT INTO legacy_imports SELECT i, 'csv', md5(i::text) FROM generate_series(101, %s) i",
        (s.legacy,),
    )


def _flush_stats(conn: psycopg.Connection[Any]) -> None:
    """Report this backend's pending table counters now. A backend flushes them at most once a
    second, so without this they land after a later pg_stat_reset() or ANALYZE and double the
    live, dead and modified counts."""
    conn.execute("SELECT pg_stat_force_next_flush()")
    conn.execute("SELECT 1")  # the flush happens when the backend next goes idle


def _replay(conn: psycopg.Connection[Any], s: Scale) -> None:
    """Run the workload as an application would: parameterized statements, so
    pg_stat_statements records them with $n placeholders."""
    for sql, kind in WORKLOAD:
        calls = s.replay_calls if kind in ("account", "booking") else max(5, s.replay_calls // 10)
        for i in range(calls):
            key = {
                "account": 1 + (i * 37) % s.accounts,
                "booking": 1 + (i * 101) % s.bookings,
                "tenant": 1 + i % s.tenants,
                "kind": f"kind-{i % 7}",
                "source": "csv",
                "none": None,
            }[kind]
            conn.execute(sql, () if key is None else (key,))
    conn.execute("SET work_mem = '64kB'")
    for _ in range(3):
        conn.execute(SPILLING_SORT)
    conn.execute("RESET work_mem")
