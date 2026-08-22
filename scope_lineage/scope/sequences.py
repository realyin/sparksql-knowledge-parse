"""Order-preserving sequence utilities for the scope domain."""
from __future__ import annotations

def _unique_ordered(values: list[str]) -> list[str]:
    """Order-preserving dedupe that drops falsy entries.

    The single implementation (WI-03): the former `_unique_ordered__resolver`, which kept
    empty strings, differed only at call sites whose inputs are provably non-empty
    (or-fallback scope ids and qualified table names), so its behavior was unreachable.
    """
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _extend_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value and value not in target:
            target.append(value)
