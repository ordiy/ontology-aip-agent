"""Joiner module for executing federated plans."""

import logging
from contextlib import contextmanager, nullcontext
from typing import Any, Optional
import duckdb
import pandas as pd
from src.database.executor import SQLResult
from src.federation.planner import QueryPlan
from src.federation.executor_registry import ExecutorRegistry

JOIN_ROW_LIMIT = 1_000_000

logger = logging.getLogger(__name__)

class Joiner:
    """Executes a federated QueryPlan by collecting sub-query results and
    joining them in an in-process DuckDB session.

    The joiner owns no long-lived state; each execute() call creates a fresh
    in-memory DuckDB connection that is closed before returning.
    """

    def __init__(
        self,
        registry: ExecutorRegistry,
        row_limit: int = JOIN_ROW_LIMIT,
        obs: Any | None = None,
    ) -> None:
        """Initializes the Joiner.

        Args:
            registry: The executor registry to resolve engines.
            row_limit: Maximum allowed rows from a single subquery.
            obs: Optional ObservabilityClient for nested span tracing.
        """
        self._registry = registry
        self._row_limit = row_limit
        self._obs = obs

    def _span(self, name: str, input: Any = None, metadata: dict | None = None):
        if self._obs is None or not getattr(self._obs, "enabled", False):
            return nullcontext(None)
        return self._obs.start_span(name, input=input, metadata=metadata)

    def execute(self, plan: QueryPlan, approved: bool = False) -> SQLResult:
        """Executes a federated QueryPlan using DuckDB.

        Args:
            plan: The federated QueryPlan to execute.
            approved: Whether the query has user approval.

        Returns:
            The combined SQLResult after joining the subqueries.
        
        Raises:
            ValueError: If the plan is not a federated plan.
        """
        if plan.kind != "federated":
            raise ValueError("Joiner only executes federated plans.")
            
        dfs = []
        total_rows = 0
        
        for i, sub in enumerate(plan.sub_queries):
            with self._span(
                f"federation.sub_{i}[{sub.engine}]",
                input={"sql": sub.sql},
                metadata={"engine": sub.engine, "index": i},
            ) as span:
                executor = self._registry.get(sub.engine)
                result = executor.execute(sub.sql, approved=approved)

                if result.error is not None:
                    if span is not None:
                        span.update(level="ERROR", status_message=result.error)
                    return SQLResult(
                        operation="read",
                        error=f"[federation:{sub.engine}] {result.error}"
                    )

                rows = result.rows if result.rows is not None else []
                if span is not None:
                    span.update(output={"row_count": len(rows)})

                if not rows:
                    return SQLResult(
                        operation="read",
                        rows=[],
                        affected_rows=0,
                        needs_approval=False,
                        error=None
                    )

                row_count = len(rows)
                total_rows += row_count

                if total_rows > self._row_limit:
                    msg = f"[federation] join aborted: side_{i} returned {row_count} rows > limit {self._row_limit}. Add filters or request pushdown (Phase 4)."
                    if span is not None:
                        span.update(level="ERROR", status_message=msg)
                    return SQLResult(operation="read", error=msg)

                df = pd.DataFrame(rows)
                dfs.append(df)

        with self._span(
            "federation.join[duckdb]",
            input={"sql": plan.join_spec.final_sql, "aliases": plan.join_spec.sub_aliases},
            metadata={"input_row_counts": [len(df) for df in dfs]},
        ) as span:
            conn = duckdb.connect(database=':memory:')
            try:
                for i, alias in enumerate(plan.join_spec.sub_aliases):
                    conn.register(alias, dfs[i])

                df_result = conn.execute(plan.join_spec.final_sql).df()
                final_rows = df_result.to_dict(orient="records")

                if span is not None:
                    span.update(output={"row_count": len(final_rows)})

                return SQLResult(
                    operation="read",
                    rows=final_rows,
                    affected_rows=0,
                    needs_approval=False,
                    error=None
                )
            finally:
                conn.close()
