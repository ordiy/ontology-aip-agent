import sqlite3
import re
from dataclasses import dataclass


class PermissionDenied(Exception):
    pass


@dataclass
class SQLClassification:
    operation: str  # read, write, delete, admin
    approval_mode: str  # auto, confirm, deny


@dataclass
class SQLResult:
    operation: str
    rows: list[dict] | None = None
    affected_rows: int = 0
    needs_approval: bool = False
    error: str | None = None


_OPERATION_PATTERNS = [
    (r"^\s*(SELECT|WITH)\b", "read"),
    (r"^\s*(INSERT|UPDATE|REPLACE)\b", "write"),
    (r"^\s*DELETE\b", "delete"),
    (r"^\s*(DROP|CREATE|ALTER|TRUNCATE)\b", "admin"),
]


class SQLExecutor:
    def __init__(self, db_path: str, permissions: dict[str, str]):
        self._db_path = db_path
        self._permissions = permissions

    def classify(self, sql: str) -> SQLClassification:
        sql_upper = sql.strip()
        for pattern, operation in _OPERATION_PATTERNS:
            if re.match(pattern, sql_upper, re.IGNORECASE):
                approval_mode = self._permissions.get(operation, "deny")
                return SQLClassification(operation=operation, approval_mode=approval_mode)
        return SQLClassification(operation="admin", approval_mode="deny")

    def execute(self, sql: str, approved: bool = False) -> SQLResult:
        classification = self.classify(sql)

        if classification.approval_mode == "deny":
            raise PermissionDenied(
                f"Operation '{classification.operation}' is denied by permission policy"
            )

        if classification.approval_mode == "confirm" and not approved:
            return SQLResult(
                operation=classification.operation,
                needs_approval=True,
            )

        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(sql)
            if classification.operation == "read":
                rows = [dict(row) for row in cursor.fetchall()]
                return SQLResult(operation=classification.operation, rows=rows)
            else:
                conn.commit()
                return SQLResult(
                    operation=classification.operation,
                    affected_rows=cursor.rowcount,
                )
        finally:
            conn.close()
