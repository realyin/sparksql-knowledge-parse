"""Session settings a script sets for itself, folded in statement order.

Only `spark.sql.parser.quotedRegexColumnNames` is folded here. It is not a preference:
under Spark's own default (`false`) a backtick-quoted regex projection is an ordinary
column name, the analysis fails, and the statement never runs -- so expanding it anyway
invents lineage for SQL that cannot execute.

The task-level path already folds `spark.sql.sources.partitionOverwriteMode` its own way
(`task_lineage.py`). Carrying that one here too, with nothing reading it, would put two
answers to one question in the tree, so it stays where it is until something needs it in
both places.
"""
from __future__ import annotations

from sqlglot import exp

from ._constants import DIALECT

QUOTED_REGEX_COLUMN_NAMES = "quotedregexcolumnnames"

# Spark's own default. A script that never enables the feature never had it.
DEFAULT_QUOTED_REGEX_COLUMN_NAMES = False


def quoted_regex_column_names_setting(tree: exp.Expression) -> bool | None:
    """True/False when this statement sets the flag, None when it does not.

    Renders each assignment with `comments=False`. A trailing comment attaches to the
    *value* node when the value is a boolean literal, so it survives per-item rendering
    and would otherwise be compared as part of the value -- `TRUE /* note */` is not
    `true`. The sibling helper for partitionOverwriteMode escapes this only because
    `dynamic`/`static` parse as identifiers rather than literals.
    """
    if not isinstance(tree, exp.Set):
        return None
    for item in tree.args.get("expressions") or []:
        text = item.sql(dialect=DIALECT, comments=False).replace("`", "").replace('"', "")
        key, separator, value = text.partition("=")
        if not separator:
            # `SET key` reads the setting; it does not assign one. It parses as a
            # Command rather than a Set in practice, but never treat it as "false".
            continue
        if key.strip().lower().endswith(QUOTED_REGEX_COLUMN_NAMES):
            cleaned = value.strip().strip("'").lower()
            # Spark's SQLConf accepts only true/false and throws otherwise; anything
            # else is not a value we can act on, so leave the setting as it was.
            if cleaned in {"true", "false"}:
                return cleaned == "true"
    return None
