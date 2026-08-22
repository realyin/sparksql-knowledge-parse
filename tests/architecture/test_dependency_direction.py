"""Lock the package-level dependency direction: cli -> contract -> (serialize, scope) -> metadata.

Same spirit as verify_distribution.py: the boundary is a product decision, so a new
cross-package import edge must show up as a red test naming the exact import, not as
silent architecture drift.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "scope_lineage"
PACKAGE_NAME = "scope_lineage"

# First-level subpackages/modules whose edges are governed. Package-root leaf modules
# (sqlglot_config, and any future ones) are usable from anywhere and not listed.
GOVERNED = {"cli", "contract", "metadata", "render", "scope", "serialize"}

ALLOWED_EDGES: dict[str, set[str]] = {
    "cli": {"contract", "metadata", "scope"},
    "contract": {"scope", "serialize"},
    "serialize": {"scope"},
    "scope": {"metadata"},
    "metadata": set(),
    "render": set(),  # contract-derived: consumes the JSON documents only
}


def _package_of(module_path: Path) -> str:
    relative = module_path.relative_to(PACKAGE_ROOT)
    return relative.parts[0].removesuffix(".py")


def _imported_package(node: ast.ImportFrom, importer_pkg: str) -> str | None:
    """Return the first-level scope_lineage package a `from ... import` targets, if any."""
    if node.level == 0:
        if not node.module or not node.module.startswith(PACKAGE_NAME):
            return None
        parts = node.module.split(".")
        return parts[1] if len(parts) > 1 else None
    if node.level == 1:
        # Relative to the importer's own package: internal edge, not governed here --
        # except at the package root, where `from .x import` targets first-level x.
        if importer_pkg in GOVERNED:
            return importer_pkg
        return (node.module or "").split(".")[0] or None
    # level >= 2 from inside a subpackage reaches the package root: `from ..x import`.
    return (node.module or "").split(".")[0] or None


# One known inverted edge, kept deliberately: statement_lineage entries are contract dicts
# whose shape is public API, consumed inside scope/task_lineage and cli. Untangling it is
# scheduled with the v1 retirement's converter re-homing (governance plan WI-12, 0.3.0);
# remove this entry when that lands, or annotate the decision here if it is kept.
WHITELISTED_EDGES = {
    ("scope/task_lineage.py", "contract"),
}


def collect_violations() -> list[str]:
    violations: list[str] = []
    for module_path in sorted(PACKAGE_ROOT.rglob("*.py")):
        importer_pkg = _package_of(module_path)
        if importer_pkg not in GOVERNED:
            continue
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            target = _imported_package(node, importer_pkg)
            if target is None or target not in GOVERNED or target == importer_pkg:
                continue
            if target not in ALLOWED_EDGES[importer_pkg]:
                in_package = module_path.relative_to(PACKAGE_ROOT).as_posix()
                if (in_package, target) in WHITELISTED_EDGES:
                    continue
                relative = module_path.relative_to(PACKAGE_ROOT.parent)
                violations.append(
                    f"{relative}:{node.lineno} imports {PACKAGE_NAME}.{target} "
                    f"({importer_pkg} -> {target} is not an allowed edge)"
                )
    return violations


def test_package_dependency_direction_is_locked() -> None:
    violations = collect_violations()
    assert not violations, "forbidden cross-package imports:\n" + "\n".join(violations)
