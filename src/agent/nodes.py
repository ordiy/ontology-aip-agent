import re
from src.agent.state import AgentState
from src.llm.base import LLMClient
from src.database.executor import SQLExecutor, PermissionDenied


def load_ontology_context(state: AgentState, context_text: str) -> dict:
    return {"ontology_context": context_text}


def classify_intent(state: AgentState, llm: LLMClient) -> dict:
    """Classify user intent as READ, WRITE, ANALYZE, or UNCLEAR.

    ANALYZE is for complex questions requiring multiple SQL queries,
    such as comparisons, trend analysis, or questions with 'and', 'compare',
    'vs', 'difference between', 'breakdown'.
    """
    system = (
        "You are an intent classifier for a data agent. "
        "Given a user query and database schema, classify the intent as exactly one of: READ, WRITE, ANALYZE, UNCLEAR.\n"
        "READ: simple queries that retrieve data with a single SQL (SELECT).\n"
        "WRITE: queries that modify data (INSERT, UPDATE, DELETE).\n"
        "ANALYZE: complex questions needing multiple queries — comparisons, trends, breakdowns, 'vs', 'compare', 'difference'.\n"
        "UNCLEAR: query is ambiguous or unrelated to the database.\n"
        "Respond with ONLY the single word: READ, WRITE, ANALYZE, or UNCLEAR."
    )
    messages = [
        {"role": "user", "content": f"Schema:\n{state['ontology_context']}\n\nUser query: {state['user_query']}"},
    ]
    response = llm.chat(messages, system_prompt=system, temperature=0.0)
    intent = response.strip().upper()
    if intent not in ("READ", "WRITE", "ANALYZE", "UNCLEAR"):
        intent = "UNCLEAR"
    return {"intent": intent}


def generate_sql(state: AgentState, llm: LLMClient) -> dict:
    """Generate SQL from natural language query using the LLM.

    Includes the last 3 conversation turns as context so the LLM can resolve
    references to previous queries (e.g. "show his orders" after asking about a customer).
    If sql_error_message is set, this is a retry — includes previous error for self-correction.
    """
    system = (
        "You are a SQL generator for a SQLite database. "
        "Given the database schema and a user query, generate ONLY the SQL statement. "
        "Do not include any explanation, markdown, or code fences. "
        "Output ONLY the raw SQL statement."
    )

    messages = []

    # Inject the last 3 conversation turns as context before the current query.
    # This lets the LLM resolve pronoun references (e.g. "his orders" → refers to
    # the customer found in the previous turn).
    # We truncate to 3 turns to maintain context window efficiency.
    history = state.get("conversation_history", [])
    for turn in history[-3:]:
        messages.append({
            "role": "user",
            "content": f"Previous query: {turn['query']}\nSQL used: {turn['sql']}\nResult summary: {turn['result_summary']}"
        })
        messages.append({
            "role": "model",
            "content": "Understood."
        })

    # Current query
    user_content = (
        f"Database schema:\n{state['ontology_context']}\n\n"
        f"User query: {state['user_query']}\n"
        f"Intent: {state['intent']}"
    )

    # If retrying after SQL error, append the error context
    sql_error = state.get("sql_error_message")
    if sql_error:
        user_content += (
            f"\n\nPREVIOUS ATTEMPT FAILED:\n"
            f"SQL: {state.get('generated_sql', '')}\n"
            f"Error: {sql_error}\n"
            f"Please fix the SQL to avoid this error."
        )

    messages.append({"role": "user", "content": user_content})

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
    """Format query result as natural language and produce a compact history summary.

    The result_summary is a short string (≤150 chars) stored in conversation_history
    so future turns can reference what was found without bloating the LLM context.
    """
    error_msg = state.get("error")
    
    if error_msg and "timed out" not in error_msg.lower():
        # Short-circuit for most errors
        return {
            "response": f"Error: {error_msg}",
            "result_summary": f"Error: {error_msg[:100]}"
        }

    system = (
        "You are a helpful data assistant. Summarize the query result in a clear, "
        "concise natural language response. Be specific with numbers and names.\n"
        "If the error message mentions \"timed out\", tell the user their query was too complex "
        "and suggest they break it into simpler queries or use .tables to check available data."
    )

    if error_msg:
        result_str = f"Error: {error_msg}"
    elif state.get("query_result") is not None:
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

    # Produce a compact summary for conversation history.
    # Keep it short (capped at 150 chars): just entity names and key numbers, not full rows.
    # This avoids bloating the LLM context in future turns.
    if state.get("query_result"):
        rows = state["query_result"]
        if rows:
            # Take first 3 rows, show key-value pairs
            sample = "; ".join(
                ", ".join(f"{k}={v}" for k, v in list(row.items())[:3])
                for row in rows[:3]
            )
            result_summary = f"{len(rows)} rows. Sample: {sample}"[:150]
        else:
            result_summary = "No results found."
    elif state.get("affected_rows", 0) > 0:
        result_summary = f"{state['affected_rows']} rows affected."
    elif state.get("error"):
        result_summary = f"Error: {state['error'][:100]}"
    else:
        result_summary = "No data."

    return {
        "response": response.strip(),
        "result_summary": result_summary
    }


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

