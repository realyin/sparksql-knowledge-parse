"""The internal-resolution pass must be idempotent on a settled result.

Root cause of the pipeline tail's load-bearing truncation (WI-09 step 2):
_should_rebuild_internal_expansion_from_expression treated "the expanded expression no
longer contains the original internal refs" as damage requiring a rebuild. On a settled
output that is exactly what success looks like -- the internal refs were replaced by
physical fields -- so one extra run of the pass clobbered source_scope_id and the refined
resolution, and the provenance trace built later collapsed to a single coarse record.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from scope_lineage.scope.scope_builder import parse_scope_lineage
from scope_lineage.scope.scope_facts import _resolve_internal_scope_expression_resolution

MERGE_CASE = Path(__file__).parent / "fixtures" / "lineage_contract" / "merge" / "case.json"


def test_internal_resolution_pass_is_idempotent_on_a_settled_result() -> None:
    case = json.loads(MERGE_CASE.read_text(encoding="utf-8"))
    result = parse_scope_lineage(case["sql"], case["task_id"], schema=case.get("schema"))

    before = {
        (scope_id, output.name): copy.deepcopy(output.expression_resolution)
        for scope_id, scope_data in result.scopes.items()
        for output in scope_data.outputs
    }

    _resolve_internal_scope_expression_resolution(result)

    after = {
        (scope_id, output.name): output.expression_resolution
        for scope_id, scope_data in result.scopes.items()
        for output in scope_data.outputs
    }
    changed = {key for key in before if before[key] != after.get(key)}
    assert not changed, (
        "re-running the internal-resolution pass rewrote settled resolutions: "
        + ", ".join(f"{scope}:{name}" for scope, name in sorted(changed))
    )
