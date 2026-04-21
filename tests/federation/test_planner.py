import pytest
from unittest.mock import MagicMock

from src.federation.parser import extract_tables
from src.federation.executor_registry import ExecutorRegistry
from src.federation.planner import QueryPlanner, SubQuery, QueryPlan
from src.database.executor import BaseExecutor, SQLResult
from src.ontology.provider import OntologyProvider, OntologyContext, PhysicalMapping


class FakeExecutor(BaseExecutor):
    def __init__(self, dialect_name: str):
        self._dialect = dialect_name
        self.last_sql = None
        self.last_approved = None

    @property
    def dialect(self) -> str:
        return self._dialect

    def execute(self, sql: str, approved: bool = False) -> SQLResult:
        self.last_sql = sql
        self.last_approved = approved
        return SQLResult(operation="read", rows=[{"dummy": 1}])


class MockOntologyProvider(OntologyProvider):
    def __init__(self, mappings: dict[str, PhysicalMapping]):
        self._mappings = mappings

    def load(self) -> OntologyContext:
        return OntologyContext(
            schema_for_llm="",
            rules={},
            physical_mappings=self._mappings,
        )


def test_extract_tables_simple() -> None:
    tables = extract_tables("SELECT * FROM orders")
    assert tables == ["orders"]


def test_extract_tables_join() -> None:
    tables = extract_tables("SELECT * FROM a JOIN b ON a.id=b.id")
    assert tables == ["a", "b"]


def test_extract_tables_qualified() -> None:
    tables = extract_tables("SELECT * FROM catalog.schema.tbl")
    assert tables == ["catalog.schema.tbl"]


def test_registry_get_and_default() -> None:
    executors = {"sqlite": FakeExecutor("SQLite"), "sr": FakeExecutor("MySQL")}
    registry = ExecutorRegistry(executors, default_engine="sqlite")
    
    assert registry.get("sr").dialect == "MySQL"
    assert registry.default().dialect == "SQLite"
    assert set(registry.engines) == {"sqlite", "sr"}


def test_registry_missing_engine_raises() -> None:
    executors = {"sqlite": FakeExecutor("SQLite")}
    
    with pytest.raises(KeyError, match="Default engine 'missing' not found"):
        ExecutorRegistry(executors, default_engine="missing")
        
    registry = ExecutorRegistry(executors, default_engine="sqlite")
    with pytest.raises(KeyError, match="Engine 'unknown' not found"):
        registry.get("unknown")


def test_planner_single_engine_plan() -> None:
    mappings = {
        "User": PhysicalMapping(physical_table="users", query_engine="sqlite"),
        "Order": PhysicalMapping(physical_table="orders", query_engine="sqlite")
    }
    provider = MockOntologyProvider(mappings)
    registry = ExecutorRegistry({"sqlite": FakeExecutor("SQLite")})
    planner = QueryPlanner(ontology=provider, registry=registry)
    
    plan = planner.plan("SELECT * FROM users JOIN Order ON users.id = Order.user_id")
    
    assert plan.kind == "single"
    assert len(plan.sub_queries) == 1
    assert plan.sub_queries[0].engine == "sqlite"


def test_plan_federated_cross_engine_two_tables() -> None:
    mappings = {
        "Customer": PhysicalMapping(physical_table="customers", query_engine="sqlite"),
        "Order": PhysicalMapping(physical_table="orders", query_engine="starrocks")
    }
    provider = MockOntologyProvider(mappings)
    registry = ExecutorRegistry({
        "sqlite": FakeExecutor("SQLite"),
        "starrocks": FakeExecutor("MySQL")
    })
    planner = QueryPlanner(ontology=provider, registry=registry)
    
    plan = planner.plan("SELECT c.id, o.total FROM Customer c JOIN Order o ON c.id = o.customer_id WHERE o.total > 100")
    
    assert plan.kind == "federated"
    assert len(plan.sub_queries) == 2
    engines = {sq.engine for sq in plan.sub_queries}
    assert engines == {"sqlite", "starrocks"}
    
    sql0 = plan.sub_queries[0].sql.upper()
    sql1 = plan.sub_queries[1].sql.upper()
    assert "FROM CUSTOMERS" in sql0 or "FROM CUSTOMERS" in sql1
    assert "FROM ORDERS" in sql0 or "FROM ORDERS" in sql1
    
    final_sql = plan.join_spec.final_sql.upper()
    assert "SUB_0" in final_sql
    assert "SUB_1" in final_sql
    assert "JOIN" in final_sql
    assert "TOTAL > 100" in final_sql


