"""Identifier aliasing (proposal §3.5, `alias_identifiers`): for organisations that treat schema
names as sensitive, schema, table, index, constraint and column names reach the LLM only as
stable aliases such as `table_3`.

Replacement is by whole word, in any text: tool results (JSON is walked, so names behind escape
sequences are found too), the user's messages, and catalog values such as an index definition.
The LLM writes SQL and answers in aliases; `unalias` restores real names in its tool arguments
and in what the user is shown.

Known limits: only names that are plain identifiers ([A-Za-z_][A-Za-z0-9_$]*) are aliased; an
English word that is also a table or column name is aliased wherever it appears; restoring a
name that needs quoting inside SQL gives invalid SQL, which fails rather than leaks.
"""

import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def assign(existing: Mapping[str, str], found: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Aliases for names in `found` (kind, name) that have none yet. A name keeps the alias it
    was first given, whatever kind it is seen as later."""
    found = list(found)
    real = {name for _, name in found} | set(existing)
    taken = set(existing.values())
    numbers: dict[str, int] = {}
    new: dict[str, str] = {}
    for kind, name in found:
        if name in existing or name in new or not _WORD.fullmatch(name):
            continue
        n = numbers.get(kind, 0)
        while True:
            n += 1
            alias = f"{kind}_{n}"
            if alias not in taken and alias not in real:
                break
        numbers[kind] = n
        taken.add(alias)
        new[name] = alias
    return new


class Aliases:
    def __init__(self, names: Mapping[str, str]):
        self._to_alias = dict(names)
        self._to_name = {a: n for n, a in names.items()}

    def alias(self, text: str) -> str:
        return _WORD.sub(lambda m: self._to_alias.get(m[0], m[0]), text)

    def unalias(self, text: str) -> str:
        return _WORD.sub(lambda m: self._to_name.get(m[0], m[0]), text)

    def alias_content(self, content: str) -> str:
        """`alias` for a message: JSON keeps its structure, with keys and strings aliased."""
        try:
            data = json.loads(content)
        except ValueError:
            return self.alias(content)
        return json.dumps(_map_strings(data, self.alias))

    def unalias_args(self, args: dict[str, Any]) -> dict[str, Any]:
        return {k: _map_strings(v, self.unalias) for k, v in args.items()}


def _map_strings(data: Any, transform: Callable[[str], str]) -> Any:
    """`transform` applied to every string in JSON-like data, dictionary keys included."""
    if isinstance(data, str):
        return transform(data)
    if isinstance(data, list):
        return [_map_strings(v, transform) for v in data]
    if isinstance(data, dict):
        return {transform(k): _map_strings(v, transform) for k, v in data.items()}
    return data