def plan_analysis(state: AgentState, llm: LLMClient) -> dict:
    """Break a complex analytical question into 2-4 focused sub-queries.
    Limited to 4 sub-queries maximum to bound execution time.

    The planner uses the LLM to decompose the question into individual,
    answerable steps. Each step is a plain-language description that will
    be turned into SQL by the execute_analysis_step node.

    Returns a list of step descriptions in analysis_plan, and resets sub_results.
    """
    system = (
        "You are a data analysis planner. Break the user's complex question into "
        "2-4 focused sub-questions that can each be answered with a single SQL query.\n"
        "Output ONLY a numbered list, one sub-question per line, like:\n"
        "1. What is the total revenue this month?\n"
        "2. What was the total revenue last month?\n"
        "3. Which products had the highest sales increase?\n"
        "No explanation, no preamble — just the numbered list."
    )
    messages = [
        {
            "role": "user",
            "content": (
                f"Database schema:\n{state['ontology_context']}\n\n"
                f"Complex question: {state['user_query']}"
            ),
        }
    ]
    response = llm.chat(messages, system_prompt=system, temperature=0.0)

    # Parse numbered list into individual steps
    steps = []
    for line in response.strip().splitlines():
        line = line.strip()
        if line and line[0].isdigit():
            # Strip leading "1. " numbering
            step = re.sub(r"^\d+\.\s*", "", line).strip()
            if step:
                steps.append(step)

    # Cap at 4 steps to prevent runaway execution on overly complex plans.
    # LLMs sometimes generate 6-8 steps for broad questions; 4 is enough
    # for meaningful analysis without making the user wait too long.
    steps = steps[:4]

    # Fallback: if parsing failed, treat whole question as single step
    if not steps:
        steps = [state["user_query"]]

    return {"analysis_plan": steps, "sub_results": []}


def execute_analysis_step(state: AgentState, llm: LLMClient, executor: SQLExecutor) -> dict:
    """Execute the next pending sub-query in the analysis plan.

    Picks the first step not yet in sub_results, generates SQL for it,
    executes it, and appends the result to sub_results.

    Returns updated sub_results. When all steps are done, the graph routes
    to synthesize_results.
    
    Analysis sub-steps are always READ (analysis doesn't write data).
    """
    plan = state.get("analysis_plan", [])
    sub_results = list(state.get("sub_results", []))

    # Find next unexecuted step
    step_index = len(sub_results)
    if step_index >= len(plan):
        # All steps done — should not happen if graph routing is correct
        return {"sub_results": sub_results}

    step_query = plan[step_index]

    # Generate SQL for this sub-step
    sql_system = (
        "You are a SQL generator for SQLite. Given the schema and a specific sub-question, "
        "generate ONLY the SQL statement. No explanation, no markdown fences."
    )
    sql_messages = [
        {
            "role": "user",
            "content": (
                f"Database schema:\n{state['ontology_context']}\n\n"
                f"Sub-question: {step_query}"
            ),
        }
    ]
    sql_response = llm.chat(sql_messages, system_prompt=sql_system, temperature=0.0)

    # Clean up SQL
    sql = sql_response.strip()
    sql = re.sub(r"^```sql\s*", "", sql)
    sql = re.sub(r"^```\s*", "", sql)
    sql = re.sub(r"\s*```$", "", sql)
    sql = sql.strip()

    # Execute (sub-steps are always READ — analysis doesn't write data)
    exec_result = executor.execute(sql, approved=False)

    sub_results.append({
        "step": step_query,
        "sql": sql,
        "rows": exec_result.rows or [],
        "error": exec_result.error,
    })

    return {"sub_results": sub_results}


def synthesize_results(state: AgentState, llm: LLMClient) -> dict:
    """Synthesize all sub-query results into a coherent final answer.

    Collects all sub-results and asks the LLM to produce a unified,
    insightful response that directly answers the original complex question.
    """
    system = (
        "You are a data analyst. You have executed multiple queries to answer a complex question. "
        "Synthesize the results into a clear, insightful answer. "
        "Be specific with numbers. Point out key findings, comparisons, or trends."
    )

    # Format sub-results for the LLM
    sub_results_text = ""
    for i, sr in enumerate(state.get("sub_results", []), 1):
        sub_results_text += f"\nStep {i}: {sr['step']}\n"
        sub_results_text += f"SQL: {sr['sql']}\n"
        if sr.get("error"):
            sub_results_text += f"Error: {sr['error']}\n"
        elif sr.get("rows"):
            # Show up to 5 rows per sub-result to avoid exceeding the context window
            sub_results_text += f"Results ({len(sr['rows'])} rows): {str(sr['rows'][:5])}\n"
        else:
            sub_results_text += "No results.\n"

    messages = [
        {
            "role": "user",
            "content": (
                f"Original question: {state['user_query']}\n\n"
                f"Query results:\n{sub_results_text}"
            ),
        }
    ]
    response = llm.chat(messages, system_prompt=system, temperature=0.0)

    # Build a compact result_summary for conversation memory so follow-up
    # questions can reference what was analyzed (e.g. "which of those grew most?")
    step_summaries = []
    for sr in state.get("sub_results", []):
        if sr.get("rows"):
            first_row = str(list(sr["rows"][0].values())[:3]) if sr["rows"] else ""
            step_summaries.append(f"{sr['step'][:60]}: {first_row}")
    result_summary = "; ".join(step_summaries)[:150] if step_summaries else "Analysis complete."

    return {"response": response.strip(), "result_summary": result_summary}
