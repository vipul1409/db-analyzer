import argparse
import json

from db_analyzer.adapters.postgres import plans
from db_analyzer.adapters.postgres.session import open_session
from db_analyzer.core.model import SessionLimits
from db_analyzer.safety.executor import SafeExecutor
from tests.fixtures.dataset import DATABASE, fixture_dsn
from tests.fixtures.plans import DIRECTORY, name, normalized_statements

parser = argparse.ArgumentParser(prog="python -m tests.fixtures.plans")
parser.add_argument("--pg", type=int, action="append", required=True, help="e.g. 15")
args = parser.parse_args()

for major in args.pg:
    out = DIRECTORY / f"pg{major}"
    out.mkdir(exist_ok=True)
    with open_session(fixture_dsn(major, "db_analyzer", DATABASE), SessionLimits()) as conn:
        executor = SafeExecutor(conn, "capture", audit=lambda _: None)
        for match, text in normalized_statements(major).items():
            target = plans.plannable(text)
            assert isinstance(target, plans.Plannable), target
            raw = plans.explain(executor, target.sql)
            (out / f"{name(match)}.json").write_text(json.dumps(raw, indent=1) + "\n")
            print(f"PG {major}: {name(match)}")
