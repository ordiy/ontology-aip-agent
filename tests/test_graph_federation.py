from unittest.mock import MagicMock
from src.agent.graph import build_graph
from src.database.executor import BaseExecutor, SQLResult
from src.ontology.provider import OntologyProvider, OntologyContext, PhysicalMapping

class FakeExecutor(BaseExecutor):
    def __init__(self, name="fake", dialect="sqlite"):
        self._name = name
        self._dialect = dialect
        self.execute_called_with = None

    @property
    def dialect(self) -> str:
        return self._dialect

    def execute(self, sql: str, approved: bool = False) -> SQLResult:
        self.execute_called_with = sql
        return SQLResult(operation="read", rows=[{"res": self._name}], affected_rows=0, needs_approval=False)

class FakeLLM:
    def __init__(self, responses):
        self._responses = iter(responses)

    def chat(self, messages, system_prompt=None, temperature=0.0):
        return next(self._responses)

    def get_model_name(self): return "fake"

class MockOntologyProvider(OntologyProvider):
    def __init__(self, mappings: dict[str, str]):
        self._mappings = mappings

    def load(self) -> OntologyContext:
        phys = {
            k: PhysicalMapping(physical_table=k, query_engine=v)
            for k, v in self._mappings.items()
        }
        return OntologyContext(schema_for_llm="mock schema", rules={}, physical_mappings=phys)

def test_build_graph_accepts_single_executor():
    llm = FakeLLM(["READ", "SELECT 1 FROM tbl", "Result 1"])
    executor = FakeExecutor("single")
    ontology = MockOntologyProvider({"tbl": "sqlite"})
    
    graph = build_graph(llm, executor, ontology)
    result = graph.invoke({"user_query": "q", "clarify_count": 0, "messages": []})
    
    assert executor.execute_called_with == "SELECT 1 FROM tbl"
    assert result["response"] == "Result 1"

def test_build_graph_accepts_executor_dict():
    llm = FakeLLM(["READ", "SELECT 1 FROM tbl", "Result 1"])
    executor = FakeExecutor("dict")
    ontology = MockOntologyProvider({"tbl": "sqlite"})
    
    graph = build_graph(llm, {"sqlite": executor}, ontology)
    result = graph.invoke({"user_query": "q", "clarify_count": 0, "messages": []})
    
    assert executor.execute_called_with == "SELECT 1 FROM tbl"
    assert result["response"] == "Result 1"

def test_build_graph_routes_to_correct_engine():
    llm = FakeLLM(["READ", "SELECT 1 FROM users", "Result 1"])
    exec_a = FakeExecutor("sqlite")
    exec_b = FakeExecutor("starrocks", dialect="mysql")
    ontology = MockOntologyProvider({"users": "starrocks"})
    
    graph = build_graph(llm, {"sqlite": exec_a, "starrocks": exec_b}, ontology)
    result = graph.invoke({"user_query": "query", "clarify_count": 0, "messages": []})
    
    assert exec_a.execute_called_with is None
    assert exec_b.execute_called_with == "SELECT 1 FROM users"
    assert result["response"] == "Result 1"
