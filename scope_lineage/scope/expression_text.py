"""Textual SQL-expression operations: reference rewriting and qualifier probes."""
from __future__ import annotations

import re

from .expression_refs import _cached_pattern, _strip_sql_comments, _strip_sql_string_literals
from .scope_types import ScopeData
from .sequences import _unique_ordered

def _unexpanded_bound_aliases_in_expression(scope_data: ScopeData, expression: str | None) -> list[str]:
    expression = _strip_sql_comments(_strip_sql_string_literals(str(expression or "")))
    if not expression:
        return []
    unresolved_aliases: list[str] = []
    for binding in scope_data.alias_source_bindings or []:
        alias = str(binding.get("alias") or "")
        if not alias or not _qualifier_present(expression, alias):
            continue
        physical_ids = {str(item) for item in binding.get("physical_source_ids") or [] if item}
        physical_id = str(binding.get("physical_source_id") or "")
        if physical_id:
            physical_ids.add(physical_id)
        # "physical_table" is the value the binding builder emits; comparing against
        # "physical" here meant this exemption never fired, so an expression that kept a
        # physical table's own name — already a fully resolved reference — was reported as
        # an unexpanded alias. The `alias in physical_ids` half still holds the line: a
        # local alias such as `FROM ods.source s` that survived expansion is not exempt,
        # because that one really did fail to rewrite.
        # `qualify` names an unaliased table after itself, so `FROM ods.pay` yields
        # references written `pay.uid` while the physical id stays `ods.pay`. Comparing the
        # two directly never matched, and a fully resolved direct physical source was reported
        # as an unexpanded alias (BARE-ALIAS-001). Matching the id's table segment as well
        # still refuses a genuine local alias: `FROM ods.source s` leaves `s`, which is
        # neither the id nor its table name, and an `s.` that survived expansion really did
        # fail to rewrite.
        if binding.get("source_type") == "physical_table" and (
            alias in physical_ids
            or alias in {identifier.rsplit(".", 1)[-1] for identifier in physical_ids}
        ):
            continue
        unresolved_aliases.append(alias)
    return _unique_ordered(unresolved_aliases)


def _qualifier_present(expression: str, qualifier: str) -> bool:
    if f"`{qualifier}`." in expression:
        return True
    return bool(_cached_pattern(rf"(?<![.`\w]){re.escape(qualifier)}\.").search(expression))


def _replace_qualified_ref_with_expression(expression: str, qualifier: str, field: str, replacement: str) -> str:
    replacement_sql = _parenthesize_replacement_expression(replacement)
    expression = expression.replace(f"`{qualifier}`.`{field}`", replacement_sql)
    expression = expression.replace(f"`{qualifier}`.{replacement_sql}", replacement_sql)
    if f"{qualifier}.{field}" in expression:
        expression = _cached_pattern(
            rf"(?<![.`\w]){re.escape(qualifier)}\.{re.escape(field)}(?![`.\w])"
        ).sub(lambda _match: replacement_sql, expression)
    expression = expression.replace(f"{qualifier}.{replacement_sql}", replacement_sql)
    return expression


def _replace_unqualified_ref_with_expression(expression: str, field: str, replacement: str) -> str:
    replacement_sql = _parenthesize_replacement_expression(replacement)
    expression = _cached_pattern(
        rf"(?<![.`\w])`{re.escape(field)}`(?![`.\w])"
    ).sub(lambda _match: replacement_sql, expression)
    return _cached_pattern(
        rf"(?<![.`'\"\w]){re.escape(field)}(?![`.'\"\w])"
    ).sub(lambda _match: replacement_sql, expression)


def _parenthesize_replacement_expression(expression: str) -> str:
    stripped = expression.strip()
    if re.fullmatch(r"`[^`]+`\.`[^`]+`", stripped):
        return stripped
    if stripped.startswith("(") and stripped.endswith(")"):
        return stripped
    return f"({stripped})"


def _function_names(expression: str) -> list[str]:
    names = []
    for match in re.finditer(r"\b([a-z_][a-z0-9_]*)\s*\(", expression):
        name = match.group(1)
        if name in {"cast", "case", "if", "over"}:
            continue
        if name not in names:
            names.append(name)
    return names
