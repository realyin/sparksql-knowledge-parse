"""Semantic sentinel: the fine-grained scope_output_trace must not collapse.

The fact pipeline's tail deliberately ends on the output-sources pass and does NOT run
_resolve_internal_scope_expression_resolution again: one extra run of that pass rewrites a
settled resolution and compresses the two-step provenance chain
(scope_projection -> physical_expression, with step numbers and physical_source_fields)
into a single coarse expanded_from_upstream_scope_expression record. The golden baselines
catch that byte-wise; this test states the invariant semantically so a re-recorded golden
cannot silently bless the degradation. (Governance plan WI-08/WI-09; verified to go red
under the extra-pass experiment that motivated it.)
"""

from __future__ import annotations

import json
from pathlib import Path

from scope_lineage.scope.scope_builder import parse_scope_lineage

MERGE_CASE = Path(__file__).parent / "fixtures" / "lineage_contract" / "merge" / "case.json"


def test_merge_root_outputs_keep_the_two_step_provenance_chain() -> None:
    case = json.loads(MERGE_CASE.read_text(encoding="utf-8"))
    result = parse_scope_lineage(case["sql"], case["task_id"], schema=case.get("schema"))

    root = result.scopes["ROOT"]
    two_step_traces = 0
    for output in root.outputs:
        trace = (output.expression_resolution or {}).get("scope_output_trace") or []
        if not trace:
            continue
        relations = [step.get("relation") for step in trace]
        if "physical_expression" in relations:
            two_step_traces += 1
            assert [step.get("step") for step in trace] == list(range(1, len(trace) + 1)), (
                f"{output.name}: trace steps lost their numbering: {trace}"
            )
            physical_steps = [s for s in trace if s.get("relation") == "physical_expression"]
            assert any(s.get("physical_source_fields") for s in physical_steps), (
                f"{output.name}: physical_expression step lost physical_source_fields"
            )
    assert two_step_traces >= 2, (
        "the merge case no longer produces fine-grained ROOT traces at all -- "
        "either the fixture changed or the pipeline collapsed them"
    )
