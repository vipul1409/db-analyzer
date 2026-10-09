"""Privacy filter (proposal §3.5): the LLM sees metadata, aggregates and entity keys only.

`check_output` runs on every SELECT the LLM wrote, before it executes. Each output expression
must be built only from:

- counts (`count`, `regr_count`), over anything;
- other aggregates (sum, avg, min, max, stddev, percentiles, ...) of sizes and lengths
  (`sum(length(notes))`), or of something that is itself allowed: over one row or a group of
  one, these return the row's value;
- catalog metadata: columns of catalog relations, except the few that hold sampled or literal
  row values (VALUE_BEARING);
- confirmed entity keys;
- constants, parameters, and operators or functions over the above.

Column names are resolved through FROM items, joins, subqueries and CTEs by name, without the
live catalog. A name that might come from a user table, and is not a confirmed entity key
there, counts as row data. Clauses that produce no output (WHERE, GROUP BY, ORDER BY) are not
checked: what a filter reveals about rows can only come out through an aggregate.

`pg_stats` value columns (most_common_vals, histogram bounds) depend on the row: they pass only
as bare columns of a lone `pg_stats` next to its schemaname, tablename and attname, and
`OutputFilter.apply` withholds them for every row that is not an entity key.

`find_personal_data` is the last-line check the LLMGateway re-applies to outgoing messages.

Known limits: entity keys of an unqualified table are assumed to be in `public`; whole-row
references and `*` over a user table always count as row data.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Literal

from pglast import ast, parse_sql
from pglast.enums import SubLinkType

from db_analyzer.core.model import EntityKey, PrivacyRejected
from db_analyzer.safety.guard import children, is_catalog_relation

Row = dict[str, Any]


class Verdict(IntEnum):
    SAFE = 0
    GATED = 1  # pg_stats values: only bare, beside their column's identity
    UNSAFE = 2


SAFE, GATED, UNSAFE = Verdict.SAFE, Verdict.GATED, Verdict.UNSAFE

# Aggregates that only count their input.
COUNTING = frozenset({"count", "regr_count"})
# Sizes and lengths: what any other aggregate may range over.
MEASURES = frozenset(
    {
        "length",
        "char_length",
        "character_length",
        "octet_length",
        "bit_length",
        "pg_column_size",
        "array_length",
        "cardinality",
        "json_array_length",
        "jsonb_array_length",
    }
)
# Functions that return another session's statement text, literals included.
VALUE_BEARING_FUNCTIONS = frozenset({"pg_stat_get_activity", "pg_stat_get_backend_activity"})

_PG_STATS = "pg_stats"
_PG_STATS_IDENTITY = ("schemaname", "tablename", "attname")
# Catalog columns holding sampled or literal row values (or secrets).
VALUE_BEARING: dict[tuple[str, str], Verdict] = {
    **{
        (_PG_STATS, c): GATED
        for c in (
            "most_common_vals",
            "most_common_elems",
            "histogram_bounds",
            "range_bounds_histogram",
        )
    },
    **{
        (rel, c): UNSAFE
        for rel, cols in {
            "pg_stats_ext": ("most_common_vals",),
            "pg_stats_ext_exprs": ("most_common_vals", "most_common_elems", "histogram_bounds"),
            "pg_statistic": tuple(f"stavalues{i}" for i in range(1, 6)),
            "pg_statistic_ext_data": ("stxdmcv", "stxdexpr"),
            "pg_stat_activity": ("query",),
            # Normalized, except utility statements (SET, DDL, ...), which keep their literals.
            # Workload tools pass on normalized text only.
            "pg_stat_statements": ("query",),
            "pg_prepared_statements": ("statement",),
            "pg_largeobject": ("data",),
            "pg_user_mapping": ("umoptions",),
            "pg_user_mappings": ("umoptions",),
            "user_mapping_options": ("option_value",),
            "pg_authid": ("rolpassword",),
            "pg_shadow": ("passwd",),
        }.items()
        for c in cols
    },
}

WITHHELD = "[withheld: not an entity key]"


@dataclass(frozen=True)
class _Gated:
    """A pg_stats value column in the output, and the outputs naming its column."""

    output: str
    schema: str
    table: str
    column: str


@dataclass(frozen=True)
class OutputFilter:
    """What still has to happen to a checked statement's rows after it runs."""

    entity_keys: frozenset[EntityKey] = frozenset()
    gated: tuple[_Gated, ...] = ()

    def apply(self, rows: list[Row]) -> list[Row]:
        if not self.gated:
            return rows
        return [self._row(r) for r in rows]

    def _row(self, row: Row) -> Row:
        out = dict(row)
        for g in self.gated:
            key = EntityKey(str(row[g.schema]), str(row[g.table]), str(row[g.column]))
            if key not in self.entity_keys:
                out[g.output] = WITHHELD
        return out


