"""Integration test: two real SQLite databases joined via DuckDB.

Simulates a cross-source federation scenario where Customer lives in one
engine and Order lives in another. The planner produces a federated plan,
the joiner fetches both sides and stitches them in DuckDB.
"""

import sqlite3
from pathlib import Path

import pytest

from src.database.executor import SQLExecutor
from src.federation.executor_registry import ExecutorRegistry
from src.federation.planner import QueryPlanner
from src.ontology.provider import OntologyContext, OntologyProvider, PhysicalMapping


class _StaticOntology(OntologyProvider):
    def __init__(self, ctx: OntologyContext) -> None:
        self._ctx = ctx

    def load(self) -> OntologyContext:
        return self._ctx


def _make_customers_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO customers VALUES (1, 'Alice'), (2, 'Bob'), (3, 'Carol');
        """
    )
    conn.commit()
    conn.close()


def _make_orders_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE orders (id INTEGER PRIMARY KEY, cust_id INTEGER, total REAL);
        INSERT INTO orders VALUES (101, 1, 50.0), (102, 1, 25.0), (103, 2, 10.0);
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def cross_source_setup(tmp_path):
    cust_db = tmp_path / "customers.db"
    ord_db = tmp_path / "orders.db"
    _make_customers_db(cust_db)
    _make_orders_db(ord_db)

    perms = {"read": "auto", "write": "confirm", "delete": "confirm", "admin": "deny"}
    exec_a = SQLExecutor(str(cust_db), permissions=perms)
    exec_b = SQLExecutor(str(ord_db), permissions=perms)

    ctx = OntologyContext(
        schema_for_llm="",
        rules={},
        physical_mappings={
            "Customer": PhysicalMapping(
                physical_table="customers",
                query_engine="sqlite_a",
                partition_keys=[],
            ),
            "Order": PhysicalMapping(
                physical_table="orders",
                query_engine="sqlite_b",
                partition_keys=[],
            ),
        },
    )
    ontology = _StaticOntology(ctx)
    registry = ExecutorRegistry(
        {"sqlite_a": exec_a, "sqlite_b": exec_b},
        default_engine="sqlite_a",
    )
    return QueryPlanner(ontology=ontology, registry=registry)


def test_federated_join_returns_joined_rows(cross_source_setup):
    planner = cross_source_setup
    sql = (
        'SELECT c.name, o.total FROM Customer c JOIN "Order" o '
        "ON c.id = o.cust_id ORDER BY c.name, o.total"
    )
    plan = planner.plan(sql)
    assert plan.kind == "federated"
    assert {sq.engine for sq in plan.sub_queries} == {"sqlite_a", "sqlite_b"}

    result = planner.execute(plan, approved=True)
    assert result.error is None
    assert result.rows == [
        {"name": "Alice", "total": 25.0},
        {"name": "Alice", "total": 50.0},
        {"name": "Bob", "total": 10.0},
    ]


def test_federated_join_with_where_filter(cross_source_setup):
    planner = cross_source_setup
    sql = (
        'SELECT c.name, o.total FROM Customer c JOIN "Order" o '
        "ON c.id = o.cust_id WHERE o.total > 20 ORDER BY c.name, o.total"
    )
    plan = planner.plan(sql)
    result = planner.execute(plan, approved=True)
    assert result.error is None
    assert result.rows == [
        {"name": "Alice", "total": 25.0},
        {"name": "Alice", "total": 50.0},
    ]


def test_federated_left_join_preserves_unmatched(cross_source_setup):
    planner = cross_source_setup
    sql = (
        'SELECT c.name, o.total FROM Customer c LEFT JOIN "Order" o '
        "ON c.id = o.cust_id ORDER BY c.name, o.total"
    )
    plan = planner.plan(sql)
    result = planner.execute(plan, approved=True)
    assert result.error is None
    names = [r["name"] for r in result.rows]
    assert "Carol" in names
