"""Query Planner for the Federation Layer.

Decomposes a SQL query and routes sub-queries to the correct executor.
"""

import logging
from dataclasses import dataclass, field
from typing import Literal

from src.database.executor import SQLResult
from src.ontology.provider import OntologyProvider
from src.federation.executor_registry import ExecutorRegistry
from src.federation.parser import extract_tables
from src.federation.rewriter import expand_virtual_entities

logger = logging.getLogger(__name__)


@dataclass
class SubQuery:
    """Represents a part of a query to be executed on a specific engine."""
    engine: str
    sql: str
    projected_columns: list[str] = field(default_factory=list)


@dataclass
class QueryPlan:
    """Represents a planned execution strategy for a query."""
    kind: Literal["single", "federated"]
    sub_queries: list[SubQuery]
    join_spec: object | None = None  # Phase 3 will define JoinSpec


class QueryPlanner:
    """Decomposes a SQL query and routes sub-queries to the correct executor.

    Phase 1 scope: same-engine routing only. If all referenced tables resolve to
    the same engine, produce a single-engine plan. If tables span multiple
    engines, raise NotImplementedError('cross-source federation not yet supported').
    """

    def __init__(
        self,
        ontology: OntologyProvider,
        registry: ExecutorRegistry,
    ) -> None:
        """Initialize the query planner.

        Args:
            ontology: Provider for mapping entities to physical tables/engines.
            registry: Registry of available executors.
        """
        self._ontology = ontology
        self._registry = registry

    def plan(self, sql: str) -> QueryPlan:
        """Plan the execution of a SQL query.

        Args:
            sql: The SQL query to plan.

        Returns:
            A QueryPlan specifying how to execute the query.

        Raises:
            NotImplementedError: If the query spans multiple engines.
        """
        ctx = self._ontology.context
        
        default_engine = self._registry.default().dialect
        
        if ctx.virtual_entities:
            rewritten_sql = expand_virtual_entities(sql, ctx.virtual_entities, dialect=default_engine)
        else:
            rewritten_sql = sql
            
        tables = extract_tables(rewritten_sql, dialect=default_engine)
        
        mappings = ctx.physical_mappings

        engines = set()

        for table in tables:
            resolved_engine = None

            # First priority: match exact physical_table
            for _, mapping in mappings.items():
                if mapping.physical_table == table:
                    resolved_engine = mapping.query_engine
                    break

            # Second priority: match entity name (the dict key)
            if not resolved_engine:
                if table in mappings:
                    resolved_engine = mappings[table].query_engine

            if resolved_engine:
                engines.add(resolved_engine)
            else:
                logger.debug(f"Table '{table}' not found in ontology mappings. Falling back to default engine.")
                # We need to find the name of the default engine from registry
                engines.add(self._registry._default_engine)

        if not engines:
            engines.add(self._registry._default_engine)

        if len(engines) > 1:
            raise NotImplementedError("cross-source federation not yet supported (Phase 3)")

        # Single engine execution
        engine = engines.pop()
        
        # In phase 1, we just pass the original SQL
        # Phase 2: use the rewritten SQL containing expanded virtual entities
        sub_query = SubQuery(engine=engine, sql=rewritten_sql)
        
        return QueryPlan(
            kind="single",
            sub_queries=[sub_query],
        )

    def execute(self, plan: QueryPlan, approved: bool = False) -> SQLResult:
        """Execute a query plan.

        Args:
            plan: The execution plan.
            approved: Whether the operation has been approved (for writes).

        Returns:
            The combined result of the query.

        Raises:
            NotImplementedError: If the plan is federated.
        """
        if plan.kind == "federated":
            raise NotImplementedError("cross-source federation not yet supported (Phase 3)")

        sub_query = plan.sub_queries[0]
        executor = self._registry.get(sub_query.engine)
        
        return executor.execute(sub_query.sql, approved=approved)
