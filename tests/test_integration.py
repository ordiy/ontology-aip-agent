"""Integration test: ontology → DB → executor → agent nodes, with fake LLM."""
import sqlite3
from src.ontology.parser import parse_ontology
from src.ontology.context import generate_context
from src.database.schema import create_tables
from src.database.mock_data import generate_mock_data
from src.database.executor import SQLExecutor
from src.agent.nodes import classify_intent, generate_sql, execute_sql_node, format_result


class FakeLLM:
    def __init__(self, responses):
        self._responses = iter(responses)

    def chat(self, messages, system_prompt=None, temperature=0.0):
        return next(self._responses)

    def get_model_name(self):
        return "fake"


def test_full_read_pipeline(sample_rdf_path, tmp_path):
    """Test the full pipeline: parse RDF → create DB → mock data → query."""
    # 1. Parse ontology
    schema = parse_ontology(sample_rdf_path)
    assert schema.domain == "Test Store"
    assert len(schema.classes) == 3

    # 2. Create DB + mock data
    db_path = str(tmp_path / "test.db")
    create_tables(db_path, schema)
    generate_mock_data(db_path, schema, rows_per_table=20)

    # Verify data exists
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    assert count == 20
    conn.close()

    # 3. Generate context
    context = generate_context(schema)
    assert "customers" in context

    # 4. Run agent nodes with fake LLM
    executor = SQLExecutor(db_path, {"read": "auto", "write": "confirm", "delete": "confirm", "admin": "deny"})

    state = {
        "messages": [],
        "ontology_context": context,
        "user_query": "How many customers are there?",
        "clarify_count": 0,
    }

    # Classify intent
    llm = FakeLLM(["READ"])
    result = classify_intent(state, llm)
    assert result["intent"] == "READ"
    state.update(result)

    # Generate SQL
    llm = FakeLLM(["SELECT COUNT(*) as total FROM customers"])
    result = generate_sql(state, llm)
    assert "SELECT" in result["generated_sql"]
    state.update(result)

    # Execute SQL
    result = execute_sql_node(state, executor)
    assert result["query_result"][0]["total"] == 20
    state.update(result)

    # Format result
    llm = FakeLLM(["There are 20 customers in the database."])
    result = format_result(state, llm)
    assert "20" in result["response"]


def test_full_write_pipeline_with_approval(sample_rdf_path, tmp_path):
    """Test write pipeline requires approval."""
    schema = parse_ontology(sample_rdf_path)
    db_path = str(tmp_path / "test.db")
    create_tables(db_path, schema)
    generate_mock_data(db_path, schema, rows_per_table=10)

    context = generate_context(schema)
    executor = SQLExecutor(db_path, {"read": "auto", "write": "confirm", "delete": "confirm", "admin": "deny"})

    state = {
        "messages": [],
        "ontology_context": context,
        "user_query": "Set all order totals to 0",
        "clarify_count": 0,
    }

    # Classify as WRITE
    llm = FakeLLM(["WRITE"])
    result = classify_intent(state, llm)
    state.update(result)

    # Generate SQL
    llm = FakeLLM(["UPDATE orders SET total = 0"])
    result = generate_sql(state, llm)
    assert result["permission_level"] == "confirm"
    state.update(result)

    # Execute without approval — should signal needs_approval
    state["approved"] = False
    result = execute_sql_node(state, executor)
    assert result.get("approved") is None  # needs approval

    # Execute with approval
    state["approved"] = True
    state["permission_level"] = "confirm"
    result = execute_sql_node(state, executor)
    assert result["affected_rows"] == 10

def test_full_analyze_pipeline(sample_rdf_path, tmp_path):
    """Test the full ANALYZE pipeline: planner → multi-step execute → synthesize."""
    from src.ontology.parser import parse_ontology
    from src.ontology.context import generate_context
    from src.database.schema import create_tables
    from src.database.mock_data import generate_mock_data
    from src.database.executor import SQLExecutor
    from src.agent.nodes import plan_analysis, execute_analysis_step, synthesize_results

    # Setup
    schema = parse_ontology(sample_rdf_path)
    db_path = str(tmp_path / "analyze_test.db")
    create_tables(db_path, schema)
    generate_mock_data(db_path, schema, rows_per_table=20)

    executor = SQLExecutor(db_path, {"read": "auto", "write": "confirm", "delete": "confirm", "admin": "deny"})
    context = generate_context(schema)

    # Step 1: Plan
    planner_responses = iter([
        "1. How many customers are there?\n2. What is the total number of orders?"
    ])

    class PlannerFakeLLM:
        def chat(self, messages, system_prompt=None, temperature=0.0):
            return next(planner_responses)
        def get_model_name(self):
            return "fake"

    state = {
        "user_query": "Give me an overview of customers and orders",
        "ontology_context": context,
        "analysis_plan": [],
        "sub_results": [],
        "conversation_history": [],
    }

    plan_result = plan_analysis(state, PlannerFakeLLM())
    assert len(plan_result["analysis_plan"]) == 2
    state.update(plan_result)

    # Step 2: Execute step 1
    sql_responses = iter([
        "SELECT COUNT(*) as total FROM customers",
        "SELECT COUNT(*) as total FROM orders",
    ])

    class ExecFakeLLM:
        def chat(self, messages, system_prompt=None, temperature=0.0):
            return next(sql_responses)
        def get_model_name(self):
            return "fake"

    exec_result_1 = execute_analysis_step(state, ExecFakeLLM(), executor)
    assert len(exec_result_1["sub_results"]) == 1
    assert exec_result_1["sub_results"][0]["rows"][0]["total"] == 20
    state.update(exec_result_1)

    # Step 3: Execute step 2
    exec_result_2 = execute_analysis_step(state, ExecFakeLLM(), executor)
    assert len(exec_result_2["sub_results"]) == 2
    assert exec_result_2["sub_results"][1]["rows"][0]["total"] == 20
    state.update(exec_result_2)

    # Step 4: Synthesize
    class SynthFakeLLM:
        def chat(self, messages, system_prompt=None, temperature=0.0):
            return "There are 20 customers and 20 orders in the database."
        def get_model_name(self):
            return "fake"

    synth_result = synthesize_results(state, SynthFakeLLM())
    assert "20" in synth_result["response"]
    assert synth_result.get("result_summary") is not None
