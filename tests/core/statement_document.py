"""Test-side assembly of the per-statement document pair.

The standalone contract-1.0 artifact writer (`write_lineage`) is gone, but the statement
document SHAPE it wrote is not retired: it is exactly what a task document embeds per
entry in `statement_lineage`, and `lineage.schema.json` / `diagnostics.schema.json`
remain its schemas. Statement-level tests keep exercising that shape through this
helper, which replicates the retired writer's assembly, validation, and file format
byte-for-byte so the golden fixtures stay meaningful.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from scope_lineage.contract import to_lineage_dict, validate_cross_references
from scope_lineage.contract.lineage import _diagnostics_summary
from scope_lineage.contract.validation import (
    validate_diagnostics_document,
    validate_lineage_document,
)


def build_statement_documents(result) -> tuple[dict, dict]:
    """Return the validated (lineage_data, diagnostics_data) pair for one statement."""
    data = to_lineage_dict(result)
    xref_errors = validate_cross_references(data)
    if xref_errors:
        raise ValueError(
            f"Cross-reference validation failed ({len(xref_errors)} errors):\n"
            + "\n".join(xref_errors[:5])
        )
    diagnostics_full = data.get("diagnostics", {})
    lineage_data = copy.deepcopy(data)
    lineage_data["diagnostics"] = _diagnostics_summary(diagnostics_full)
    diagnostics_data = {"schema_version": "1.0", **diagnostics_full}
    validate_lineage_document(lineage_data)
    validate_diagnostics_document(diagnostics_data)
    return lineage_data, diagnostics_data


def write_statement_documents(
    result,
    output_dir: str | Path,
    *,
    compact: bool = False,
) -> Path:
    """Write lineage.json + diagnostics.json exactly as the retired v1 writer did."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    lineage_data, diagnostics_data = build_statement_documents(result)
    for name, document in (("lineage.json", lineage_data), ("diagnostics.json", diagnostics_data)):
        with open(output_dir / name, "w", encoding="utf-8") as stream:
            json.dump(
                document,
                stream,
                ensure_ascii=False,
                indent=None if compact else 2,
                separators=(",", ":") if compact else None,
                default=str,
            )
    return output_dir
