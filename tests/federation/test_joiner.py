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


class _FakeSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.updates: list[dict] = []

    def update(self, **kwargs) -> None:
        self.updates.append(kwargs)


class _FakeObs:
    """Minimal ObservabilityClient stand-in capturing span lifecycle."""

    def __init__(self) -> None:
        self.enabled = True
        self.spans: list[_FakeSpan] = []

    def start_span(self, name: str, input=None, metadata=None):
        span = _FakeSpan(name)
        self.spans.append(span)

        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield span

        return _cm()


def test_joiner_emits_nested_spans_when_obs_enabled():
    """Joiner should open one span per sub-query and one for the DuckDB join."""
    fake_a = FakeExecutor("sqlite", [
        SQLResult(operation="read", rows=[{"id": 1, "name": "Alice"}])
    ])
    fake_b = FakeExecutor("other", [
        SQLResult(operation="read", rows=[{"cust_id": 1, "total": 100}])
    ])
    registry = ExecutorRegistry({"sqlite": fake_a, "other": fake_b}, default_engine="sqlite")

    plan = QueryPlan(
        kind="federated",
        sub_queries=[
            SubQuery(engine="sqlite", sql="SELECT * FROM Customer"),
            SubQuery(engine="other", sql="SELECT * FROM Order"),
        ],
        join_spec=JoinSpec(
            sub_aliases=["sub_0", "sub_1"],
            final_sql="SELECT sub_0.name, sub_1.total FROM sub_0 JOIN sub_1 ON sub_0.id = sub_1.cust_id",
        ),
    )

    obs = _FakeObs()
    joiner = Joiner(registry, obs=obs)
    result = joiner.execute(plan)

    assert result.error is None
    span_names = [s.name for s in obs.spans]
    assert span_names == [
        "federation.sub_0[sqlite]",
        "federation.sub_1[other]",
        "federation.join[duckdb]",
    ]
    # Each span should have at least one output update with a row_count.
    for span in obs.spans:
        assert any("output" in u and "row_count" in u["output"] for u in span.updates)


def test_joiner_no_op_when_obs_disabled():
    """Joiner should not crash and should produce no spans when obs is None."""
    fake_a = FakeExecutor("sqlite", [
        SQLResult(operation="read", rows=[{"id": 1, "name": "Alice"}])
    ])
    registry = ExecutorRegistry({"sqlite": fake_a}, default_engine="sqlite")

    plan = QueryPlan(
        kind="federated",
        sub_queries=[SubQuery(engine="sqlite", sql="SELECT * FROM table_a")],
        join_spec=JoinSpec(sub_aliases=["sub_0"], final_sql="SELECT * FROM sub_0"),
    )

    joiner = Joiner(registry, obs=None)
    result = joiner.execute(plan)

    assert result.error is None
    assert len(result.rows) == 1


def test_planner_passes_join_row_limit_to_joiner():
    """QueryPlanner.__init__ accepts join_row_limit and threads it through to Joiner."""
    from src.federation.planner import QueryPlanner
    from src.ontology.provider import OntologyContext, OntologyProvider, PhysicalMapping

    class _Onto(OntologyProvider):
        def __init__(self):
            self._ctx = OntologyContext(
                schema_for_llm="",
                rules={},
                physical_mappings={
                    "Customer": PhysicalMapping(physical_table="customers", query_engine="a", partition_keys=[]),
                    "Order": PhysicalMapping(physical_table="orders", query_engine="b", partition_keys=[]),
                },
            )

        def load(self):
            return self._ctx

    fake_a = FakeExecutor("sqlite", [SQLResult(operation="read", rows=[{"id": i, "name": f"n{i}"} for i in range(5)])])
    fake_b = FakeExecutor("other", [SQLResult(operation="read", rows=[{"cust_id": 1, "total": 1}])])
    registry = ExecutorRegistry({"a": fake_a, "b": fake_b}, default_engine="a")

    planner = QueryPlanner(ontology=_Onto(), registry=registry, join_row_limit=2)
    plan = planner.plan('SELECT c.name, o.total FROM Customer c JOIN "Order" o ON c.id = o.cust_id')
    result = planner.execute(plan, approved=True)
    assert result.error is not None
    assert "limit 2" in result.error
