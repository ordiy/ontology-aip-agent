import pytest
from src.database.executor import SQLResult, BaseExecutor
from src.federation.planner import QueryPlan, SubQuery, JoinSpec
from src.federation.executor_registry import ExecutorRegistry
from src.federation.joiner import Joiner

class FakeExecutor(BaseExecutor):
    def __init__(self, dialect: str, canned: dict[str, SQLResult] | list[SQLResult]) -> None:
        self._dialect = dialect
        self._canned = canned
        self.calls = []
        
    @property
    def dialect(self) -> str: return self._dialect
    
    def execute(self, sql: str, approved: bool = False) -> SQLResult:
        self.calls.append(sql)
        if isinstance(self._canned, dict):
            return self._canned.get(sql) or self._canned.get("*") or SQLResult(operation="read", rows=[])
        return self._canned.pop(0)

def test_joiner_two_sides_returned_joined_rows():
    fake_a = FakeExecutor("sqlite", [
        SQLResult(operation="read", rows=[{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}])
    ])
    fake_b = FakeExecutor("other", [
        SQLResult(operation="read", rows=[{"cust_id": 1, "total": 100}, {"cust_id": 1, "total": 50}, {"cust_id": 2, "total": 30}])
    ])
    registry = ExecutorRegistry({"sqlite": fake_a, "other": fake_b}, default_engine="sqlite")
    
    plan = QueryPlan(
        kind="federated",
        sub_queries=[
            SubQuery(engine="sqlite", sql="SELECT * FROM Customer"),
            SubQuery(engine="other", sql="SELECT * FROM Order")
        ],
        join_spec=JoinSpec(
            sub_aliases=["sub_0", "sub_1"],
            final_sql="SELECT sub_0.name, sub_1.total FROM sub_0 JOIN sub_1 ON sub_0.id = sub_1.cust_id ORDER BY sub_0.name, sub_1.total"
        )
    )
    
    joiner = Joiner(registry)
    result = joiner.execute(plan)
    
    assert result.error is None
    assert len(result.rows) == 3
    assert result.rows == [
        {"name": "Alice", "total": 50},
        {"name": "Alice", "total": 100},
        {"name": "Bob", "total": 30},
    ]

def test_joiner_propagates_subquery_error():
    fake_a = FakeExecutor("sqlite", [
        SQLResult(operation="read", error="boom")
    ])
    registry = ExecutorRegistry({"sqlite": fake_a}, default_engine="sqlite")
    
    plan = QueryPlan(
        kind="federated",
        sub_queries=[SubQuery(engine="sqlite", sql="SELECT * FROM table_a")],
        join_spec=JoinSpec(sub_aliases=["sub_0"], final_sql="SELECT * FROM sub_0")
    )
    
    joiner = Joiner(registry)
    result = joiner.execute(plan)
    
    assert "[federation:sqlite] boom" in result.error
    
def test_joiner_empty_side_returns_empty_result():
    fake_a = FakeExecutor("sqlite", [
        SQLResult(operation="read", rows=[])
    ])
    registry = ExecutorRegistry({"sqlite": fake_a}, default_engine="sqlite")
    
    plan = QueryPlan(
        kind="federated",
        sub_queries=[SubQuery(engine="sqlite", sql="SELECT * FROM table_a")],
        join_spec=JoinSpec(sub_aliases=["sub_0"], final_sql="SELECT * FROM sub_0")
    )
    
    joiner = Joiner(registry)
    result = joiner.execute(plan)
    
    assert result.error is None
    assert result.rows == []

def test_joiner_row_limit_exceeded_returns_error():
    fake_a = FakeExecutor("sqlite", [
        SQLResult(operation="read", rows=[{"id": 1}, {"id": 2}, {"id": 3}])
    ])
    registry = ExecutorRegistry({"sqlite": fake_a}, default_engine="sqlite")
    
    plan = QueryPlan(
        kind="federated",
        sub_queries=[SubQuery(engine="sqlite", sql="SELECT * FROM table_a")],
        join_spec=JoinSpec(sub_aliases=["sub_0"], final_sql="SELECT * FROM sub_0")
    )
    
    joiner = Joiner(registry, row_limit=2)
    result = joiner.execute(plan)
    
    assert "join aborted" in result.error
    assert "limit 2" in result.error