def check_output(sql: str, entity_keys: frozenset[EntityKey]) -> OutputFilter:
    """Refuse a SELECT whose output could carry row data. Other statements pass unchanged."""
    stmt = parse_sql(sql)[0].stmt
    if not isinstance(stmt, ast.SelectStmt):
        return OutputFilter()
    checker = _Checker(entity_keys)
    scope, outputs = checker.select(stmt, [])
    gated = [o for o in outputs if o.verdict == GATED]
    for o in outputs:
        if o.verdict == UNSAFE:
            raise PrivacyRejected(
                f"privacy: output {o.name!r} is neither an aggregate, catalog metadata nor a "
                "confirmed entity key"
            )
    return OutputFilter(entity_keys, tuple(_gate(o, outputs, scope) for o in gated))


def _gate(o: "_Output", outputs: list["_Output"], scope: "_Scope | None") -> _Gated:
    """A pg_stats value column passes only bare, from a lone pg_stats, beside its identity."""
    lone_pg_stats = (
        scope is not None
        and len(scope.sources) == 1
        and scope.sources[0].kind == "catalog"
        and scope.sources[0].relname == _PG_STATS
    )
    bare = {p.column: p.name for p in outputs if p.column is not None}
    names = [p.name for p in outputs]
    if not (
        lone_pg_stats
        and o.column is not None
        and all(c in bare for c in _PG_STATS_IDENTITY)
        and len(names) == len(set(names))
    ):
        raise PrivacyRejected(
            f"privacy: {o.name!r} holds sampled values; it is shown only for entity keys, as a "
            "plain column of pg_stats selected with schemaname, tablename and attname"
        )
    schema, table, column = (bare[c] for c in _PG_STATS_IDENTITY)
    return _Gated(o.name, schema, table, column)


def find_personal_data(text: str) -> list[str]:
    """Kinds of personal data found in text bound for the LLM: the gateway's last-line check
    for row data that slipped past the filters above."""
    return [kind for kind, pattern in _PERSONAL_DATA if pattern.search(text)]


