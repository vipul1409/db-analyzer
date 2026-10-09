"""Identifier aliasing (proposal §3.5): schema, table and column names the LLM never sees."""

import json

from db_analyzer.safety.aliases import Aliases, assign

FOUND = [
    ("schema", "public"),
    ("table", "events"),
    ("table", "accounts"),
    ("column", "tenant_id"),
    ("index", "idx_events_tenant"),
]


def aliases() -> Aliases:
    return Aliases(assign({}, FOUND))


def test_names_are_replaced_as_whole_words_and_restored() -> None:
    a = aliases()
    sql = "SELECT tenant_id, count(*) FROM public.events GROUP BY 1 -- eventsx"

    hidden = a.alias(sql)

    assert hidden == "SELECT column_1, count(*) FROM schema_1.table_1 GROUP BY 1 -- eventsx"
    assert a.unalias(hidden) == sql


def test_json_keys_and_values_are_aliased_even_behind_escapes() -> None:
    content = json.dumps(
        {"tenant_id": 7, "table": "public.events", "def": "CREATE INDEX\nidx_events_tenant"}
    )

    hidden = aliases().alias_content(content)

    assert json.loads(hidden) == {
        "column_1": 7,
        "table": "schema_1.table_1",
        "def": "CREATE INDEX\nindex_1",
    }


def test_aliases_are_stable_and_new_names_get_the_next_number() -> None:
    first = assign({}, FOUND)

    later = assign(first, [*FOUND, ("table", "audit_log")])

    assert later == {"audit_log": "table_3"}


def test_an_alias_never_collides_with_a_real_name() -> None:
    assert assign({}, [("table", "table_1"), ("table", "events")]) == {
        "table_1": "table_2",
        "events": "table_3",
    }


def test_a_name_used_as_two_kinds_keeps_one_alias() -> None:
    assert assign({}, [("table", "events"), ("column", "events")]) == {"events": "table_1"}
