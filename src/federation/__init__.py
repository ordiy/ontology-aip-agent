"""Federation Layer — Orchestrates cross-source queries and execution routing.

Provides the QueryPlanner to parse SQL, map entities to physical engines via the
OntologyProvider, and route sub-queries to appropriate executors.
"""

from src.federation.planner import QueryPlan, QueryPlanner, SubQuery
from src.federation.executor_registry import ExecutorRegistry

__all__ = [
    "QueryPlan",
    "QueryPlanner",
    "SubQuery",
    "ExecutorRegistry",
]
