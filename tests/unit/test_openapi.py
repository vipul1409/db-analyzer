"""The OpenAPI spec is the contract the web UI's client is generated from."""

import json
from pathlib import Path
from typing import Any

from db_analyzer.api.openapi import SPEC_PATH, spec


def test_the_export_is_deterministic() -> None:
    assert json.dumps(spec()) == json.dumps(spec())


def test_the_committed_spec_is_current() -> None:
    """If this fails, run `make api-client` and commit the result."""
    assert json.loads(Path(SPEC_PATH).read_text()) == spec()


def test_agent_events_are_a_discriminated_union_on_type() -> None:
    schemas: dict[str, Any] = spec()["components"]["schemas"]
    union = schemas["AgentEvent"]

    assert union["discriminator"]["propertyName"] == "type"
    mapping = union["discriminator"]["mapping"]
    assert {"token", "subagent_started", "subagent_finished", "done"} <= set(mapping)
    for ref in mapping.values():
        event = schemas[ref.removeprefix("#/components/schemas/")]
        assert set(event["properties"]) == set(event["required"]), "every field is always sent"


def test_a_message_streams_agent_events() -> None:
    [path] = [p for p in spec()["paths"] if p.endswith("/messages")]
    ok = spec()["paths"][path]["post"]["responses"]["200"]

    assert ok["content"]["text/event-stream"]["schema"] == {
        "$ref": "#/components/schemas/AgentEvent"
    }
