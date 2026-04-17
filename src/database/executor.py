import sqlite3
import re
import threading
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

QUERY_TIMEOUT_SECONDS = 5  # Maximum seconds allowed for a single SQL query



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
        """Execute SQL with permission check and 5-second timeout.

        Uses ThreadPoolExecutor to enforce query timeout — SQLite has no built-in
        per-query timeout, so we run the query in a thread and cancel if it exceeds
        QUERY_TIMEOUT_SECONDS.

        Args:
            sql: The SQL statement to execute
            approved: Whether the user has approved this write/delete operation

        Returns:
            SQLResult with rows, affected_rows, or error message
        """
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

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._execute_sql_inner, sql, classification)
            try:
                return future.result(timeout=QUERY_TIMEOUT_SECONDS)
            except FuturesTimeoutError:
                return SQLResult(
                    operation=classification.operation,
                    rows=None, affected_rows=0,
                    needs_approval=False,
                    error=f"Query timed out after {QUERY_TIMEOUT_SECONDS} seconds. Try a simpler query."
                )
            except Exception as e:
                return SQLResult(
                    operation=classification.operation,
                    rows=None, affected_rows=0,
                    needs_approval=False,
                    error=str(e)
                )

    def _execute_sql_inner(self, sql: str, classification: SQLClassification) -> SQLResult:
        """Execute SQL against SQLite and return result.

        This runs inside a thread — do not access shared state outside this method.
        """
        conn = sqlite3.connect(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(sql)

            if classification.operation == "read":
                rows = [dict(row) for row in cursor.fetchall()]
                return SQLResult(
                    operation="read",
                    rows=rows, affected_rows=0,
                    needs_approval=False, error=None
                )
            else:
                conn.commit()
                return SQLResult(
                    operation=classification.operation,
                    rows=None, affected_rows=cursor.rowcount,
                    needs_approval=False, error=None
                )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
