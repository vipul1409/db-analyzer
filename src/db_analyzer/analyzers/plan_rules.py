"""Deterministic plan rules: why a slow statement is slow, read from its generic plan.

Store-agnostic: rules read a normalized PlanNode tree plus what the catalog says about the
relations it touches. A generic plan holds the planner's estimates, never measured rows, so
every rule is about what the planner expects to do. Each rule fires at most once per statement,
on the node it is about, which its reason quotes as evidence.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from db_analyzer.analyzers.inventory import stats_are_stale
from db_analyzer.core.model import PlanNode, RelationEstimate, UnindexedForeignKey
from db_analyzer.core.units import format_bytes

# A relation is large enough for a full read of it to matter from this many rows.
LARGE_RELATION_ROWS = 10_000
# A filter is selective when it keeps at most this share of the relation's rows.
SELECTIVE_SHARE = 0.05
# An index scan is not selective when it returns at least this share of the relation's rows.
NON_SELECTIVE_SHARE = 0.10
# A nested loop is big when its inner side runs this many times, over this many rows in all.
BIG_LOOP_OUTER_ROWS = 1_000
BIG_LOOP_ROWS = 100_000
# A plan's estimate is large, and stale statistics make it matter, from this many rows.
LARGE_ESTIMATE_ROWS = 10_000

_SCANS_BY_INDEX = ("Index Scan", "Index Only Scan", "Bitmap Heap Scan")
_SORTS = ("Sort", "Incremental Sort")
_HASHED = ("Hashed", "Mixed")


@dataclass(frozen=True)
class PlanContext:
    """What the catalog says about the relations a plan reads."""

    relations: Mapping[str, RelationEstimate]
    unindexed_foreign_keys: Sequence[UnindexedForeignKey] = field(default_factory=list)


@dataclass(frozen=True)
class PlanReason:
    """One rule that fired: the node it is about (one line of plan text) and why it matters."""

    rule: str
    node: str
    explanation: str


Rule = Callable[[PlanNode, PlanContext, int], PlanReason | None]


def evaluate(plan: PlanNode, ctx: PlanContext, *, temp_blks_written: int) -> list[PlanReason]:
    """Every rule that fires on `plan`, in rule order. `temp_blks_written` is what the workload
    source measured for the statement: it confirms a spill the plan only makes likely."""
    return [r for rule in RULES.values() if (r := rule(plan, ctx, temp_blks_written))]


def _rows(ctx: PlanContext, node: PlanNode) -> int:
    rel = ctx.relations.get(node.relation or "")
    return rel.rows if rel else 0


def seq_scan_selective_filter(plan: PlanNode, ctx: PlanContext, _temp: int) -> PlanReason | None:
    for n in plan.walk():
        total = _rows(ctx, n)
        if (
            n.node_type == "Seq Scan"
            and n.filter
            and total >= LARGE_RELATION_ROWS
            and n.rows <= SELECTIVE_SHARE * total
        ):
            return PlanReason(
                "seq_scan_selective_filter",
                n.line(),
                f"It reads all {total:,} rows of {n.relation} to keep about {n.rows:,} "
                f"({n.rows / total:.1%}): an index on the filtered columns would find them "
                "directly.",
            )
    return None


def big_nested_loop(plan: PlanNode, ctx: PlanContext, _temp: int) -> PlanReason | None:
    for n in plan.walk():
        if n.node_type != "Nested Loop" or len(n.children) != 2:
            continue
        outer, inner = n.children
        if outer.rows >= BIG_LOOP_OUTER_ROWS and outer.rows * inner.rows >= BIG_LOOP_ROWS:
            return PlanReason(
                "big_nested_loop",
                n.line(),
                f"A nested loop runs its inner side ({inner.node_type}) once for each of "
                f"{outer.rows:,} outer rows, about {outer.rows * inner.rows:,} rows in all: a "
                "join condition the planner can hash or merge on, or fewer outer rows, would "
                "avoid it.",
            )
    return None


def spill_prone_sort_or_hash(plan: PlanNode, ctx: PlanContext, temp: int) -> PlanReason | None:
    if temp <= 0:
        return None
    candidates = [
        n
        for n in plan.walk()
        if n.node_type in (*_SORTS, "Hash")
        or (n.node_type in ("Aggregate", "SetOp") and n.strategy in _HASHED)
    ]
    if not candidates:
        return None
    n = max(candidates, key=lambda c: c.rows * c.width)
    return PlanReason(
        "spill_prone_sort_or_hash",
        n.line(),
        f"Its {n.node_type.lower()} holds about {format_bytes(n.rows * n.width)} "
        f"({n.rows:,} rows), and the statement wrote {format_bytes(temp * 8192)} of temporary "
        "files: it spills past work_mem. Raise work_mem for it, or sort or hash fewer or "
        "narrower rows.",
    )


def unindexed_fk_filter(plan: PlanNode, ctx: PlanContext, _temp: int) -> PlanReason | None:
    for n in plan.walk():
        for fk in ctx.unindexed_foreign_keys:
            if n.relation == fk.table.qualified and set(fk.columns) <= set(n.filter_columns):
                columns = ", ".join(fk.columns)
                return PlanReason(
                    "unindexed_fk_filter",
                    n.line(),
                    f"It filters {n.relation} on its foreign key ({columns}), which no index "
                    f"starts with: index ({columns}) and the filter becomes an index lookup.",
                )
    return None


def non_selective_index_scan(plan: PlanNode, ctx: PlanContext, _temp: int) -> PlanReason | None:
    for n in plan.walk():
        total = _rows(ctx, n)
        if n.node_type not in _SCANS_BY_INDEX or total < LARGE_RELATION_ROWS:
            continue
        # Walking a whole index in order is fine when every row is wanted (ORDER BY … LIMIT
        # with no filter); with a filter, few matches mean most of the table is read.
        whole = n.node_type != "Bitmap Heap Scan" and not n.condition and n.filter
        if whole:
            return PlanReason(
                "non_selective_index_scan",
                n.line(),
                f"It walks the whole index {n.index} in order and filters each of "
                f"{n.relation}'s {total:,} rows: when few rows match, it reads most of the "
                "table. An index that leads with the filtered columns would find them "
                "directly.",
            )
        if n.condition and n.rows >= NON_SELECTIVE_SHARE * total:
            return PlanReason(
                "non_selective_index_scan",
                n.line(),
                f"Its index scan returns about {n.rows:,} of {n.relation}'s {total:,} rows "
                f"({n.rows / total:.0%}): fetching that many rows through an index costs more "
                "than reading the table. A more selective index, or a covering one, would help.",
            )
    return None


def stale_stats_estimate(plan: PlanNode, ctx: PlanContext, _temp: int) -> PlanReason | None:
    for n in plan.walk():
        rel = ctx.relations.get(n.relation or "")
        if rel and _stale(rel) and n.rows >= LARGE_ESTIMATE_ROWS:
            return PlanReason(
                "stale_stats_estimate",
                n.line(),
                f"It plans for about {n.rows:,} rows of {n.relation}, whose statistics are "
                f"stale: the planner may be choosing joins and scans for the wrong row counts. "
                f"ANALYZE {n.relation} and plan it again.",
            )
    return None


def _stale(rel: RelationEstimate) -> bool:
    return stats_are_stale(
        analyzed=rel.analyzed,
        live_rows=rel.live_rows,
        modified_since_analyze=rel.modified_since_analyze,
        estimate=rel.estimated_rows,
    )


RULES: dict[str, Rule] = {
    "seq_scan_selective_filter": seq_scan_selective_filter,
    "unindexed_fk_filter": unindexed_fk_filter,
    "non_selective_index_scan": non_selective_index_scan,
    "big_nested_loop": big_nested_loop,
    "spill_prone_sort_or_hash": spill_prone_sort_or_hash,
    "stale_stats_estimate": stale_stats_estimate,
}