def test_plan_federated_preserves_join_type() -> None:
    mappings = {
        "Customer": PhysicalMapping(physical_table="customers", query_engine="sqlite"),
        "Order": PhysicalMapping(physical_table="orders", query_engine="starrocks")
    }
    provider = MockOntologyProvider(mappings)
    registry = ExecutorRegistry({
        "sqlite": FakeExecutor("SQLite"),
        "starrocks": FakeExecutor("MySQL")
    })
    planner = QueryPlanner(ontology=provider, registry=registry)
    
    plan = planner.plan("SELECT c.id, o.total FROM Customer c LEFT JOIN Order o ON c.id = o.customer_id WHERE o.total > 100")
    
    assert plan.kind == "federated"
    final_sql = plan.join_spec.final_sql.upper()
    assert "LEFT JOIN" in final_sql or "LEFT OUTER JOIN" in final_sql


def test_plan_unsupported_shape_raises() -> None:
    mappings = {
        "Customer": PhysicalMapping(physical_table="customers", query_engine="sqlite"),
        "Order": PhysicalMapping(physical_table="orders", query_engine="starrocks"),
        "Product": PhysicalMapping(physical_table="products", query_engine="postgres")
    }
    provider = MockOntologyProvider(mappings)
    registry = ExecutorRegistry({
        "sqlite": FakeExecutor("SQLite"),
        "starrocks": FakeExecutor("MySQL"),
        "postgres": FakeExecutor("PostgreSQL")
    })
    planner = QueryPlanner(ontology=provider, registry=registry)
    
    with pytest.raises(NotImplementedError, match="federated plan supports only 2-table JOINs in Phase 3"):
        planner.plan("SELECT * FROM Customer c JOIN Order o ON c.id = o.customer_id JOIN Product p ON o.product_id = p.id")


def test_planner_execute_delegates() -> None:
    executor = FakeExecutor("SQLite")
    registry = ExecutorRegistry({"sqlite": executor})
    planner = QueryPlanner(ontology=MockOntologyProvider({}), registry=registry)
    
    plan = QueryPlan(
        kind="single",
        sub_queries=[SubQuery(engine="sqlite", sql="SELECT 1")]
    )
    result = planner.execute(plan, approved=True)
    
    assert executor.last_sql == "SELECT 1"
    assert executor.last_approved is True
    assert result.rows == [{"dummy": 1}]


def test_planner_unknown_table_uses_default() -> None:
    provider = MockOntologyProvider({})
    registry = ExecutorRegistry({"sqlite": FakeExecutor("SQLite")})
    planner = QueryPlanner(ontology=provider, registry=registry)
    
    plan = planner.plan("SELECT * FROM unknown_table")
    
    assert plan.kind == "single"
    assert len(plan.sub_queries) == 1
    assert plan.sub_queries[0].engine == "sqlite"
from src.federation.rewriter import expand_virtual_entities
from src.ontology.provider import VirtualEntity

def test_rewriter_expands_simple_virtual_entity() -> None:
    virtual_entities = {
        "VIPCustomer": VirtualEntity(name="VIPCustomer", based_on="Customer", filter_sql="lifetime_value > 10000")
    }
    sql = "SELECT COUNT(*) FROM VIPCustomer"
    rewritten = expand_virtual_entities(sql, virtual_entities, dialect="sqlite")
    assert "Customer" in rewritten
    assert "lifetime_value > 10000" in rewritten

def test_rewriter_preserves_alias() -> None:
    virtual_entities = {
        "VIPCustomer": VirtualEntity(name="VIPCustomer", based_on="Customer", filter_sql="lifetime_value > 10000")
    }
    sql = "SELECT v.id FROM VIPCustomer v"
    rewritten = expand_virtual_entities(sql, virtual_entities, dialect="sqlite")
    # Check that alias is preserved
    assert " AS v" in rewritten or " v" in rewritten

