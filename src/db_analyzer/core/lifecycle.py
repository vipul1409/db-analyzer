"""Finding lifecycle: how a finished Run moves the status of a Connection's Findings.

Plain rules, no I/O. A Finding observed again is current (an acknowledged one stays
acknowledged); one a covering Run did not observe asks "fixed?" and keeps its status; one whose
subject no longer exists is obsolete."""

from dataclasses import replace

from db_analyzer.core.model import Finding, FindingCategory, FindingStatus, Run

# Categories whose subject is one relation: a collection, or an index on one. Such a Finding is
# obsolete once the relation is gone. Others (queries, entities, proposed indexes) never are.
RELATION_CATEGORIES: frozenset[FindingCategory] = frozenset(
    {"size", "bloat", "stale_stats", "unused_index", "duplicate_index", "invalid_index"}
)


def covers(run: Run, f: Finding) -> bool:
    """Whether `run` measured the subject's collection with the analyzer that covers `f`, so not
    observing `f` means something. Skipped collections are outside the scope, so never
    covered."""
    return f.covered_by is not None and f.collection in run.in_scope(f.covered_by)


def after_run(
    run: Run, findings: list[Finding], observed: set[str], existing: set[str] | None
) -> list[Finding]:
    """The Findings whose status or "fixed?" prompt `run` changes, updated. `observed` holds the
    fingerprints the Run observed; `existing` the relations (collections and indexes, qualified)
    the Connection has now, or None when the Run did not list them."""
    changed = []
    for f in findings:
        updated = _after(run, f, f.fingerprint in observed, existing)
        if updated != f:
            changed.append(updated)
    return changed


def _after(run: Run, f: Finding, observed: bool, existing: set[str] | None) -> Finding:
    if observed:
        status: FindingStatus = "acknowledged" if f.status == "acknowledged" else "open"
        return replace(f, status=status, unobserved_by=None)
    if existing is not None and f.category in RELATION_CATEGORIES and f.subject not in existing:
        return replace(f, status="obsolete", unobserved_by=None)
    if f.status in ("open", "acknowledged") and covers(run, f):
        return replace(f, unobserved_by=run.id)
    return f
