"""Shared SQL helper utilities used across node sub-modules."""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_DDL_PREFIXES: tuple[str, ...] = ("DROP", "CREATE", "ALTER", "TRUNCATE")


def clean_sql(raw: str) -> str:
    """Remove markdown code fences from an LLM-generated SQL string.

    Args:
        raw: Raw LLM response text, potentially wrapped in ``` fences.

    Returns:
        Plain SQL string without fences or surrounding whitespace.
    """
    sql = raw.strip()
    sql = re.sub(r"^```sql\s*", "", sql)
    sql = re.sub(r"^```\s*", "", sql)
    sql = re.sub(r"\s*```$", "", sql)
    return sql.strip()


def detect_permission_level(sql: str) -> str:
    """Classify a SQL statement as ``auto``, ``confirm``, or ``deny``.

    * ``auto``    – SELECT / WITH (read-only; no confirmation required).
    * ``deny``    – DDL statements (DROP, CREATE, ALTER, TRUNCATE).
    * ``confirm`` – DML statements (INSERT, UPDATE, DELETE, MERGE, …).

    Args:
        sql: SQL statement to classify.

    Returns:
        One of ``"auto"``, ``"confirm"``, or ``"deny"``.
    """
    upper = sql.upper().lstrip()
    if upper.startswith(("SELECT", "WITH")):
        return "auto"
    if upper.startswith(_DDL_PREFIXES):
        return "deny"
    return "confirm"