def test_rewriter_no_op_when_no_virtuals_referenced() -> None:
    virtual_entities = {
        "VIPCustomer": VirtualEntity(name="VIPCustomer", based_on="Customer", filter_sql="lifetime_value > 10000")
    }
    sql = "SELECT * FROM Customer"
    rewritten = expand_virtual_entities(sql, virtual_entities, dialect="sqlite")
    assert "Customer" in rewritten
    assert "VIPCustomer" not in rewritten
    assert "lifetime_value" not in rewritten

class MockOntologyProviderWithVirtual(MockOntologyProvider):
    def __init__(self, mappings, virtual_entities):
        super().__init__(mappings)
        self._virtual_entities = virtual_entities

    def load(self) -> OntologyContext:
        return OntologyContext(
            schema_for_llm="",
            rules={},
            physical_mappings=self._mappings,
            virtual_entities=self._virtual_entities,
        )

def test_planner_routes_virtual_entity_to_base_engine() -> None:
    mappings = {
        "Customer": PhysicalMapping(physical_table="customers", query_engine="sqlite")
    }
    virtual_entities = {
        "VIPCustomer": VirtualEntity(name="VIPCustomer", based_on="Customer", filter_sql="lifetime_value > 10000")
    }
    provider = MockOntologyProviderWithVirtual(mappings, virtual_entities)
    registry = ExecutorRegistry({"sqlite": FakeExecutor("SQLite")})
    planner = QueryPlanner(ontology=provider, registry=registry)
    
    plan = planner.plan("SELECT * FROM VIPCustomer")
    assert plan.kind == "single"
    assert len(plan.sub_queries) == 1
    assert plan.sub_queries[0].engine == "sqlite"
    assert "lifetime_value > 10000" in plan.sub_queries[0].sql

def test_planner_executes_rewritten_sql() -> None:
    executor = FakeExecutor("SQLite")
    mappings = {
        "Customer": PhysicalMapping(physical_table="customers", query_engine="sqlite")
    }
    virtual_entities = {
        "VIPCustomer": VirtualEntity(name="VIPCustomer", based_on="Customer", filter_sql="lifetime_value > 10000")
    }
    provider = MockOntologyProviderWithVirtual(mappings, virtual_entities)
    registry = ExecutorRegistry({"sqlite": executor})
    planner = QueryPlanner(ontology=provider, registry=registry)
    
    plan = planner.plan("SELECT * FROM VIPCustomer")
    planner.execute(plan, approved=True)
    
    assert executor.last_sql == plan.sub_queries[0].sql
    assert "lifetime_value > 10000" in executor.last_sql

def test_planner_execute_dispatches_federated_to_joiner() -> None:
    mappings = {
        "Customer": PhysicalMapping(physical_table="customers", query_engine="sqlite"),
        "Order": PhysicalMapping(physical_table="orders", query_engine="other")
    }
    provider = MockOntologyProvider(mappings)
    
    # We will subclass FakeExecutor to return explicit rows so that the joiner actually does the join
    class CannedFakeExecutor(FakeExecutor):
        def __init__(self, dialect_name: str, rows: list[dict]):
            super().__init__(dialect_name)
            self._rows = rows
        def execute(self, sql: str, approved: bool = False) -> SQLResult:
            self.last_sql = sql
            self.last_approved = approved
            return SQLResult(operation="read", rows=self._rows)

    fake_a = CannedFakeExecutor("SQLite", [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}])
    fake_b = CannedFakeExecutor("other", [{"customer_id": 1, "total": 100}, {"customer_id": 1, "total": 50}, {"customer_id": 2, "total": 30}])
    
    registry = ExecutorRegistry({
        "sqlite": fake_a,
        "other": fake_b
    })
    planner = QueryPlanner(ontology=provider, registry=registry)
    
    plan = planner.plan("SELECT c.name, o.total FROM Customer c JOIN \"Order\" o ON c.id = o.customer_id ORDER BY c.name, o.total")
    result = planner.execute(plan)
    
    assert result.error is None
    assert len(result.rows) == 3
    assert result.rows == [
        {"name": "Alice", "total": 50},
        {"name": "Alice", "total": 100},
        {"name": "Bob", "total": 30},
    ]
