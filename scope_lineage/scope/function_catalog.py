"""SQL function-name catalogs used to classify expressions."""
from __future__ import annotations

_AGGREGATE_FUNCTIONS = {
    "avg",
    "collect_list",
    "collect_set",
    "count",
    "count_if",
    "max",
    "min",
    "sum",
}


_CLEANING_FUNCTIONS = {
    "coalesce",
    "nvl",
    "replace",
    "regexp_replace",
    "substr",
    "substring",
    "trim",
}


_KNOWN_SCALAR_FUNCTIONS = {
    *_AGGREGATE_FUNCTIONS,
    *_CLEANING_FUNCTIONS,
    "abs",
    "ceil",
    "concat",
    "concat_ws",
    "current_date",
    "current_timestamp",
    "date_add",
    "date_format",
    "date_sub",
    "datediff",
    "floor",
    "from_unixtime",
    "greatest",
    "least",
    "left",
    "length",
    "lower",
    "md5",
    "now",
    "regexp_extract",
    "right",
    "round",
    "row_number",
    "sha2",
    "split",
    "to_date",
    "unix_timestamp",
    "upper",
}


_KNOWN_UDAFS = frozenset({
    "COLLECT_SET", "COLLECT_LIST", "CONCAT_WS", "PERCENTILE",
    "PERCENTILE_APPROX", "HISTOGRAM_NUMERIC", "NVL",
})