_PERSONAL_DATA = [
    ("email address", re.compile(r"[\w.+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    ("phone number", re.compile(r"(?<![\w+])\+\d[\d ()-]{7,17}\d(?!\d)")),
]


# --- Resolution -----------------------------------------------------------------------------

SourceKind = Literal["catalog", "relation", "derived", "function", "opaque"]


@dataclass
class _Source:
    """A FROM item. `name` qualifies its columns; derived `columns` are known by name, and
    `rest` covers columns a `*` brought in."""

    name: str | None
    kind: SourceKind
    schema: str | None = None
    relname: str | None = None
    columns: dict[str, Verdict] = field(default_factory=dict)
    rest: Verdict | None = None
    verdict: Verdict = UNSAFE  # function outputs


@dataclass
class _Scope:
    sources: list[_Source] = field(default_factory=list)
    ctes: dict[str, list[tuple[str, Verdict]] | None] = field(default_factory=dict)
    merged: set[str] = field(default_factory=set)  # JOIN ... USING columns
    natural: bool = False


@dataclass(frozen=True)
class _Output:
    name: str
    verdict: Verdict
    column: str | None = None  # set for a bare, unqualified-or-qualified column reference


Found = Literal["definite", "possible", "absent"]


class _Checker:
    def __init__(self, entity_keys: frozenset[EntityKey]):
        self._keys = entity_keys

    # Statements ----------------------------------------------------------------------------

    def select(
        self, stmt: ast.SelectStmt, outer: list[_Scope]
    ) -> tuple[_Scope | None, list[_Output]]:
        """The statement's own FROM scope (None for a set operation) and its outputs."""
        scope = _Scope()
        chain = [scope, *outer]
        if stmt.withClause is not None:
            self._ctes(stmt.withClause, scope, chain)
        if stmt.larg is not None and stmt.rarg is not None:
            _, left = self.select(stmt.larg, chain)
            _, right = self.select(stmt.rarg, chain)
            return None, [
                _Output(lo.name, max(lo.verdict, ro.verdict))
                for lo, ro in zip(left, right, strict=True)
            ]
        if stmt.valuesLists:
            rows = [[self.expr(e, chain) for e in row] for row in stmt.valuesLists]
            return scope, [
                _Output(f"column{i}", max(col))
                for i, col in enumerate(zip(*rows, strict=True), start=1)
            ]
        for item in stmt.fromClause or ():
            self._from(item, scope, chain)
        outputs: list[_Output] = []
        for target in stmt.targetList or ():
            outputs += self._target(target, scope, chain)
        return scope, outputs

    def _ctes(self, with_: ast.WithClause, scope: _Scope, chain: list[_Scope]) -> None:
        ctes = [c for c in with_.ctes or () if c.ctename]
        if with_.recursive:
            for cte in ctes:
                scope.ctes[cte.ctename] = None  # unknown until computed: every column unsafe
        for cte in ctes:
            assert isinstance(cte.ctequery, ast.SelectStmt)  # the guard refuses DML CTEs
            _, outputs = self.select(cte.ctequery, chain)
            names = _strings(cte.aliascolnames)
            scope.ctes[cte.ctename] = _rename(outputs, names)

    def _target(self, target: ast.ResTarget, scope: _Scope, chain: list[_Scope]) -> list[_Output]:
        node = target.val
        if isinstance(node, ast.ColumnRef) and isinstance(_last(node.fields), ast.A_Star):
            return [_Output("*", self._star(node, chain))]
        verdict = self.expr(node, chain)
        column = _last(node.fields).sval if isinstance(node, ast.ColumnRef) else None
        return [_Output(target.name or _name(node), verdict, column)]

    # FROM ----------------------------------------------------------------------------------

    def _from(self, item: Any, scope: _Scope, chain: list[_Scope]) -> None:
        if isinstance(item, ast.JoinExpr):
            self._from(item.larg, scope, chain)
            self._from(item.rarg, scope, chain)
            scope.natural |= bool(item.isNatural)
            scope.merged |= set(_strings(item.usingClause))
            if item.alias is not None or item.join_using_alias is not None:
                scope.sources.append(_Source(None, "opaque"))
            return
        if isinstance(item, ast.RangeTableSample):
            self._from(item.relation, scope, chain)
            return
        if isinstance(item, ast.RangeVar):
            scope.sources.append(self._range_var(item, chain))
            return
        if isinstance(item, ast.RangeSubselect):
            assert isinstance(item.subquery, ast.SelectStmt)
            _, outputs = self.select(item.subquery, chain)
            alias = item.alias
            names = _strings(alias.colnames) if alias else []
            source = _derived(alias.aliasname if alias else None, _rename(outputs, names))
            scope.sources.append(source)
            return
        if isinstance(item, ast.RangeFunction):
            scope.sources.append(self._function(item, chain))
            return
        scope.sources.append(_Source(None, "opaque"))  # XMLTABLE, JSON_TABLE, ...

    def _range_var(self, rv: ast.RangeVar, chain: list[_Scope]) -> _Source:
        alias = rv.alias.aliasname if rv.alias else rv.relname
        renamed = rv.alias is not None and bool(rv.alias.colnames)
        if rv.schemaname is None:
            for scope in chain:
                if rv.relname in scope.ctes:
                    outputs = scope.ctes[rv.relname]
                    names = _strings(rv.alias.colnames) if rv.alias else []
                    if outputs is None:
                        return _Source(alias, "derived", rest=UNSAFE)
                    return _derived(alias, _rename(outputs, names))
        if renamed:
            return _Source(alias, "opaque")  # renamed columns hide which catalog column is which
        kind: SourceKind = "catalog" if is_catalog_relation(rv) else "relation"
        return _Source(alias, kind, schema=rv.schemaname, relname=rv.relname)

    def _function(self, rf: ast.RangeFunction, chain: list[_Scope]) -> _Source:
        verdict = SAFE
        for call, _coldefs in rf.functions or ():
            if isinstance(call, ast.FuncCall) and _funcname(call) in VALUE_BEARING_FUNCTIONS:
                verdict = UNSAFE
            else:
                verdict = max(verdict, self.expr(call, chain))
        return _Source(
            rf.alias.aliasname if rf.alias else None,
            "function",
            verdict=UNSAFE if verdict == GATED else verdict,
        )

    # Expressions ---------------------------------------------------------------------------

    def expr(self, node: Any, chain: list[_Scope]) -> Verdict:
        if isinstance(node, ast.ColumnRef):
            return self._column(node, chain)
        if isinstance(node, ast.FuncCall):
            return self._call(node, chain)
        if isinstance(node, ast.SubLink):
            return self._sublink(node, chain)
        if isinstance(node, ast.Node):
            return max((self.expr(c, chain) for c in children(node)), default=SAFE)
        return SAFE

    def _call(self, call: ast.FuncCall, chain: list[_Scope]) -> Verdict:
        name = _funcname(call)
        if call.over is None and name in COUNTING:
            return SAFE
        if name in VALUE_BEARING_FUNCTIONS:
            return UNSAFE
        args = list(call.args or ())
        if call.over is None and name in _AGGREGATES:
            ordered = [s.node for s in call.agg_order or ()] if call.agg_within_group else []
            return max((self._measure(a, chain) for a in [*args, *ordered]), default=SAFE)
        # Window definitions and FILTER clauses shape the result but are not output.
        return max((self.expr(a, chain) for a in args), default=SAFE)

    def _measure(self, node: Any, chain: list[_Scope]) -> Verdict:
        """A size or length, arithmetic on them, or a CASE choosing between them: what an
        aggregate other than a count may range over. CASE conditions are a per-row filter,
        like WHERE, and are not output."""
        if isinstance(node, ast.FuncCall) and _funcname(node) in MEASURES:
            return SAFE
        if isinstance(node, ast.TypeCast):
            return self._measure(node.arg, chain)
        if isinstance(node, ast.A_Expr):
            return max(self._measure(node.lexpr, chain), self._measure(node.rexpr, chain))
        if isinstance(node, ast.CaseExpr):
            results = [w.result for w in node.args or ()] + [node.defresult]
            return max(self._measure(r, chain) for r in results)
        return self.expr(node, chain)

    def _sublink(self, link: ast.SubLink, chain: list[_Scope]) -> Verdict:
        kind = link.subLinkType
        if kind == SubLinkType.EXISTS_SUBLINK:
            return SAFE
        if kind in (SubLinkType.ANY_SUBLINK, SubLinkType.ALL_SUBLINK):
            return self.expr(link.testexpr, chain)  # one boolean about the tested value
        assert isinstance(link.subselect, ast.SelectStmt)
        _, outputs = self.select(link.subselect, chain)
        verdict = max((o.verdict for o in outputs), default=SAFE)
        return UNSAFE if verdict == GATED else max(verdict, self.expr(link.testexpr, chain))

    # Columns -------------------------------------------------------------------------------

    def _column(self, ref: ast.ColumnRef, chain: list[_Scope]) -> Verdict:
        fields = _strings(ref.fields)
        if isinstance(_last(ref.fields), ast.A_Star):
            return self._star(ref, chain)
        *qualifier, column = fields
        if qualifier:
            source = self._qualified(qualifier, chain)
            if source is None:
                return UNSAFE
            found, verdict = self._lookup(source, column)
            return UNSAFE if found == "absent" else verdict
        verdict = self._unqualified(column, chain)
        # A lone name that matches a FROM item may be that item's whole row.
        whole_rows = [s for scope in chain for s in scope.sources if s.name == column]
        return max([verdict, *(self._whole_row(s) for s in whole_rows)])

    def _qualified(self, qualifier: list[str], chain: list[_Scope]) -> _Source | None:
        for scope in chain:
            for s in scope.sources:
                if qualifier == [s.name] or (
                    len(qualifier) == 2 and qualifier == [s.schema, s.relname]
                ):
                    return s
        return None

    def _unqualified(self, column: str, chain: list[_Scope]) -> Verdict:
        """Postgres takes the innermost scope that has the column. A scope that only might
        have it (a user table, a catalog relation) may also be passed over, so outer scopes
        count too; a scope known to have it ends the search."""
        maybe: list[Verdict] = []
        for scope in chain:
            if scope.sources and (scope.natural or column in scope.merged):
                return UNSAFE  # a merged join column may come from either side
            found = [self._lookup(s, column) for s in scope.sources]
            if definite := [v for f, v in found if f == "definite"]:
                return max(definite + maybe)
            maybe += [v for f, v in found if f == "possible"]
        return max(maybe, default=UNSAFE)

    def _lookup(self, source: _Source, column: str) -> tuple[Found, Verdict]:
        match source.kind:
            case "catalog":
                return "possible", VALUE_BEARING.get((source.relname or "", column), SAFE)
            case "relation":
                if self._is_entity_key(source, column):
                    return "definite", SAFE
                return "possible", UNSAFE
            case "derived":
                if column in source.columns:
                    return "definite", source.columns[column]
                return ("possible", source.rest) if source.rest is not None else ("absent", SAFE)
            case "function":
                return "possible", source.verdict
            case _:
                return "possible", UNSAFE

    def _is_entity_key(self, source: _Source, column: str) -> bool:
        key = EntityKey(source.schema or "public", source.relname or "", column)
        return key in self._keys

    def _star(self, ref: ast.ColumnRef, chain: list[_Scope]) -> Verdict:
        qualifier = _strings(ref.fields)
        if qualifier:
            source = self._qualified(qualifier, chain)
            return UNSAFE if source is None else self._whole_row(source)
        sources = chain[0].sources if chain else []
        return max((self._whole_row(s) for s in sources), default=SAFE)

    def _whole_row(self, source: _Source) -> Verdict:
        match source.kind:
            case "catalog":
                holds_values = any(rel == source.relname for rel, _ in VALUE_BEARING)
                return UNSAFE if holds_values else SAFE
            case "derived":
                return max([*source.columns.values(), source.rest or SAFE])
            case "function":
                return source.verdict
            case _:
                return UNSAFE


# Every aggregate the guard allows by name, from the PG 15-18 catalog snapshot, would be the
# precise set; these are the ones agents write. Others (array_agg, string_agg, json_agg, ...)
# return their inputs and are checked as plain functions.
_AGGREGATES = frozenset(
    {
        "sum",
        "avg",
        "min",
        "max",
        "mode",
        "percentile_cont",
        "percentile_disc",
        "stddev",
        "stddev_pop",
        "stddev_samp",
        "variance",
        "var_pop",
        "var_samp",
        "bool_and",
        "bool_or",
        "every",
        "bit_and",
        "bit_or",
        "bit_xor",
        "corr",
        "covar_pop",
        "covar_samp",
        *(f"regr_{s}" for s in ("avgx", "avgy", "intercept", "r2", "slope", "sxx", "sxy", "syy")),
    }
)


def _derived(name: str | None, columns: Sequence[tuple[str, Verdict]]) -> _Source:
    """A subquery or CTE as a FROM item. Sampled values never pass through one."""
    known: dict[str, Verdict] = {}
    rest: Verdict | None = None
    for col, verdict in columns:
        v = UNSAFE if verdict == GATED else verdict
        if col == "*":
            rest = v
        else:
            known[col] = max(v, known.get(col, SAFE))
    return _Source(name, "derived", columns=known, rest=rest)


def _rename(
    columns: Iterable[_Output | tuple[str, Verdict]], names: list[str]
) -> list[tuple[str, Verdict]]:
    """Columns as (name, verdict), renamed positionally by a column alias list."""
    pairs = [(c.name, c.verdict) if isinstance(c, _Output) else c for c in columns]
    return [(names[i] if i < len(names) else n, v) for i, (n, v) in enumerate(pairs)]


def _name(node: Any) -> str:
    """The output column name Postgres gives an unaliased expression."""
    if isinstance(node, ast.ColumnRef) and isinstance(_last(node.fields), ast.String):
        return str(_last(node.fields).sval)
    if isinstance(node, ast.FuncCall):
        return str(_funcname(node))
    if isinstance(node, ast.TypeCast):
        return _name(node.arg)
    return "?column?"


def _last(nodes: Sequence[Any] | None) -> Any:
    return nodes[-1] if nodes else None


def _funcname(call: ast.FuncCall) -> str:
    return str(_last(call.funcname).sval)


def _strings(nodes: Sequence[Any] | None) -> list[str]:
    return [str(n.sval) for n in nodes or () if isinstance(n, ast.String)]
