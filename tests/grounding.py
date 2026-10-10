"""Eval check: every number in an answer appears in a tool result of the same Turn."""

import re
from decimal import Decimal, InvalidOperation

# A number not glued to an identifier or placeholder (table_3, $1), with optional thousands
# separators and decimals.
_NUMBER = re.compile(
    r"(?<![\w$.])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w])|(?<![\w$.,])\d+(?:\.\d+)?(?![\w])"
)
# Ranks, list numbering and "top 10" are counted, not measured.
SMALL = 25


def numbers(text: str) -> set[Decimal]:
    found = set()
    for match in _NUMBER.findall(text):
        try:
            found.add(Decimal(match.replace(",", "")).normalize())
        except InvalidOperation:
            continue
    return found


def ungrounded(answer: str, tool_results: list[str]) -> set[Decimal]:
    """Numbers in `answer` that no tool result holds, ignoring small integers."""
    known = set().union(*(numbers(r) for r in tool_results)) if tool_results else set()
    return {n for n in numbers(answer) - known if not (n == n.to_integral_value() and n <= SMALL)}
