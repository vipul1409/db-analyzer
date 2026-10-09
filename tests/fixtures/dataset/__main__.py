import argparse
import time

from tests.fixtures.dataset import DATABASE, SCALES, fixture_dsn, seed

parser = argparse.ArgumentParser(prog="python -m tests.fixtures.dataset")
parser.add_argument("--pg", type=int, required=True, help="fixture major version, e.g. 17")
parser.add_argument("--scale", choices=sorted(SCALES), default="ci")
args = parser.parse_args()

started = time.monotonic()
seed(fixture_dsn(args.pg, "postgres", "postgres"), args.scale)
elapsed = time.monotonic() - started
print(f"Seeded '{DATABASE}' on PG {args.pg} at {args.scale} scale in {elapsed:.0f}s")
