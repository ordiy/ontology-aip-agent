"""Node functions for READ and WRITE intent patterns.

Handles the single-query lifecycle:
    load_ontology_context → classify_intent → generate_sql
    → execute_sql_node → format_result / clarify_question
"""
from __future__ import annotations

import logging

from src.agent.nodes._sql_utils import clean_sql, detect_permission_level
from src.agent.state import AgentState
from src.database.executor import BaseExecutor, PermissionDenied
from src.llm.base import LLMClient

logger = logging.getLogger(__name__)

_VALID_INTENTS = frozenset(
    {"READ", "WRITE", "ANALYZE", "DECIDE", "OPERATE", "UNCLEAR"}
)


def load_ontology_context(state: AgentState, context_text: str) -> dict:
    """Load ontology schema text into agent state.

    Args:
        state: Current agent state (unused but required by node signature).
        context_text: Formatted ontology schema for LLM consumption.

    Returns:
        Partial state update setting ``ontology_context``.
    """
    return {"ontology_context": context_text}


def classify_intent(state: AgentState, llm: LLMClient) -> dict:
    """Classify user intent as READ, WRITE, ANALYZE, DECIDE, OPERATE, or UNCLEAR.

    DECIDE: based on business rules to make recommendations.
    OPERATE: perform multi-step workflow operations.
    ANALYZE: for complex questions requiring multiple SQL queries.

    Args:
        state: Agent state containing ``user_query`` and ``ontology_context``.
        llm: LLM client used for classification.

    Returns:
        Partial state update with ``intent`` key.
    """
    system = (
        "You are an intent classifier for a data agent. "
        "Given a user query and database schema, classify the intent as exactly one of: "
        "READ, WRITE, ANALYZE, DECIDE, OPERATE, UNCLEAR.\n"
        "READ: simple queries that retrieve data with a single SQL (SELECT).\n"
        "WRITE: queries that modify data (INSERT, UPDATE, DELETE).\n"
        "ANALYZE: complex questions needing multiple queries — comparisons, trends, "
        "breakdowns, 'vs', 'compare', 'difference'.\n"
        "DECIDE: based on business rules (IF-THEN) make judgments or recommendations "
        "(e.g. 'which orders should be cancelled?', 'recommend VIP upgrade').\n"
        "OPERATE: perform orchestrated multi-step business operations "
        "(e.g. 'process all overdue orders', 'run month-end reconciliation').\n"
        "UNCLEAR: query is ambiguous or unrelated to the database.\n"
        "Respond with ONLY the single word: READ, WRITE, ANALYZE, DECIDE, OPERATE, or UNCLEAR."
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
    intent = response.strip().upper()
    if intent not in _VALID_INTENTS:
        logger.warning(
            "Unexpected intent value '%s' from LLM, defaulting to UNCLEAR.", intent
        )
        intent = "UNCLEAR"
    return {"intent": intent}


def generate_sql(
    state: AgentState,
    llm: LLMClient,
    db_dialect: str = "SQLite",
) -> dict:
    """Generate SQL from a natural language query using the LLM.

    Includes the last 3 conversation turns as context so the LLM can resolve
    references to previous queries (e.g. "show his orders" after asking about a
    customer).  If ``sql_error_message`` is set, this is a retry — the previous
    error is appended so the LLM can self-correct.

    Args:
        state: Agent state; must contain ``ontology_context``, ``user_query``,
            and ``intent``.
        llm: LLM client for SQL generation.
        db_dialect: SQL dialect name injected from the executor so the LLM
            generates compatible syntax (e.g. ``"SQLite"``,
            ``"MySQL (StarRocks-compatible)"``).

    Returns:
        Partial state update with ``generated_sql`` and ``permission_level``.
    """
    system = (
        f"You are a SQL generator for a {db_dialect} database. "
        "Given the database schema and a user query, generate ONLY the SQL statement. "
        "Do not include any explanation, markdown, or code fences. "
        "Output ONLY the raw SQL statement."
    )

    messages: list[dict] = []

    # Inject the last 3 conversation turns as context before the current query.
    # This lets the LLM resolve pronoun references (e.g. "his orders" → refers to
    # the customer found in the previous turn).
    # Truncated to 3 turns to maintain context-window efficiency.
    history = state.get("conversation_history", [])
    for turn in history[-3:]:
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Previous query: {turn['query']}\n"
                    f"SQL used: {turn['sql']}\n"
                    f"Result summary: {turn['result_summary']}"
                ),
            }
        )
        messages.append({"role": "model", "content": "Understood."})

    user_content = (
        f"Database schema:\n{state['ontology_context']}\n\n"
        f"User query: {state['user_query']}\n"
        f"Intent: {state['intent']}"
    )

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
    sql = clean_sql(response)
    permission_level = detect_permission_level(sql)

    return {"generated_sql": sql, "permission_level": permission_level}


