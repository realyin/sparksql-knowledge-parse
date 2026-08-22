"""Leaf constants shared across the scope domain.

Zero package-internal imports, so any scope module (and the modules being
extracted from _shared.py) can depend on these without creating a cycle.
"""
from __future__ import annotations

from sqlglot import ErrorLevel

DIALECT = "spark"

PARSE_OPTS = {"error_level": ErrorLevel.IGNORE}

_SCOPE_ID_ATTR = "_lineage_scope_id"
_ORIGINALLY_UNQUALIFIED_META = "lineage_originally_unqualified"
