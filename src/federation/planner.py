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
class JoinSpec:
    """Describes the final stitch query that runs in the joiner (e.g. DuckDB).

    sub_aliases: the aliases assigned to each SubQuery result, in order
                 matching QueryPlan.sub_queries (e.g. ["sub_0", "sub_1"]).
    final_sql: the SQL to execute in the joiner after each SubQuery has
               been registered as a table named after its alias. References
               sub_aliases in place of the original table names.
    """
    sub_aliases: list[str]
    final_sql: str


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
    join_spec: JoinSpec | None = None


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
        self._joiner = None

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

        if len(engines) <= 1:
            # Single engine execution
            engine = engines.pop() if engines else self._registry._default_engine
            
            # In phase 1, we just pass the original SQL
            # Phase 2: use the rewritten SQL containing expanded virtual entities
            sub_query = SubQuery(engine=engine, sql=rewritten_sql)
            
            return QueryPlan(
                kind="single",
                sub_queries=[sub_query],
            )

        # Cross-engine logic (Phase 3.1)
        import sqlglot
        import sqlglot.expressions as exp
        
        try:
            parse_dialect = default_engine.split()[0].lower() if default_engine else None
            ast = sqlglot.parse_one(rewritten_sql, read=parse_dialect)
        except Exception as e:
            raise ValueError(f"Failed to parse SQL: {e}") from e

        if not isinstance(ast, exp.Select):
            raise NotImplementedError("federated plan supports only 2-table JOINs in Phase 3")

        from_expr = ast.args.get("from_")
        joins = ast.args.get("joins") or []

        if not from_expr or not isinstance(from_expr.this, exp.Table):
            raise NotImplementedError("federated plan supports only 2-table JOINs in Phase 3")
            
        if len(joins) != 1:
            raise NotImplementedError("federated plan supports only 2-table JOINs in Phase 3")
            
        join_expr = joins[0]
        if not isinstance(join_expr.this, exp.Table):
            raise NotImplementedError("federated plan supports only 2-table JOINs in Phase 3")
            
        if not join_expr.args.get("on"):
            raise NotImplementedError("federated plan supports only 2-table JOINs in Phase 3")

        table_a = from_expr.this
        table_b = join_expr.this

        table_a_name = ".".join(p.name for p in table_a.parts)
        table_b_name = ".".join(p.name for p in table_b.parts)

        def resolve_table(name: str) -> tuple[str, str]:
            for _, m in mappings.items():
                if m.physical_table == name:
                    return m.query_engine, m.physical_table
            if name in mappings:
                m = mappings[name]
                return m.query_engine, m.physical_table or name
            return self._registry._default_engine, name

        engine_a, phys_a = resolve_table(table_a_name)
        engine_b, phys_b = resolve_table(table_b_name)
        
        sq0 = SubQuery(engine=engine_a, sql=f"SELECT * FROM {phys_a}")
        sq1 = SubQuery(engine=engine_b, sql=f"SELECT * FROM {phys_b}")

        a_names = {table_a.name}
        if table_a.alias:
            a_names.add(table_a.alias)

        b_names = {table_b.name}
        if table_b.alias:
            b_names.add(table_b.alias)

        table_a.set("this", exp.Identifier(this="sub_0"))
        table_a.set("alias", exp.TableAlias(this=exp.Identifier(this="sub_0")))
        table_a.set("db", None)
        table_a.set("catalog", None)

        table_b.set("this", exp.Identifier(this="sub_1"))
        table_b.set("alias", exp.TableAlias(this=exp.Identifier(this="sub_1")))
        table_b.set("db", None)
        table_b.set("catalog", None)

        for col in ast.find_all(exp.Column):
            if col.table in a_names:
                col.set("table", exp.Identifier(this="sub_0"))
                col.set("db", None)
                col.set("catalog", None)
            elif col.table in b_names:
                col.set("table", exp.Identifier(this="sub_1"))
                col.set("db", None)
                col.set("catalog", None)

        final_sql = ast.sql(dialect="duckdb")

        return QueryPlan(
            kind="federated",
            sub_queries=[sq0, sq1],
            join_spec=JoinSpec(
                sub_aliases=["sub_0", "sub_1"],
                final_sql=final_sql,
            ),
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
            if self._joiner is None:
                from src.federation.joiner import Joiner
                self._joiner = Joiner(self._registry)
            return self._joiner.execute(plan, approved=approved)

        sub_query = plan.sub_queries[0]
        executor = self._registry.get(sub_query.engine)
        
        return executor.execute(sub_query.sql, approved=approved)
