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

from unittest.mock import MagicMock

def test_execute_sql_node_signals_retry_on_first_error():
    """execute_sql_node should signal retry (not set error) on first SQL failure."""
    from src.agent.nodes import execute_sql_node
    from src.database.executor import SQLExecutor, SQLResult

    # Mock executor that returns an error
    mock_executor = MagicMock()
    mock_executor.execute.return_value = SQLResult(
        operation="read", rows=None, affected_rows=0,
        needs_approval=False, error="no such table: nonexistent"
    )

    state = {
        "generated_sql": "SELECT * FROM nonexistent",
        "permission_level": "auto",
        "approved": None,
        "sql_retry_count": 0,
    }
    result = execute_sql_node(state, mock_executor)

    # Should signal retry, not propagate error
    assert result.get("sql_error_message") == "no such table: nonexistent"
    assert result.get("sql_retry_count") == 1
    assert result.get("error") is None  # Error NOT propagated on first attempt


def test_execute_sql_node_propagates_error_on_second_failure():
    """execute_sql_node should propagate error after 1 retry (sql_retry_count >= 1)."""
    from src.agent.nodes import execute_sql_node
    from src.database.executor import SQLExecutor, SQLResult

    mock_executor = MagicMock()
    mock_executor.execute.return_value = SQLResult(
        operation="read", rows=None, affected_rows=0,
        needs_approval=False, error="syntax error"
    )

    state = {
        "generated_sql": "SELECT INVALID",
        "permission_level": "auto",
        "approved": None,
        "sql_retry_count": 1,  # Already retried once
    }
    result = execute_sql_node(state, mock_executor)

    # Should propagate the error after retry
    assert result.get("error") == "syntax error"
    assert result.get("sql_error_message") is None


def test_generate_sql_includes_error_context_on_retry():
    """generate_sql should include previous error in prompt when sql_error_message is set."""
    from src.agent.nodes import generate_sql

    captured_messages = []

    class CapturingFakeLLM:
        def chat(self, messages, system_prompt=None, temperature=0.0):
            captured_messages.extend(messages)
            return "SELECT COUNT(*) FROM customers"
        def get_model_name(self):
            return "fake"

    state = {
        "user_query": "count customers",
        "ontology_context": "Table: customers",
        "intent": "READ",
        "generated_sql": "SELECT * FROM custmers",  # Old broken SQL
        "sql_error_message": "no such table: custmers",
        "sql_retry_count": 1,
    }
    result = generate_sql(state, CapturingFakeLLM())
    # The messages sent to LLM should contain error context
    all_content = " ".join(m.get("content", "") for m in captured_messages)
    assert "custmers" in all_content or "no such table" in all_content

def test_generate_sql_includes_conversation_history():
    """generate_sql should include prior conversation turns in LLM messages for context."""
    captured_messages = []

    class CapturingFakeLLM:
        def chat(self, messages, system_prompt=None, temperature=0.0):
            captured_messages.extend(messages)
            return "SELECT * FROM accounts WHERE id = 42"
        def get_model_name(self):
            return "fake"

    state = {
        "user_query": "show his transactions",
        "ontology_context": "Table: accounts\nTable: transactions",
        "intent": "READ",
        "generated_sql": "",
        "sql_error_message": None,
        "sql_retry_count": 0,
        "conversation_history": [
            {
                "query": "who has the highest balance?",
                "sql": "SELECT * FROM accounts ORDER BY balance DESC LIMIT 1",
                "result_summary": "1 rows. Sample: id=42, name=Alice, balance=99999",
            }
        ],
    }
    result = generate_sql(state, CapturingFakeLLM())

    # The prior turn should appear in the messages sent to the LLM
    all_content = " ".join(m.get("content", "") for m in captured_messages)
    assert "who has the highest balance" in all_content or "Alice" in all_content or "id=42" in all_content
    assert "SELECT" in result["generated_sql"]


def test_generate_sql_empty_history_works_normally():
    """generate_sql with empty conversation_history should work as before."""
    from src.agent.nodes import generate_sql

    class FakeLLM:
        def chat(self, messages, system_prompt=None, temperature=0.0):
            return "SELECT COUNT(*) FROM customers"
        def get_model_name(self):
            return "fake"

    state = {
        "user_query": "how many customers?",
        "ontology_context": "Table: customers",
        "intent": "READ",
        "generated_sql": "",
        "sql_error_message": None,
        "sql_retry_count": 0,
        "conversation_history": [],
    }
    result = generate_sql(state, FakeLLM())
    assert "SELECT" in result["generated_sql"]