def execute_sql_node(state: AgentState, executor: BaseExecutor) -> dict:
    """Execute the generated SQL statement.

    On SQL syntax/runtime errors (not permission denials), signals the graph
    to retry SQL generation with the error message as feedback.
    Only retries once (``sql_retry_count >= 1`` → propagate error to
    ``format_result``).

    Args:
        state: Agent state; must contain ``generated_sql``.
        executor: Database executor that runs the SQL.

    Returns:
        Partial state update with query results, error flags, or approval signal.
    """
    sql = state.get("generated_sql", "")
    approved = state.get("approved")
    permission_level = state.get("permission_level", "auto")
    retry_count = state.get("sql_retry_count", 0)

    try:
        result = executor.execute(
            sql, approved=(approved is True) or permission_level == "auto"
        )
    except PermissionDenied as exc:
        return {
            "error": str(exc),
            "query_result": None,
            "affected_rows": 0,
            "sql_error_message": None,
        }
    except Exception as exc:  # noqa: BLE001
        error_msg = str(exc)
        logger.warning("SQL execution raised an exception: %s", error_msg)
        if retry_count >= 1:
            return {"error": error_msg, "sql_error_message": None}
        return {
            "sql_error_message": error_msg,
            "sql_retry_count": retry_count + 1,
            "error": None,
        }

    if result.error:
        # If this is already a retry, propagate the error — do not retry again.
        if retry_count >= 1:
            return {"error": result.error, "sql_error_message": None}
        return {
            "sql_error_message": result.error,
            "sql_retry_count": retry_count + 1,
            "error": None,
        }

    if result.needs_approval:
        return {"approved": None}

    rows = result.rows or []
    return {
        "query_result": rows,
        "affected_rows": result.affected_rows,
        "error": None,
        "sql_error_message": None,
    }


def format_result(state: AgentState, llm: LLMClient) -> dict:
    """Format a query result as natural language and produce a compact history summary.

    The ``result_summary`` is a short string (≤ 150 chars) stored in
    ``conversation_history`` so future turns can reference what was found
    without bloating the LLM context.

    Args:
        state: Agent state containing ``query_result``, ``error``, and related keys.
        llm: LLM client for natural-language formatting.

    Returns:
        Partial state update with ``response`` and ``result_summary``.
    """
    error_msg = state.get("error")

    if error_msg and "timed out" not in error_msg.lower():
        return {
            "response": f"Error: {error_msg}",
            "result_summary": f"Error: {error_msg[:100]}",
        }

    system = (
        "You are a helpful data assistant. Summarize the query result in a clear, "
        "concise natural language response. Be specific with numbers and names.\n"
        "If the error message mentions \"timed out\", tell the user their query was "
        "too complex and suggest they break it into simpler queries or use .tables "
        "to check available data."
    )

    if error_msg:
        result_str = f"Error: {error_msg}"
    elif state.get("query_result") is not None:
        result_str = str(state["query_result"][:20])
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

    # Build a compact summary for conversation history (≤ 150 chars).
    if state.get("query_result"):
        rows = state["query_result"]
        if rows:
            sample = "; ".join(
                ", ".join(f"{k}={v}" for k, v in list(row.items())[:3])
                for row in rows[:3]
            )
            result_summary: str = f"{len(rows)} rows. Sample: {sample}"[:150]
        else:
            result_summary = "No results found."
    elif state.get("affected_rows", 0) > 0:
        result_summary = f"{state['affected_rows']} rows affected."
    elif state.get("error"):
        result_summary = f"Error: {state['error'][:100]}"
    else:
        result_summary = "No data."

    return {"response": response.strip(), "result_summary": result_summary}


def clarify_question(state: AgentState, llm: LLMClient) -> dict:
    """Ask the user a clarifying question when the intent is UNCLEAR.

    Args:
        state: Agent state containing ``ontology_context`` and ``user_query``.
        llm: LLM client for generating the clarifying question.

    Returns:
        Partial state update with ``response`` and incremented ``clarify_count``.
    """
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
