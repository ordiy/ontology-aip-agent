"""SQL execution with permission control and timeout enforcement.

Architecture: BaseExecutor is an ABC that defines the execute() contract.
SQLiteExecutor (aliased as SQLExecutor for backward compatibility) is the
production implementation. Future drivers (e.g. StarRocksExecutor) implement
the same interface so the agent graph never needs to know which backend is active.
"""

import sqlite3
import re
import threading
from abc import ABC, abstractmethod
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


class BaseExecutor(ABC):
    """Abstract base class for all SQL executors.

    Defines the interface that the agent graph depends on so concrete
    backends (SQLite, StarRocks, etc.) are interchangeable without
    touching any graph or node code.

    Subclasses must implement:
      - execute(sql, approved) -> SQLResult
      - dialect property -> str   (e.g. "SQLite", "MySQL")

    The shared classify() helper is provided here because SQL operation
    classification is backend-agnostic (it's just regex on the SQL text).
    """

    @property
    @abstractmethod
    def dialect(self) -> str:
        """SQL dialect name injected into LLM SQL-generation prompts.

        Examples: "SQLite", "MySQL (StarRocks-compatible)"
        """
        ...

    @abstractmethod
    def execute(self, sql: str, approved: bool = False) -> SQLResult:
        """Execute sql and return a SQLResult.

        Args:
            sql: The SQL statement to run.
            approved: True if the user has explicitly confirmed a write/delete.

        Returns:
            SQLResult with rows, affected_rows, needs_approval, or error.

        Raises:
            PermissionDenied: if the operation is blocked by policy.
        """
        ...

    def classify(self, sql: str) -> SQLClassification:
        """Classify sql by operation type and look up the permission mode.

        Shared across all executors — classification is purely regex-based
        so it doesn't depend on the backend.
        """
        for pattern, operation in _OPERATION_PATTERNS:
            if re.match(pattern, sql.strip(), re.IGNORECASE):
                approval_mode = self._permissions.get(operation, "deny")
                return SQLClassification(operation=operation, approval_mode=approval_mode)
        return SQLClassification(operation="admin", approval_mode="deny")


class SQLiteExecutor(BaseExecutor):
    """SQLite executor with permission control and per-query timeout.

    Uses ThreadPoolExecutor to enforce QUERY_TIMEOUT_SECONDS — SQLite has
    no built-in per-query timeout, so we run queries in a thread and cancel
    if they exceed the limit.
    """

    def __init__(self, db_path: str, permissions: dict[str, str]):
        self._db_path = db_path
        self._permissions = permissions

    @property
    def dialect(self) -> str:
        return "SQLite"

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


# Backward-compatibility alias — all existing code that imports SQLExecutor
# continues to work unchanged. New code should prefer SQLiteExecutor.
SQLExecutor = SQLiteExecutor
