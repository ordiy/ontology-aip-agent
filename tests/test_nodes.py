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

def test_plan_analysis_parses_numbered_list():
    """plan_analysis should parse LLM numbered list into analysis_plan steps."""
    from src.agent.nodes import plan_analysis

    class FakeLLM:
        def chat(self, messages, system_prompt=None, temperature=0.0):
            return "1. What is total revenue this month?\n2. What was revenue last month?\n3. Which products grew most?"
        def get_model_name(self):
            return "fake"

    state = {
        "user_query": "Compare this month vs last month revenue",
        "ontology_context": "Table: orders",
        "conversation_history": [],
    }
    result = plan_analysis(state, FakeLLM())
    assert len(result["analysis_plan"]) == 3
    assert "this month" in result["analysis_plan"][0].lower()
    assert result["sub_results"] == []


def test_classify_intent_analyze():
    """classify_intent should return ANALYZE for multi-step questions."""
    from src.agent.nodes import classify_intent

    class FakeLLM:
        def chat(self, messages, system_prompt=None, temperature=0.0):
            return "ANALYZE"
        def get_model_name(self):
            return "fake"

    state = {
        "user_query": "Compare this month vs last month revenue",
        "ontology_context": "Table: orders",
    }
    result = classify_intent(state, FakeLLM())
    assert result["intent"] == "ANALYZE"


def test_synthesize_results_calls_llm():
    """synthesize_results should format sub_results and return response."""
    from src.agent.nodes import synthesize_results

    class FakeLLM:
        def chat(self, messages, system_prompt=None, temperature=0.0):
            return "This month revenue is higher by 20%."
        def get_model_name(self):
            return "fake"

    state = {
        "user_query": "Compare months",
        "ontology_context": "Table: orders",
        "sub_results": [
            {"step": "This month", "sql": "SELECT SUM(total) FROM orders WHERE ...", "rows": [{"total": 1200}], "error": None},
            {"step": "Last month", "sql": "SELECT SUM(total) FROM orders WHERE ...", "rows": [{"total": 1000}], "error": None},
        ],
    }
    result = synthesize_results(state, FakeLLM())
    assert "20%" in result["response"] or "higher" in result["response"]

def test_execute_analysis_step_executes_sql_and_appends_result(tmp_path):
    """execute_analysis_step should generate SQL, execute it, and append to sub_results."""
    import sqlite3
    from src.agent.nodes import execute_analysis_step
    from src.database.executor import SQLExecutor

    # Create a simple test DB
    db_path = str(tmp_path / "test_analyze.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, total REAL, status TEXT)")
    conn.execute("INSERT INTO orders VALUES (1, 100.0, 'completed')")
    conn.execute("INSERT INTO orders VALUES (2, 200.0, 'completed')")
    conn.execute("INSERT INTO orders VALUES (3, 50.0, 'pending')")
    conn.commit()
    conn.close()

    executor = SQLExecutor(db_path, {"read": "auto", "write": "confirm", "delete": "confirm", "admin": "deny"})

    class FakeLLM:
        def chat(self, messages, system_prompt=None, temperature=0.0):
            return "SELECT COUNT(*) as total_orders FROM orders"
        def get_model_name(self):
            return "fake"

    state = {
        "analysis_plan": ["How many orders are there?", "What is the average order total?"],
        "sub_results": [],  # No steps executed yet
        "ontology_context": "Table: orders (id, total, status)",
        "user_query": "Analyze order statistics",
    }

    result = execute_analysis_step(state, FakeLLM(), executor)

    assert len(result["sub_results"]) == 1
    step = result["sub_results"][0]
    assert step["step"] == "How many orders are there?"
    assert "SELECT" in step["sql"].upper()
    # Rows should be returned (FakeLLM generated a valid COUNT query)
    assert step["rows"] is not None
    assert step["error"] is None


