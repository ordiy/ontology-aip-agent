from typing import TypedDict


class AgentState(TypedDict, total=False):
    messages: list[dict]
    ontology_context: str
    user_query: str
    intent: str              # READ / WRITE / UNCLEAR
    generated_sql: str
    permission_level: str    # auto / confirm / deny
    approved: bool | None
    query_result: list[dict] | None
    affected_rows: int
    response: str
    clarify_count: int       # track clarification retries (max 2)
    error: str | None
    sql_retry_count: int     # Tracks how many SQL retries have happened (max 1)
    sql_error_message: str | None # Error message from failed SQL execution
    conversation_history: list[dict]  # Past turns: [{"query": str, "sql": str, "result_summary": str}, ...]
    result_summary: str      # Compact summary of the result for conversation history
