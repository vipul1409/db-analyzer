"""Finding lifecycle: how a finished Run moves the status of a Connection's Findings.

Plain rules, no I/O. A Finding observed again is current (an acknowledged one stays
acknowledged); one a covering Run did not observe asks "fixed?" and keeps its status; one whose
subject no longer exists is obsolete."""

from dataclasses import replace

from db_analyzer.core.model import AnalyzerName, Finding, FindingCategory, FindingStatus, Run

# Which analyzer evaluates each category, for categories whose subject is one collection.
# Categories not listed (indexes, queries, entities) are never treated as covered yet: no
# "fixed?" prompt and no obsolete check until their analyzer says what its scope covers.
COLLECTION_CATEGORIES: dict[FindingCategory, AnalyzerName] = {
    "size": "inventory",
    "bloat": "inventory",
    "stale_stats": "inventory",
}


def is_ranking_fact(f: Finding) -> bool:
    """A `size` Finding without a rule ranks a collection among the largest. It is a fact about
    the Run, not a problem: dropping out of the top N is not a fix, so it never asks "fixed?"."""
    return f.category == "size" and f.fingerprint == f"size:{f.subject}"


def evaluated_by(f: Finding) -> AnalyzerName | None:
    """The analyzer whose scope decides whether a Run's silence about `f` means something, or
    None when it never does (ranking facts, categories not mapped yet)."""
    return None if is_ranking_fact(f) else COLLECTION_CATEGORIES.get(f.category)


def covers(run: Run, f: Finding) -> bool:
    """Whether `run` measured the subject with the analyzer that evaluates `f`, so not observing
    `f` means something. Skipped collections are outside the scope, so never covered."""
    analyzer = evaluated_by(f)
    return analyzer is not None and f.subject in run.in_scope(analyzer)


def after_run(
    run: Run, findings: list[Finding], observed: set[str], existing: set[str] | None
) -> list[Finding]:
    """The Findings whose status or "fixed?" prompt `run` changes, updated. `observed` holds the
    fingerprints the Run observed; `existing` the collections the Connection has now, or None
    when the Run did not list them."""
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
    if existing is not None and f.category in COLLECTION_CATEGORIES and f.subject not in existing:
        return replace(f, status="obsolete", unobserved_by=None)
    if f.status in ("open", "acknowledged") and covers(run, f):
        return replace(f, unobserved_by=run.id)
    return f
