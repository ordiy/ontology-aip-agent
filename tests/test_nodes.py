import pytest
from src.agent.state import AgentState
from src.agent.nodes import (
    load_ontology_context,
    classify_intent,
    generate_sql,
    execute_sql_node,
    format_result,
)


class FakeLLM:
    """Fake LLM that returns canned responses based on call count."""
    def __init__(self, responses: list[str]):
        self._responses = responses
        self._call_index = 0

    def chat(self, messages, system_prompt=None, temperature=0.0):
        resp = self._responses[self._call_index]
        self._call_index += 1
        return resp

    def get_model_name(self):
        return "fake-model"


def test_load_ontology_context():
    state: AgentState = {"ontology_context": "", "messages": []}
    context_text = "Domain: Test\nTables: users"
    result = load_ontology_context(state, context_text)
    assert result["ontology_context"] == context_text


def test_classify_intent_read():
    llm = FakeLLM(["READ"])
    state: AgentState = {
        "user_query": "How many customers are there?",
        "ontology_context": "Domain: Test\nTables: customers",
        "messages": [],
        "clarify_count": 0,
    }
    result = classify_intent(state, llm)
    assert result["intent"] == "READ"


def test_classify_intent_write():
    llm = FakeLLM(["WRITE"])
    state: AgentState = {
        "user_query": "Update all orders to cancelled",
        "ontology_context": "Domain: Test\nTables: orders",
        "messages": [],
        "clarify_count": 0,
    }
    result = classify_intent(state, llm)
    assert result["intent"] == "WRITE"


def test_generate_sql():
    llm = FakeLLM(["SELECT COUNT(*) as total FROM customers"])
    state: AgentState = {
        "user_query": "How many customers?",
        "ontology_context": "Domain: Test\nTables: customers\n\nTable: customers\n  Columns: id (INTEGER PK), name (TEXT)",
        "intent": "READ",
        "messages": [],
    }
    result = generate_sql(state, llm)
    assert "SELECT" in result["generated_sql"]
    assert result["permission_level"] == "auto"


def test_generate_sql_write_gets_confirm():
    llm = FakeLLM(["UPDATE orders SET status = 'cancelled' WHERE status = 'overdue'"])
    state: AgentState = {
        "user_query": "Cancel overdue orders",
        "ontology_context": "Domain: Test\nTables: orders",
        "intent": "WRITE",
        "messages": [],
    }
    result = generate_sql(state, llm)
    assert result["permission_level"] == "confirm"


def test_format_result_read():
    llm = FakeLLM(["There are 42 customers in total."])
    state: AgentState = {
        "user_query": "How many customers?",
        "query_result": [{"total": 42}],
        "affected_rows": 0,
        "intent": "READ",
        "messages": [],
        "generated_sql": "SELECT COUNT(*) as total FROM customers",
    }
    result = format_result(state, llm)
    assert "42" in result["response"]
