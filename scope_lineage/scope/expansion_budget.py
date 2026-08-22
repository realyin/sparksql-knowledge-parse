"""Budgeted expression expansion (PERF-001): bounded, composable, never damaged."""
from __future__ import annotations

# Expansion budget for `expanded_expression`. Inlining an upstream field's expanded text copies
# it once per reference, and each additional scope layer multiplies again; a moderately sized
# statement expanded to a string and a lineage.json three orders of magnitude larger (PERF-001).
#
# The budget bounds the copy by DECLINING a substitution, not by cutting text. What is left
# behind is the original `a.field` reference — still valid SQL, and itself the pointer to the
# upstream output that holds the rest of the logic. Because upstream expressions are bounded
# first, downstream ones inline small text, so the multiplication collapses at every layer
# instead of only at the top.
EXPANSION_MAX_CHARS = 262_144       # 256 KiB per materialized expression
EXPANSION_MAX_SUBSTITUTIONS = 2_000  # guards reference count, which chars alone does not

class ExpansionBudget:
    """One expression's expansion allowance, and the record of what it had to decline.

    Shared by every inlining site so a single expression cannot exceed the limit by being
    grown from several places, and so the reason is reported the same way everywhere.
    """

    __slots__ = ("max_chars", "max_substitutions", "substitutions", "stop_reason", "skipped_refs")

    def __init__(self, max_chars: int | None = None,
                 max_substitutions: int | None = None) -> None:
        # Read at construction, not as a default argument: the limits are module-level policy
        # and tests raise them to prove the case under test actually blows up without them.
        self.max_chars = EXPANSION_MAX_CHARS if max_chars is None else max_chars
        self.max_substitutions = (
            EXPANSION_MAX_SUBSTITUTIONS if max_substitutions is None else max_substitutions
        )
        self.substitutions = 0
        self.stop_reason: str | None = None
        self.skipped_refs: list[dict] = []

    @property
    def status(self) -> str:
        return "bounded" if self.stop_reason else "full"

    def _decline(self, reason: str, ref: str, scope_id: str | None, field: str | None) -> None:
        self.stop_reason = self.stop_reason or reason
        entry = {"ref": ref, "reason": reason}
        if scope_id:
            entry["scope_id"] = scope_id
        if field:
            entry["field"] = field
        if entry not in self.skipped_refs:
            self.skipped_refs.append(entry)

    def substitute(self, expression: str, replacement: str, apply, *,
                   ref: str, scope_id: str | None = None, field: str | None = None) -> str:
        """Apply `apply(expression, replacement)` unless it would break the budget.

        Declining is checked twice: once cheaply on the replacement itself (a single upstream
        expression already at the limit can never be inlined), and once on the actual result,
        because one reference can occur many times.
        """
        if not replacement:
            return expression
        if self.substitutions >= self.max_substitutions:
            self._decline("max_substitutions", ref, scope_id, field)
            return expression
        if len(replacement) > self.max_chars:
            self._decline("max_chars", ref, scope_id, field)
            return expression
        expanded = apply(expression, replacement)
        if len(expanded) > self.max_chars:
            self._decline("max_chars", ref, scope_id, field)
            return expression
        if expanded != expression:
            self.substitutions += 1
        return expanded
