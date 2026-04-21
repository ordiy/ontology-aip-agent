"""Joiner module for executing federated plans."""

import logging
from typing import Optional
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

    def __init__(self, registry: ExecutorRegistry, row_limit: int = JOIN_ROW_LIMIT) -> None:
        """Initializes the Joiner.

        Args:
            registry: The executor registry to resolve engines.
            row_limit: Maximum allowed rows from a single subquery.
        """
        self._registry = registry
        self._row_limit = row_limit

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
            executor = self._registry.get(sub.engine)
            result = executor.execute(sub.sql, approved=approved)
            
            if result.error is not None:
                return SQLResult(
                    operation="read",
                    error=f"[federation:{sub.engine}] {result.error}"
                )
                
            rows = result.rows if result.rows is not None else []
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
                return SQLResult(
                    operation="read",
                    error=f"[federation] join aborted: side_{i} returned {row_count} rows > limit {self._row_limit}. Add filters or request pushdown (Phase 4)."
                )
                
            df = pd.DataFrame(rows)
            dfs.append(df)
            
        conn = duckdb.connect(database=':memory:')
        try:
            for i, alias in enumerate(plan.join_spec.sub_aliases):
                conn.register(alias, dfs[i])
                
            df_result = conn.execute(plan.join_spec.final_sql).df()
            final_rows = df_result.to_dict(orient="records")
            
            return SQLResult(
                operation="read",
                rows=final_rows,
                affected_rows=0,
                needs_approval=False,
                error=None
            )
        finally:
            conn.close()
