"""Shared sqlglot runtime configuration."""

from __future__ import annotations

import logging


class _InvalidJsonPathWarningFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("Invalid JSON path syntax.")


def suppress_invalid_json_path_warnings() -> None:
    """Suppress noisy sqlglot warnings for Spark JSON paths like '$.0.key'."""
    logger = logging.getLogger("sqlglot")
    if any(isinstance(f, _InvalidJsonPathWarningFilter) for f in logger.filters):
        return
    logger.addFilter(_InvalidJsonPathWarningFilter())
