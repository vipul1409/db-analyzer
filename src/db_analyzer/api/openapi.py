"""Export the API's OpenAPI spec: the contract the web UI's TypeScript client is generated from
(`make api-client`). CI regenerates both and fails when either differs from what is committed.

    uv run python -m db_analyzer.api.openapi [path]
"""

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from db_analyzer.api.app import create_app
from db_analyzer.service import AnalyzerService

SPEC_PATH = Path(__file__).parents[3] / "web" / "openapi.json"


def spec() -> dict[str, Any]:
    """The spec of the app every `dbx serve` runs; no service method is called."""
    with tempfile.TemporaryDirectory() as home:
        return create_app(AnalyzerService(home=Path(home))).openapi()


def main(argv: list[str]) -> None:
    path = Path(argv[0]) if argv else SPEC_PATH
    path.write_text(json.dumps(spec(), indent=2) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
