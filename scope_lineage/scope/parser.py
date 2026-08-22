"""Column-level lineage parser (hybrid: sqlglot.lineage + post-processor).

v0.3 scope:
  - INSERT...SELECT and INSERT...WITH...SELECT (CTE) and INSERT...UNION ALL
  - MERGE INTO (UPDATE SET column lineage)
  - Strategy A (strict): never guess source table for unqualified columns;
    falls into AMBIGUOUS_COLUMN unresolved.
  - `*` / `a.*` expanded when schema is provided; otherwise STAR_UNRESOLVED.

We delegate column traversal (CTE / UNION / Subquery) to
``sqlglot.lineage.lineage`` and post-process its leaves into our flat edge
representation.
"""

import re
from typing import Any, Dict, List, Tuple

from sqlglot import ErrorLevel
from sqlglot import exp

from ..sqlglot_config import suppress_invalid_json_path_warnings
from ..metadata.schema_metadata import load_schema as load_schema, normalize_table_name as _schema_normalize_table_name, strip_catalog_prefix as _strip_catalog_prefix


DIALECT = "spark"
PARSE_OPTS = {"error_level": ErrorLevel.IGNORE}
suppress_invalid_json_path_warnings()

# sqlglot's qualifier wraps anonymous expressions in generated aliases such as
# ``_col_0``.  Mark the original expression before qualification so projection
# naming can distinguish that placeholder from an author-written alias with the
# same spelling.
_ORIGINALLY_ANONYMOUS_PROJECTION_META = "_lineage_originally_anonymous_projection"


# -- AST helpers ----------------------------------------------------------


def _unwrap_target(node):
    if isinstance(node, exp.Schema):
        return node.this
    return node


def _qualified_table(t: exp.Table) -> str:
    parts = []
    cat = t.args.get("catalog")
    db = t.args.get("db")
    if cat is not None:
        parts.append(cat.name)
    if db is not None:
        parts.append(db.name)
    parts.append(t.name)
    # Drop a leading known-catalog segment so a physical table has ONE identity
    # in lineage.json whether the SQL wrote catalog.db.table or db.table. Without
    # this the same table appears under two forms (duplicate source_tables, split
    # column lineage). Vocabulary-conditioned: only known catalogs are stripped,
    # so db.table stays intact.
    return _strip_catalog_prefix(".".join(parts))


def _normalize_table_name(name: str) -> str:
    """Normalize table names for schema lookup."""
    return _schema_normalize_table_name(name)


# -- display-expression resolution ---------------------------------------
#
# An expression is stored verbatim (a lineage fact — `SUM(IF(`a`.`bal_tp`=..., `a`.`trn_amt`,
# 0))`), keeping the source query's local `FROM ... AS a` alias. That alias is meaningless once
# the expression is lifted out of its scope for a build/mapping document. Resolving it belongs
# with the parser — it is the authority on what `a` names — but MUST NOT touch the verbatim
# `expression`/`expression_resolution` facts (rewriting a subquery-bound alias to a physical
# table would falsely assert a direct physical projection). So we emit a SEPARATE display form.
#
# The rewrite is regex-based, deliberately mirroring the pipeline's own `_rewrite_sql_qualifiers`
# used for SQL-draft generation: no second parse of SQL the parser already parsed. Every consumer
# reads this resolved form instead of re-deriving it.


def _alias_to_physical_table(alias_bindings: List[dict]) -> Dict[str, str]:
    """`alias(lower) -> physical table short name`, from a scope's `alias_source_bindings`.

    A binding records the physical source even when the alias binds to a subquery scope, so the
    display form can name the real table without the parser's fact layer having to."""
    amap: Dict[str, str] = {}
    for binding in alias_bindings or []:
        alias = str(binding.get("alias") or "").strip().lower()
        physical = binding.get("physical_source_id") or next(
            iter(binding.get("physical_source_ids") or []), None
        )
        if alias and physical:
            amap[alias] = str(physical).split(".")[-1]
    return amap


# A string literal ('...', with '' / \\ escapes) or a comment (/* */ or -- ...). These spans carry
# text that must NEVER be treated as code: a literal like 'a.b' or a Chinese comment mentioning a
# qualifier would otherwise be corrupted by a blind alias substitution.
_SQL_LITERAL_OR_COMMENT = r"'(?:''|\\.|[^'])*'|/\*.*?\*/|--[^\n\r]*"


