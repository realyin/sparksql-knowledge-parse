"""Optional authoritative target-table metadata for INSERT column binding."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

import sqlglot
from sqlglot import ErrorLevel, exp

from .schema_metadata import (
    MetadataFileError,
    check_metadata_file,
    normalize_table_name,
)
from ..sqlglot_config import suppress_invalid_json_path_warnings


suppress_invalid_json_path_warnings()


@dataclass(frozen=True)
class TargetColumnMetadata:
    name: str
    data_type: str = ""
    ordinal: int = 0
    is_partition: bool = False
    comment: str = ""


@dataclass
class TargetTableMetadata:
    table_name: str
    full_table_name: str
    columns: list[TargetColumnMetadata]
    partition_columns: list[str]
    ddl: str
    source_file: str
    validation_issues: list[str] = field(default_factory=list)
    query_time: str = ""
    ddl_update_time: str = ""
    data_source: str = ""
    structure_source: str = "ddl"

    @property
    def usable(self) -> bool:
        return bool(self.columns) and not self.validation_issues


class TargetMetadataMap(dict[str, TargetTableMetadata]):
    """Normalized ``db.table`` -> authoritative target metadata."""

    def __init__(
        self,
        metadata: Mapping[str, TargetTableMetadata] | None = None,
    ) -> None:
        super().__init__()
        # File-level rejections land here, mirroring SchemaMap: one unreadable file costs that
        # file, and the reason travels with the map instead of being lost to an exception.
        self.metadata_conflicts: list[dict] = []
        for table, item in (metadata or {}).items():
            normalized = normalize_table_name(table or item.table_name)
            if normalized:
                self[normalized] = item


def load_target_table_metadata(
    path: str | Path,
    *,
    sanitize_nul: bool = False,
    provenance: list[dict] | None = None,
    provenance_role: str = "target_ddl_metadata",
) -> TargetMetadataMap:
    """Load one metadata JSON file or a directory containing one file per table.

    File extensions are intentionally not semantic: platform downloads often append ``.txt``
    to JSON filenames. Every selected file must contain a JSON object; malformed JSON fails
    the metadata load before any task is parsed.
    """
    root = Path(path)
    files = _metadata_files(root)
    candidates: dict[str, list[TargetTableMetadata]] = {}
    rejected: list[dict] = []
    provenance_by_file: dict[str, dict] = {}
    for file_path in files:
        text = check_metadata_file(
            file_path,
            sanitize_nul=sanitize_nul,
            provenance=provenance,
            role=provenance_role,
        )
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            # Skip this file rather than abandoning the directory: 0.1.6 set that rule for source
            # schema after two bad files among 3434 left every table without columns, and the
            # same argument applies here. A load that produced no table at all still raises,
            # below (META-ISOLATION-001).
            rejected.append({
                "table": "",
                "source_file": Path(file_path).name,
                "reason": "metadata_rejected",
                "issues": [f"not valid JSON: line {exc.lineno}, column {exc.colno}: {exc.msg}"],
            })
            continue
        item = _target_table_metadata_from_document(document, file_path)
        key = normalize_table_name(item.table_name or item.full_table_name)
        if not key:
            rejected.append({
                "table": "",
                "source_file": Path(file_path).name,
                "reason": "metadata_rejected",
                "issues": ["no recognizable table name"],
            })
            continue
        candidates.setdefault(key, []).append(item)
        if provenance is not None and provenance:
            # The platform commonly names JSON exports ``*.txt``. Provenance records the
            # parsed content format, not the transport suffix, so contract consumers do not
            # mistake valid JSON for a new opaque metadata format.
            provenance[-1]["format"] = "json"
            provenance[-1]["table_name"] = key
            provenance[-1]["usable"] = item.usable
            provenance[-1]["validation_issues"] = list(item.validation_issues)
            provenance[-1]["query_time"] = item.query_time or None
            provenance[-1]["ddl_update_time"] = item.ddl_update_time or None
            provenance[-1]["data_source"] = item.data_source or None
            provenance[-1]["structure_source"] = item.structure_source
            provenance_by_file[file_path.name] = provenance[-1]
    result = TargetMetadataMap()
    for key, versions in candidates.items():
        selected = _select_latest_metadata(key, versions)
        result[key] = selected
        for item in versions:
            provenance_item = provenance_by_file.get(item.source_file)
            if provenance_item is not None:
                provenance_item["selected_version"] = item is selected
    result.metadata_conflicts = rejected
    if not result and rejected:
        # Nothing loaded is the one case that must not pass quietly -- an empty map reads exactly
        # like "these tables have no metadata". Name every file, so an operator learns what to
        # fix rather than only that something failed.
        detail = "; ".join(
            f"{item['source_file']}: {', '.join(item['issues'])}" for item in rejected[:3]
        )
        raise MetadataFileError(
            f"目标表 DDL/Schema 元数据全部无效，未能加载任何表\n  {detail}"
        )
    return result


def lookup_target_table_metadata(
    metadata: Mapping[str, TargetTableMetadata] | None,
    table_name: str,
) -> TargetTableMetadata | None:
    if not metadata:
        return None
    normalized = normalize_table_name(table_name)
    item = metadata.get(normalized)
    if item is not None:
        return item
    for key, candidate in metadata.items():
        if normalize_table_name(key) == normalized:
            return candidate
        if normalize_table_name(candidate.full_table_name) == normalized:
            return candidate
    return None


def _metadata_files(path: Path) -> list[Path]:
    if not path.exists():
        raise MetadataFileError(f"目标表 DDL/Schema 元数据路径不存在: {path}")
    if path.is_file():
        return [path]
    files = sorted(
        item
        for item in path.iterdir()
        if item.is_file()
        and not item.name.startswith(".")
        and (
            item.suffix.lower() == ".json"
            or (
                item.suffix.lower() == ".txt"
                and ".json" in item.name.lower()
            )
        )
    )
    named_exports = [
        item
        for item in files
        if "_metadata.json" in item.name.lower()
    ]
    if named_exports:
        files = named_exports
    if not files:
        raise MetadataFileError(
            f"目标表 DDL/Schema 元数据目录中没有 JSON/.txt 文件: {path}"
        )
    return files


def _target_table_metadata_from_document(
    document: object,
    source_path: Path,
) -> TargetTableMetadata:
    if not isinstance(document, dict):
        raise MetadataFileError(
            f"目标表 DDL/Schema 元数据顶层必须是 JSON object: {source_path}"
        )
    table_name = str(
        document.get("table_name")
        or document.get("full_table_name")
        or ""
    ).strip()
    full_table_name = str(document.get("full_table_name") or table_name).strip()
    schema = document.get("schema")
    ddl = str(document.get("ddl") or "")
    schema_issues: list[str] = []
    columns = _columns_from_schema(schema, schema_issues)
    ddl_issues: list[str] = []
    ddl_present = bool(ddl.strip())
    ddl_table, ddl_columns, ddl_partitions = _facts_from_ddl(ddl, ddl_issues)
    if not ddl_present:
        # A platform Schema export is an accepted structural fallback when the same
        # document does not contain DDL. Schema validation issues still remain fatal.
        ddl_issues = [issue for issue in ddl_issues if issue != "ddl_missing"]
    issues = [*schema_issues, *ddl_issues]
    reconciled = False
    if ddl_present and not ddl_issues:
        columns, reconciled = _columns_reconciled_to_ddl(
            columns,
            ddl_columns,
            ddl_partitions,
        )
        if reconciled:
            # The DDL settled the column set, so complaints about the array's own shape
            # no longer describe anything the caller receives.
            issues = [
                issue
                for issue in issues
                if issue != "schema_column_indices_not_contiguous"
            ]
    partition_columns = (
        list(ddl_partitions)
        if reconciled
        else [column.name for column in columns if column.is_partition]
    )

    normalized_table = normalize_table_name(table_name)
    if ddl_table and normalize_table_name(ddl_table) != normalized_table:
        issues.append("schema_ddl_table_name_mismatch")

    return TargetTableMetadata(
        table_name=normalized_table,
        full_table_name=full_table_name,
        columns=columns,
        partition_columns=partition_columns,
        ddl=ddl,
        source_file=source_path.name,
        validation_issues=_unique_ordered(issues),
        query_time=str(document.get("query_time") or "").strip(),
        ddl_update_time=str(
            document.get("ddl_update_time") or ""
        ).strip(),
        data_source=str(document.get("data_source") or "").strip(),
        structure_source="ddl" if ddl_present else "schema",
    )


def _columns_reconciled_to_ddl(
    columns: list[TargetColumnMetadata],
    ddl_columns: list[str],
    ddl_partitions: list[str],
) -> tuple[list[TargetColumnMetadata], bool]:
    """The DDL decides which columns exist; the exported array only enriches them.

    These are two descriptions of one table, not two claims to be checked against each
    other. The DDL is the table's own definition, so it settles the column set — including
    a partition column declared only in ``PARTITIONED BY``, which is an ordinary export
    shape rather than a contradiction. The array supplies type and comment for the names it
    covers and is otherwise ignored; a column it names that the DDL does not have is a
    stale export, and publishing it would assert a column the table says is not there.

    Deliberately not a union of the two: with both sources partially winning, neither is
    authoritative and the result describes no real table (METADATA-001).
    """
    schema_by_name = {column.name: column for column in columns}
    partition_names = list(dict.fromkeys(ddl_partitions))
    all_names = list(dict.fromkeys([*ddl_columns, *partition_names]))
    return (
        [
            TargetColumnMetadata(
                name=name,
                data_type=schema_by_name[name].data_type if name in schema_by_name else "",
                ordinal=ordinal,
                is_partition=name in set(partition_names),
                comment=schema_by_name[name].comment if name in schema_by_name else "",
            )
            for ordinal, name in enumerate(all_names)
        ],
        True,
    )


def _select_latest_metadata(
    table_name: str,
    versions: list[TargetTableMetadata],
) -> TargetTableMetadata:
    if len(versions) == 1:
        return versions[0]
    ranked = [
        (_metadata_timestamp(item), item)
        for item in versions
    ]
    if any(timestamp is None for timestamp, _ in ranked):
        raise MetadataFileError(
            f"目标表 DDL/Schema 元数据重复且缺少可比较版本时间: {table_name};"
            f"文件 {', '.join(item.source_file for item in versions)}"
        )
    ranked.sort(key=lambda value: value[0])
    if ranked[-1][0] == ranked[-2][0]:
        raise MetadataFileError(
            f"目标表 DDL/Schema 元数据重复且最新版本时间相同: {table_name};"
            f"文件 {', '.join(item.source_file for item in versions)}"
        )
    return ranked[-1][1]


def _metadata_timestamp(item: TargetTableMetadata) -> float | None:
    raw = item.query_time or item.ddl_update_time
    if not raw:
        return None
    try:
        return datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return None


def _columns_from_schema(
    schema: object,
    issues: list[str],
) -> list[TargetColumnMetadata]:
    if not isinstance(schema, list) or not schema:
        issues.append("schema_missing_or_empty")
        return []
    columns: list[TargetColumnMetadata] = []
    for index, raw in enumerate(schema):
        if not isinstance(raw, dict):
            issues.append(f"schema_column_not_object:{index}")
            continue
        name = str(raw.get("columnName") or raw.get("name") or "").strip().lower()
        if not name:
            issues.append(f"schema_column_name_missing:{index}")
            continue
        raw_ordinal = raw.get("columnIndex", raw.get("ordinal", index))
        try:
            ordinal = int(raw_ordinal)
        except (TypeError, ValueError):
            ordinal = index
            issues.append(f"schema_column_index_invalid:{index}")
        columns.append(
            TargetColumnMetadata(
                name=name,
                data_type=str(
                    raw.get("columnType") or raw.get("type") or ""
                ).strip(),
                ordinal=ordinal,
                is_partition=_as_bool(
                    raw.get("isPartition", raw.get("partition", False))
                ),
                comment=str(
                    raw.get("columnComment") or raw.get("comment") or ""
                ).strip(),
            )
        )
    ordinals = [column.ordinal for column in columns]
    if sorted(ordinals) != list(range(len(columns))):
        issues.append("schema_column_indices_not_contiguous")
    else:
        columns.sort(key=lambda column: column.ordinal)
    names = [column.name for column in columns]
    if len(names) != len(set(names)):
        issues.append("schema_duplicate_column_names")
    return columns


#: Words that open a table-level constraint rather than name a column. Quoting one of
#: these would turn a constraint into a nonsense column definition.
_CONSTRAINT_STARTERS = frozenset({
    "PRIMARY",
    "FOREIGN",
    "UNIQUE",
    "CONSTRAINT",
    "CHECK",
    "KEY",
    "INDEX",
})


def _spark_keywords() -> frozenset[str]:
    from sqlglot.dialects import Spark

    return frozenset(
        word for word in Spark.Tokenizer.KEYWORDS if " " not in word
    )


def _column_list_bounds(ddl: str) -> tuple[int, int] | None:
    """Locate the top-level column list, or report that it could not be found.

    Scanning rather than matching: a COMMENT may carry parentheses and commas of its own,
    and mistaking one for structure would corrupt the DDL instead of merely failing to
    normalize it.
    """
    depth = 0
    start = -1
    quote = ""
    index = 0
    while index < len(ddl):
        char = ddl[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
            if depth == 1:
                start = index
        elif char == ")":
            depth -= 1
            if depth == 0 and start >= 0:
                return start, index
            if depth < 0:
                return None
        index += 1
    return None


def _split_column_definitions(region: str) -> list[str] | None:
    """Split a column list on its own commas — those at nesting depth zero."""
    segments: list[str] = []
    depth = 0
    quote = ""
    current = 0
    index = 0
    while index < len(region):
        char = region[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char in "(<":
            depth += 1
        elif char in ")>":
            depth -= 1
            if depth < 0:
                return None
        elif char == "," and depth == 0:
            segments.append(region[current:index])
            current = index + 1
        index += 1
    if quote or depth:
        return None
    segments.append(region[current:])
    return segments


def _quoted_column_definition(segment: str, keywords: frozenset[str]) -> str:
    stripped = segment.lstrip()
    if not stripped or not (stripped[0].isalpha() or stripped[0] == "_"):
        return segment
    name = ""
    for char in stripped:
        if char.isalnum() or char == "_":
            name += char
        else:
            break
    upper = name.upper()
    if upper not in keywords or upper in _CONSTRAINT_STARTERS:
        return segment
    rest = stripped[len(name):]
    if not rest.strip():
        # A lone word is not a column definition; leave it for the parser to judge.
        return segment
    lead = segment[: len(segment) - len(stripped)]
    return f"{lead}`{name}`{rest}"


def _quoted_keyword_column_names(ddl: str) -> str:
    """Backquote column names that are reserved words, before sqlglot sees them.

    sqlglot's Spark dialect does not terminate on ``CREATE TABLE db.t (a DOUBLE, not
    DOUBLE)`` — 30.0.0, 30.6.0 and 30.17.0 all hang on that one statement, and quoting the
    name makes it parse in milliseconds. The metadata DDL is a platform export, so such a
    column is legal and real: dropping the table would be the wrong answer, and a caller
    cannot wrap sqlglot in a timeout because ``signal.alarm`` is swallowed by sqlglot's own
    ``except Exception``. Removing the trigger is the only place left to act
    (METADATA-002).

    Quoting is an equivalent rewrite — ``ColumnDef.name`` strips the quotes again — so the
    facts extracted downstream are unchanged. Anything this function cannot locate with
    confidence is returned untouched, leaving the verdict to the existing parse-failure
    path.
    """
    bounds = _column_list_bounds(ddl)
    if bounds is None:
        return ddl
    start, end = bounds
    segments = _split_column_definitions(ddl[start + 1:end])
    if segments is None:
        return ddl
    keywords = _spark_keywords()
    rewritten = [_quoted_column_definition(segment, keywords) for segment in segments]
    if rewritten == segments:
        return ddl
    return ddl[: start + 1] + ",".join(rewritten) + ddl[end:]


def _facts_from_ddl(
    ddl: str,
    issues: list[str],
) -> tuple[str, list[str], list[str]]:
    if not ddl.strip():
        issues.append("ddl_missing")
        return "", [], []
    try:
        tree = sqlglot.parse_one(
            _quoted_keyword_column_names(ddl),
            dialect="spark",
            error_level=ErrorLevel.RAISE,
        )
    except Exception as exc:
        issues.append(f"ddl_parse_failed:{type(exc).__name__}")
        return "", [], []
    if not isinstance(tree, exp.Create) or not isinstance(tree.this, exp.Schema):
        issues.append("ddl_not_create_table_with_schema")
        return "", [], []

    table = tree.this.this
    table_name = table.sql(dialect="spark") if isinstance(table, exp.Table) else ""
    columns = [
        column.name.lower()
        for column in tree.this.expressions
        if isinstance(column, exp.ColumnDef) and column.name
    ]
    partitions: list[str] = []
    for prop in tree.find_all(exp.PartitionedByProperty):
        expressions: Iterable[exp.Expression] = (
            getattr(prop.this, "expressions", None) or []
        )
        for item in expressions:
            column = item.this if isinstance(item, exp.ColumnDef) else item
            name = getattr(column, "name", "")
            if name:
                partitions.append(str(name).lower())
    return table_name, columns, partitions


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _unique_ordered(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