def test_execute_analysis_step_continues_after_sql_error(tmp_path):
    """execute_analysis_step should record the error and NOT abort the pipeline."""
    import sqlite3
    from src.agent.nodes import execute_analysis_step
    from src.database.executor import SQLExecutor

    db_path = str(tmp_path / "test_analyze_err.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, total REAL)")
    conn.commit()
    conn.close()

    executor = SQLExecutor(db_path, {"read": "auto", "write": "confirm", "delete": "confirm", "admin": "deny"})

    class BadSQLFakeLLM:
        def chat(self, messages, system_prompt=None, temperature=0.0):
            # Returns SQL referencing a non-existent table
            return "SELECT * FROM nonexistent_table"
        def get_model_name(self):
            return "fake"

    state = {
        "analysis_plan": ["Query that will fail"],
        "sub_results": [],
        "ontology_context": "Table: orders (id, total)",
        "user_query": "Analyze orders",
    }

    result = execute_analysis_step(state, BadSQLFakeLLM(), executor)

    # Should append the step with an error, NOT raise an exception
    assert len(result["sub_results"]) == 1
    step = result["sub_results"][0]
    assert step["error"] is not None  # Error was recorded
    assert "nonexistent" in step["error"].lower() or "no such table" in step["error"].lower()


def test_plan_analysis_caps_at_four_steps():
    """plan_analysis should cap analysis_plan at 4 steps even if LLM returns more."""
    from src.agent.nodes import plan_analysis

    class VerboseFakeLLM:
        def chat(self, messages, system_prompt=None, temperature=0.0):
            # Returns 7 steps
            return "\n".join(f"{i}. Step {i}" for i in range(1, 8))
        def get_model_name(self):
            return "fake"

    state = {
        "user_query": "Very complex multi-faceted analysis question",
        "ontology_context": "Table: orders",
        "conversation_history": [],
    }
    result = plan_analysis(state, VerboseFakeLLM())
    # Must be capped at 4 regardless of how many the LLM returned
    assert len(result["analysis_plan"]) <= 4


def test_synthesize_results_produces_result_summary():
    """synthesize_results should return result_summary for conversation memory."""
    from src.agent.nodes import synthesize_results

    class FakeLLM:
        def chat(self, messages, system_prompt=None, temperature=0.0):
            return "Revenue increased by 20% this month."
        def get_model_name(self):
            return "fake"

    state = {
        "user_query": "Compare revenue month over month",
        "ontology_context": "Table: orders",
        "sub_results": [
            {"step": "This month revenue", "sql": "SELECT SUM(total) FROM orders WHERE ...", "rows": [{"total": 1200}], "error": None},
            {"step": "Last month revenue", "sql": "SELECT SUM(total) FROM orders WHERE ...", "rows": [{"total": 1000}], "error": None},
        ],
    }
    result = synthesize_results(state, FakeLLM())

    # Must have both response and result_summary
    assert result.get("response") == "Revenue increased by 20% this month."
    assert result.get("result_summary") is not None
    assert len(result["result_summary"]) <= 150

def test_route_after_analysis_step_continues_when_steps_remain():
    """_route_after_analysis_step should return 'execute_analysis_step' when plan has more steps."""
    from src.agent.graph import _route_after_analysis_step

    state = {
        "analysis_plan": ["step 1", "step 2", "step 3"],
        "sub_results": [{"step": "step 1", "sql": "SELECT 1", "rows": [], "error": None}],
    }
    assert _route_after_analysis_step(state) == "execute_analysis_step"


def test_route_after_analysis_step_synthesizes_when_all_done():
    """_route_after_analysis_step should return 'synthesize_results' when all steps executed."""
    from src.agent.graph import _route_after_analysis_step

    state = {
        "analysis_plan": ["step 1", "step 2"],
        "sub_results": [
            {"step": "step 1", "sql": "SELECT 1", "rows": [], "error": None},
            {"step": "step 2", "sql": "SELECT 2", "rows": [], "error": None},
        ],
    }
    assert _route_after_analysis_step(state) == "synthesize_results"


def test_route_after_analysis_step_synthesizes_on_empty_plan():
    """_route_after_analysis_step should handle empty plan gracefully."""
    from src.agent.graph import _route_after_analysis_step

    state = {"analysis_plan": [], "sub_results": []}
    assert _route_after_analysis_step(state) == "synthesize_results"
