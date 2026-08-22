"""Qualified field-reference extraction from SQL expression text.

Extracted from _shared.py (WI-02): this is the home of the public
``extract_qualified_field_refs`` and its private helpers/caches.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from ._constants import DIALECT, PARSE_OPTS

# Patterns here are built from identifier names, so their number grows with the statement's
# column count rather than with the code. Python's own re cache holds 512 entries, and a
# wide feature table blows straight through it: profiling a large statement showed millions of
# lookups, a sixth of them recompiling — about half the run spent in re's parser
# alone. Caching the compiled objects here removes that without changing a single match
# (PERF-002).
_COMPILED_PATTERNS: dict[str, "re.Pattern[str]"] = {}


def _cached_pattern(pattern: str) -> "re.Pattern[str]":
    compiled = _COMPILED_PATTERNS.get(pattern)
    if compiled is None:
        compiled = re.compile(pattern)
        _COMPILED_PATTERNS[pattern] = compiled
    return compiled


# Field extraction re-parses the expression into an AST every time it is asked, and the
# resolver asks about the same expressions repeatedly across its passes: the same large
# statement made far more sqlglot.parse_one calls than it had distinct questions, and the
# difference was pure repetition.
# The answer depends only on the expression text, so it is remembered (PERF-002).
_FIELD_REFS_CACHE: dict[str, tuple[tuple[str, str], ...]] = {}


def _qualified_field_refs(expression_sql: str) -> list[tuple[str, str]]:
    key = expression_sql or ""
    cached = _FIELD_REFS_CACHE.get(key)
    if cached is None:
        cached = tuple(_qualified_field_refs_uncached(key))
        _FIELD_REFS_CACHE[key] = cached
    # A fresh list each time: callers treat the result as their own to filter and extend.
    return list(cached)


def _qualified_field_refs_uncached(expression_sql: str) -> list[tuple[str, str]]:
    expression_sql = _strip_sql_comments(expression_sql or "")
    scan_sql = _strip_sql_string_literals(expression_sql)
    lambda_qualifiers = _lambda_qualifiers(scan_sql)
    # Keep string literals for the AST parse. Replacing them with blanks can make otherwise
    # valid calls (for example named_struct('x', c.value)) malformed, and newer sqlglot
    # versions may then omit legitimate column references during error recovery.
    ast_ref_keys = _qualified_field_ref_keys_from_ast(expression_sql, lambda_qualifiers)
    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in re.finditer(r"`([^`]+)`\.`([^`]+)`", scan_sql):
        qualifier, field = match.group(1), match.group(2)
        if qualifier in lambda_qualifiers:
            continue
        if _qualified_pair_is_catalog_function_prefix(scan_sql, match.end()):
            continue
        key = (qualifier, field)
        if ast_ref_keys is not None and key not in ast_ref_keys:
            continue
        if key not in seen:
            seen.add(key)
            refs.append(key)
    for match in re.finditer(
        r"(?<!\.)\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b(?!\.)",
        scan_sql,
    ):
        qualifier, field = match.group(1), match.group(2)
        if qualifier in lambda_qualifiers:
            continue
        if _qualified_pair_is_catalog_function_prefix(scan_sql, match.end()):
            continue
        key = (qualifier, field)
        if ast_ref_keys is not None and key not in ast_ref_keys:
            continue
        if key not in seen:
            seen.add(key)
            refs.append(key)
    return refs


def extract_qualified_field_refs(expression_sql: str) -> list[tuple[str, str]]:
    """Return stable ``(qualifier, field)`` references from a SQL expression."""
    return _qualified_field_refs(expression_sql)

def _qualified_field_ref_keys_from_ast(
    expression_sql: str,
    lambda_qualifiers: set[str],
) -> set[tuple[str, str]] | None:
    if not expression_sql:
        return set()
    try:
        parsed = sqlglot.parse_one(expression_sql, dialect=DIALECT, **PARSE_OPTS)
    except Exception:
        return None
    refs: set[tuple[str, str]] = set()
    for column in parsed.find_all(exp.Column):
        if _column_is_inside_nested_query(parsed, column):
            continue
        parts = [str(part.name or "") for part in column.parts if getattr(part, "name", None)]
        if len(parts) >= 3 and parts[0] and parts[1] and parts[0] not in lambda_qualifiers:
            refs.add((parts[0], parts[1]))
        qualifier = str(column.table or "")
        field = str(column.name or "")
        if not qualifier or not field or qualifier in lambda_qualifiers:
            continue
        refs.add((qualifier, field))
    return refs

def _column_is_inside_nested_query(root: exp.Expression, column: exp.Column) -> bool:
    parent = column.parent
    while parent is not None and parent is not root:
        if isinstance(parent, (exp.Select, exp.Subquery)):
            return True
        parent = parent.parent
    return False

def _qualified_pair_is_catalog_function_prefix(expression_sql: str, end_index: int) -> bool:
    suffix = expression_sql[end_index:]
    return bool(re.match(r"\s*\.\s*`?[A-Za-z_][A-Za-z0-9_]*`?\s*\(", suffix or ""))

def _lambda_qualifiers(expression_sql: str) -> set[str]:
    qualifiers: set[str] = set()
    for name in re.findall(r"`([^`]+)`\s*->", expression_sql or ""):
        if name:
            qualifiers.add(name)
    for name in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*->", expression_sql or ""):
        if name:
            qualifiers.add(name)
    for group in re.findall(r"\(([^()]+)\)\s*->", expression_sql or ""):
        for part in group.split(","):
            name = part.strip().strip("`")
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                qualifiers.add(name)
    return qualifiers

def _strip_sql_string_literals(expression_sql: str) -> str:
    return re.sub(r"'(?:''|\\'|[^'])*'", " ", expression_sql or "")

def _strip_sql_comments(expression_sql: str) -> str:
    without_block_comments = re.sub(r"/\*.*?\*/", " ", expression_sql or "", flags=re.DOTALL)
    return re.sub(r"--[^\n\r]*", " ", without_block_comments)
