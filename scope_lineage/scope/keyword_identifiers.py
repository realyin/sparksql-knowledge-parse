"""Re-quote identifiers that collide with SQL keywords, so one column cannot cost a statement.

Spark accepts `not`, `like`, `out` and `using` as column names when they are quoted, and authors
routinely leave them unquoted — a machine-generated projection list is not going to be
hand-audited for keyword collisions. sqlglot's parser stops at the first such identifier, the
statement falls back to the lenient parse, and its whole projection list is discarded, so a
statement can lose the sources for nearly every column it writes over one or two identifiers
(KEYWORD-IDENT-001).

The repair does not carry a list of reserved words. `Spark.Tokenizer.KEYWORDS` is 305 entries of
tokenizer vocabulary rather than Spark's reserved set, so any list is simultaneously too wide
(quoting `not` inside `v NOT IN (...)` would break working SQL) and too narrow (it drifts with
each sqlglot release). Instead the parser is the oracle: it reports the token it stopped on, that
token is quoted, and the statement is parsed again. A rewrite survives only if it makes the
statement parse.

Two properties are load-bearing, and both were established by measurement rather than reasoning:

*Candidates are tried, not ranked.* A ParseError names the failing token twice, in ``highlight``
and at the end of ``start_context``, and neither field wins consistently — fixing a priority
order between them repaired only half the observed cases whichever order was chosen. Trying both
and keeping whichever parses repaired all of them.

*Clause keywords are never quoted.* This is not a theoretical guard. A statement whose SQL is
genuinely malformed — an empty WHERE body — parses once its `WHERE` is quoted, yielding an AST
in which WHERE is a column name. Statements that stay `recovered` are the honest outcome for SQL
that is simply broken; a confidently wrong lineage is worse than a degraded one.
"""

from __future__ import annotations

import re
from functools import lru_cache

import sqlglot
from sqlglot.errors import ParseError

DIALECT = "spark"

# Quoting one of these can turn malformed SQL into an AST that parses and means something else.
# A column genuinely named `where` is possible but must be quoted by its author; failing to
# repair that column is a far cheaper mistake than silently reinterpreting a broken statement.
_NEVER_QUOTE = frozenset({
    "select", "from", "where", "group", "order", "by", "having", "limit", "join", "on",
    "union", "insert", "overwrite", "into", "table", "values", "set", "with", "as",
    "when", "then", "else", "end", "case", "and", "or", "partition", "distinct",
})

_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_TRAILING_WORD = re.compile(r"([A-Za-z_]\w*)$")

# A statement needing more than this is not a keyword-collision problem; stop rather than
# quote our way through a genuinely broken statement one token at a time.
_MAX_ROUNDS = 12


def _parses(sql: str) -> bool:
    try:
        sqlglot.parse(sql, dialect=DIALECT, error_level=sqlglot.ErrorLevel.RAISE)
    except Exception:  # noqa: BLE001 - probe: any failure means 'does not parse'
        return False
    return True


def _candidates(error: dict) -> list[str]:
    """The token the parser stopped on, as each of the two fields that report it."""
    found: list[str] = []
    trailing = _TRAILING_WORD.search((error.get("start_context") or "").rstrip())
    if trailing:
        found.append(trailing.group(1))
    highlight = (error.get("highlight") or "").strip()
    if _IDENTIFIER.fullmatch(highlight):
        found.append(highlight)
    return found


def _quote(sql: str, word: str) -> str:
    """Quote every bare occurrence, leaving already-quoted and suffixed ones untouched."""
    return re.sub(rf"(?<![\w`]){re.escape(word)}(?![\w`])", f"`{word}`", sql)


@lru_cache(maxsize=64)
def repair_keyword_identifiers(sql: str) -> tuple[str, tuple[str, ...]]:
    """Return SQL that parses plus the identifiers quoted to get there.

    The original text and an empty tuple are returned whenever the statement already parses,
    or when no sequence of rewrites makes it parse — the caller must not be able to tell a
    "nothing needed doing" from a "nothing could be done", because in both cases the text it
    should use is the text it passed in.
    """
    if _parses(sql):
        return sql, ()

    current, quoted = sql, []
    for _ in range(_MAX_ROUNDS):
        try:
            sqlglot.parse(current, dialect=DIALECT, error_level=sqlglot.ErrorLevel.RAISE)
        except ParseError as exc:
            errors = getattr(exc, "errors", None) or []
        except Exception:  # noqa: BLE001 - non-ParseError means repair cannot proceed; return the original
            return sql, ()
        else:
            return current, tuple(quoted)

        if not errors:
            return sql, ()

        # Prefer a candidate that resolves the statement outright; otherwise take the first
        # that changes anything and let the next round judge it.
        chosen = None
        for word in _candidates(errors[0]):
            if word in quoted or word.lower() in _NEVER_QUOTE:
                continue
            rewritten = _quote(current, word)
            if rewritten == current:
                continue
            if _parses(rewritten):
                chosen = (rewritten, word)
                break
            if chosen is None:
                chosen = (rewritten, word)
        if chosen is None:
            return sql, ()
        current, word = chosen
        quoted.append(word)

    return (current, tuple(quoted)) if _parses(current) else (sql, ())
