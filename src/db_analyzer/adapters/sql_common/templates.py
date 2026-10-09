"""Versioned catalog-query templates: reviewable `.sql` files with a declared contract.

Each file starts with a header:

    -- name: storage_stats
    -- columns: schema, name, total_bytes
    -- min_version: 15
    -- privilege: none (catalog only)

Several files may share a name; the newest variant the server supports is used. Callers pass
only declared columns on (proposal §3.5), so the header is also the template's output contract.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from db_analyzer.safety.executor import Row, SafeExecutor

_HEADER = re.compile(r"^--\s*(\w+):\s*(.*?)\s*$")
_REQUIRED = ("name", "columns", "min_version", "privilege")


@dataclass(frozen=True)
class Template:
    name: str
    columns: tuple[str, ...]
    min_version: int  # major version, e.g. 15
    privilege: str
    sql: str
    path: Path


class TemplateLibrary:
    def __init__(self, directory: Path):
        self._templates: dict[str, list[Template]] = {}
        for path in sorted(directory.glob("*.sql")):
            t = _parse(path)
            self._templates.setdefault(t.name, []).append(t)

    def get(self, name: str, server_version_num: int) -> Template:
        major = server_version_num // 10000
        variants = self._templates[name]
        usable = [t for t in variants if t.min_version <= major]
        if not usable:
            oldest = min(t.min_version for t in variants)
            raise LookupError(f"template {name!r} needs PostgreSQL {oldest} or later")
        return max(usable, key=lambda t: t.min_version)

    def all(self) -> list[Template]:
        return [t for variants in self._templates.values() for t in variants]


def _parse(path: Path) -> Template:
    header: dict[str, str] = {}
    body: list[str] = []
    for line in path.read_text().splitlines():
        m = _HEADER.match(line)
        if m and not body and m.group(1) in _REQUIRED:
            header[m.group(1)] = m.group(2)
        else:
            body.append(line)
    for key in _REQUIRED:
        if not header.get(key):
            raise ValueError(f"{path.name}: header is missing '-- {key}:'")
    return Template(
        name=header["name"],
        columns=tuple(c.strip() for c in header["columns"].split(",")),
        min_version=int(header["min_version"]),
        privilege=header["privilege"],
        sql="\n".join(body).strip() + "\n",
        path=path,
    )


def run_template(executor: SafeExecutor, template: Template, purpose: str) -> list[Row]:
    """Run a template through SafeExecutor and pass on only its declared columns."""
    rows = executor.execute(template.sql, purpose=purpose)
    if rows and (missing := set(template.columns) - set(rows[0])):
        raise ValueError(f"template {template.name!r} did not return {sorted(missing)}")
    return [{c: r[c] for c in template.columns} for r in rows]
