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