def resolve_display_expression(expression: Any, alias_bindings: List[dict]) -> Any:
    """Rewrite an expression's local FROM aliases to the real table they name, for display.

    Single physical source among the referenced aliases -> the qualifier is dropped (bare column;
    unambiguous, since a field lists its source table alongside). Multiple sources -> each column
    is qualified with its real table. Nothing to resolve (no bindings, no alias present) -> the
    expression is returned unchanged.

    No SQL parse: a single tokenizing regex alternates `string-literal-or-comment | alias.`, so a
    `WHERE ...` clause, `GROUP BY a, b`, and any spacing are handled with no special cases — and,
    crucially, literal/comment spans are matched first and returned verbatim, so `'a.b'` or a
    comment like `/* join to a.x */` is never rewritten. Only real code qualifiers are touched."""
    expr = str(expression or "")
    amap = _alias_to_physical_table(alias_bindings)
    if not expr or not amap:
        return expression
    alias_alt = "|".join(re.escape(a) for a in sorted(amap, key=len, reverse=True))
    token = re.compile(
        rf"(?P<skip>{_SQL_LITERAL_OR_COMMENT})"
        rf"|`(?P<qa>{alias_alt})`\s*\."          # backtick-qualified: `a`.
        rf"|(?<![\w.`])(?P<ba>{alias_alt})\s*\.",  # bare qualifier: a. (not mid-identifier/dotted)
        re.IGNORECASE | re.DOTALL,
    )

    def alias_of(match: "re.Match") -> str:
        return (match.group("qa") or match.group("ba") or "").lower()

    # first pass over code-only matches: which distinct physical tables are actually referenced
    present = {amap[alias_of(m)] for m in token.finditer(expr) if not m.group("skip")}
    if not present:
        return expression
    single_source = len(present) == 1

    def rewrite(match: "re.Match") -> str:
        if match.group("skip"):
            return match.group(0)  # literal / comment: leave exactly as-is
        return "" if single_source else f"`{amap[alias_of(match)]}`."

    return token.sub(rewrite, expr)


# -- projection processing -----------------------------------------------


_TYPE_RANK: Dict[str, int] = {
    "AGGREGATE": 6, "WINDOW": 5, "CONDITIONAL": 4,
    "LITERAL_SUBQUERY": 3, "EXPRESSION": 2, "DIRECT": 1, "CONSTANT": 0,
}


def _extract_name_inner(
    proj: exp.Expression, *, recover_generated_alias: bool = True
) -> Tuple[str, exp.Expression]:
    if isinstance(proj, exp.Alias):
        inner = proj.this
        generated_col_alias = bool(re.fullmatch(r"_col_\d+", str(proj.alias or "")))
        if (
            recover_generated_alias
            and
            generated_col_alias
            and inner.meta.get(_ORIGINALLY_ANONYMOUS_PROJECTION_META)
            and not isinstance(inner, (exp.Explode, exp.Posexplode))
        ):
            # Qualification inserted the alias; it was not present in the SQL.  For a
            # wrapper over exactly one field, retain the existing, evidence-backed naming
            # rule instead of leaking sqlglot's positional placeholder into target_field.
            distinct_columns = {col.name for col in inner.find_all(exp.Column) if col.name}
            if len(distinct_columns) == 1:
                return next(iter(distinct_columns)), inner
        return proj.alias, proj.this
    if isinstance(proj, exp.Column):
        return proj.name, proj
    if isinstance(proj, exp.Star):
        return "*", proj
    # Anonymous expression (no AS alias). NEVER use the raw SQL text: it carries the /* */
    # comments sqlglot attaches (and a commented-out `-- ... as x`), so the whole expression —
    # comment and all — would propagate downstream as a bogus field name (see issue: mapping
    # field name = `COALESCE(...) /* ... */`).
    output_name = proj.output_name
    if output_name:
        return output_name, proj
    # A null-default / wrapper over a SINGLE source column — COALESCE(col, 0), NVL(col, '') —
    # is that column; name it after the sole referenced column (lineage-correct, and the exact
    # shape of the failing cases). Only when one distinct column is referenced.
    distinct_columns = {col.name for col in proj.find_all(exp.Column) if col.name}
    if len(distinct_columns) == 1:
        return next(iter(distinct_columns)), proj
    # Genuinely multi-column / column-free expression: keep a COMMENT-FREE SQL name so it is at
    # least clean and recognizable as an expression for downstream review — never with comments.
    return proj.sql(dialect=DIALECT, comments=False), proj


# -- lineage traversal ---------------------------------------------------


# -- JOIN ----------------------------------------------------------------
