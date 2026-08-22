"""Every fact pass must be wired into the pipeline -- no pass added on the side.

The historical failure mode this guards: a new feature defines another
_populate_*/_resolve_*/... pass and splices a call somewhere by hand, or forgets to.
A pass function (prefix naming convention, first parameter `result`) defined in any of
the pass-hosting modules must be reachable from _populate_enhanced_scope_facts through
calls inside scope_facts.py. Cross-module passes count: the pipeline imports them and
calls them by name.
"""

from __future__ import annotations

import ast
from pathlib import Path

SCOPE_DIR = Path(__file__).resolve().parents[2] / "scope_lineage" / "scope"

PASS_MODULES = (
    "scope_facts.py",
    "passthrough_resolution.py",
    "expression_expansion.py",
    "star_passthrough.py",
    "logic_block.py",
    "lineage_fact_gaps.py",
)

PASS_PREFIXES = ("_populate_", "_resolve_", "_refresh_", "_finish_", "_restore_", "_prune_", "_propagate_")

PIPELINE_ROOT = "_populate_enhanced_scope_facts"


def _pass_candidates() -> set[str]:
    names: set[str] = set()
    for module in PASS_MODULES:
        tree = ast.parse((SCOPE_DIR / module).read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if not node.name.startswith(PASS_PREFIXES):
                continue
            args = node.args.args
            if args and args[0].arg == "result":
                names.add(node.name)
    return names


def _reachable_calls() -> set[str]:
    """Names called from the pipeline root, transitively through scope_facts functions."""
    tree = ast.parse((SCOPE_DIR / "scope_facts.py").read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}

    def direct_calls(fn: ast.FunctionDef) -> set[str]:
        calls: set[str] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.add(node.func.id)
        return calls

    reachable: set[str] = set()
    frontier = [PIPELINE_ROOT]
    while frontier:
        name = frontier.pop()
        fn = functions.get(name)
        if fn is None:
            continue
        for called in direct_calls(fn):
            if called not in reachable:
                reachable.add(called)
                frontier.append(called)
    return reachable


def test_every_fact_pass_is_reachable_from_the_pipeline() -> None:
    orphans = _pass_candidates() - _reachable_calls() - {PIPELINE_ROOT}
    assert not orphans, (
        "fact passes defined but not wired into _populate_enhanced_scope_facts: "
        + ", ".join(sorted(orphans))
    )
