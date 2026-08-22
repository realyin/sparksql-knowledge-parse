"""The typed protocol must cover what the pipeline actually emits.

ExpressionResolution is the contract the passes trade through ScopeOutputField.
If a pass starts emitting a key the TypedDict does not declare, static checking is
blind to it -- this test makes that drift loud by diffing the protocol against every
resolution key present in the golden corpus.
"""

from __future__ import annotations

import json
from pathlib import Path

from scope_lineage.scope.fact_protocols import ExpressionResolution

FIXTURES = Path(__file__).parent / "fixtures" / "lineage_contract"


def _resolution_keys_in_corpus() -> set[str]:
    keys: set[str] = set()
    for lineage_path in FIXTURES.glob("*/lineage.json"):
        doc = json.loads(lineage_path.read_text(encoding="utf-8"))
        for scope in (doc.get("scopes") or {}).values():
            for output in scope.get("outputs") or []:
                resolution = output.get("expression_resolution") or {}
                keys.update(resolution.keys())
    return keys


def test_expression_resolution_protocol_covers_the_corpus() -> None:
    corpus_keys = _resolution_keys_in_corpus()
    assert corpus_keys, "no resolutions found in the golden corpus -- fixture layout changed?"
    declared = set(ExpressionResolution.__annotations__)
    undeclared = corpus_keys - declared
    assert not undeclared, (
        "the pipeline emits resolution keys the protocol does not declare: "
        + ", ".join(sorted(undeclared))
    )
