import re
from src.agent.state import AgentState
from src.llm.base import LLMClient
from src.database.executor import SQLExecutor, PermissionDenied


def load_ontology_context(state: AgentState, context_text: str) -> dict:
    return {"ontology_context": context_text}


def classify_intent(state: AgentState, llm: LLMClient) -> dict:
    system = (
        "You are an intent classifier for a data agent. "
        "Given a user query and database schema, classify the intent as exactly one of: READ, WRITE, UNCLEAR.\n"
        "READ: queries that retrieve/analyze data (SELECT).\n"
        "WRITE: queries that modify data (INSERT, UPDATE, DELETE).\n"
        "UNCLEAR: query is ambiguous or unrelated to the database.\n"
        "Respond with ONLY the single word: READ, WRITE, or UNCLEAR."
    )
    messages = [
        {"role": "user", "content": f"Schema:\n{state['ontology_context']}\n\nUser query: {state['user_query']}"},
    ]
    response = llm.chat(messages, system_prompt=system, temperature=0.0)
    intent = response.strip().upper()
    if intent not in ("READ", "WRITE", "UNCLEAR"):
        intent = "UNCLEAR"
    return {"intent": intent}


def generate_sql(state: AgentState, llm: LLMClient) -> dict:
    """Generate SQL from natural language query using the LLM.

    If sql_error_message is set in state, this is a retry — include the
    previous SQL and error in the prompt so LLM can fix it.
    """
    system = (
        "You are a SQL generator for a SQLite database. "
        "Given the database schema and a user query, generate ONLY the SQL statement. "
        "Do not include any explanation, markdown, or code fences. "
        "Output ONLY the raw SQL statement."
    )
    user_content = (
        f"Database schema:\n{state['ontology_context']}\n\n"
        f"User query: {state['user_query']}\n"
        f"Intent: {state['intent']}"
    )

    # Check if this is a retry with error feedback
    sql_error = state.get("sql_error_message")
    if sql_error:
        # Include previous failed SQL and error message to guide the fix
        error_context = (
            f"\n\nPREVIOUS ATTEMPT FAILED:\n"
            f"SQL: {state.get('generated_sql', '')}\n"
            f"Error: {sql_error}\n"
            f"Please fix the SQL to avoid this error."
        )
        user_content += error_context  # append to existing user message

    messages = [
        {
            "role": "user",
            "content": user_content,
        },
    ]
    response = llm.chat(messages, system_prompt=system, temperature=0.0)

    # Clean up: remove markdown code fences if present
    sql = response.strip()
    sql = re.sub(r"^```sql\s*", "", sql)
    sql = re.sub(r"^```\s*", "", sql)
    sql = re.sub(r"\s*```$", "", sql)
    sql = sql.strip()

    # Determine permission level
    sql_upper = sql.upper().lstrip()
    if sql_upper.startswith(("SELECT", "WITH")):
        permission_level = "auto"
    elif sql_upper.startswith(("DROP", "CREATE", "ALTER", "TRUNCATE")):
        permission_level = "deny"
    else:
        permission_level = "confirm"

    return {"generated_sql": sql, "permission_level": permission_level}


def execute_sql_node(state: AgentState, executor: SQLExecutor) -> dict:
    """Execute the generated SQL statement.

    On SQL syntax/runtime errors (not permission denials), signals the graph
    to retry SQL generation with the error message as feedback.
    Only retries once (sql_retry_count >= 1 → propagate error to format_result).
    """
    sql = state.get("generated_sql", "")
    approved = state.get("approved")
    permission_level = state.get("permission_level", "auto")
    retry_count = state.get("sql_retry_count", 0)

    try:
        result = executor.execute(sql, approved=(approved is True) or permission_level == "auto")
    except PermissionDenied as e:
        return {"error": str(e), "query_result": None, "affected_rows": 0, "sql_error_message": None}
    except Exception as e:
        # If executor raises an exception (e.g., sqlite3.Error)
        error_msg = str(e)
        if retry_count >= 1:
            return {"error": error_msg, "sql_error_message": None}
        return {
            "sql_error_message": error_msg,
            "sql_retry_count": retry_count + 1,
            "error": None,
        }

    # Handle the case where the executor returns an error string instead of raising (used in tests)
    if result.error:
        # If this is already a retry, don't retry again — propagate the error
        if retry_count >= 1:
            return {"error": result.error, "sql_error_message": None}

        # First failure: signal graph to retry SQL generation with error feedback
        # We store the error message so generate_sql can use it
        return {
            "sql_error_message": result.error,
            "sql_retry_count": retry_count + 1,
            "error": None,  # Clear error so format_result isn't triggered
        }

    if result.needs_approval:
        return {"approved": None}  # signal that approval is needed

    rows = result.rows or []
    return {
        "query_result": rows,
        "affected_rows": result.affected_rows,
        "error": None,
        "sql_error_message": None,
    }


def format_result(state: AgentState, llm: LLMClient) -> dict:
    if state.get("error"):
        return {"response": f"Error: {state['error']}"}

    system = (
        "You are a helpful data assistant. Summarize the query result in a clear, "
        "concise natural language response. Be specific with numbers and names."
    )

    if state.get("query_result") is not None:
        result_str = str(state["query_result"][:20])  # limit for context
    else:
        result_str = f"Affected rows: {state.get('affected_rows', 0)}"

    messages = [
        {
            "role": "user",
            "content": (
                f"User asked: {state['user_query']}\n"
                f"SQL executed: {state.get('generated_sql', '')}\n"
                f"Result: {result_str}"
            ),
        },
    ]
    response = llm.chat(messages, system_prompt=system, temperature=0.0)
    return {"response": response.strip()}


def clarify_question(state: AgentState, llm: LLMClient) -> dict:
    system = (
        "You are a helpful data assistant. The user query is unclear. "
        "Ask a brief clarifying question to understand what they want. "
        "Mention what data tables are available."
    )
    messages = [
        {
            "role": "user",
            "content": (
                f"Schema:\n{state['ontology_context']}\n\n"
                f"User query: {state['user_query']}"
            ),
        },
    ]
    response = llm.chat(messages, system_prompt=system, temperature=0.0)
    count = state.get("clarify_count", 0) + 1
    return {"response": response.strip(), "clarify_count": count}
