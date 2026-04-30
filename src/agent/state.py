from __future__ import annotations

from typing import Any, TypedDict

# Imported directly (not under TYPE_CHECKING) so LangGraph's get_type_hints()
# can resolve them when building the StateGraph channel map.
from src.security.policy import AuthDecision
from src.security.principal import Principal


class AgentState(TypedDict, total=False):
    messages: list[dict]
    ontology_context: str
    user_query: str
    intent: str              # READ / WRITE / ANALYZE / UNCLEAR / DECIDE / OPERATE
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
    analysis_plan: list[str]       # List of sub-query descriptions from planner
    sub_results: list[dict]        # Results from each sub-query execution
    conversation_history: list[dict]  # Past turns: [{"query": str, "sql": str, "result_summary": str}, ...]
    result_summary: str      # Compact summary of the result for conversation history

    # Pattern D: DECIDE / OPERATE fields
    rdf_rules: dict          # entity_name → EntityRule (from ontology aip: annotations)
    user_overrides: dict     # {skip_approval, skip_steps, override_rules, reason}
    decision: dict           # {decision, affected_entities, excluded_entities, reasoning, requires_approval, confidence}
    operation_plan: list[dict]    # [{step_name, description, sql, skipped, skip_reason, rollback_sql}]
    operation_results: list[dict] # per-step execution results
    rollback_stack: list[dict]    # [{step_name, rollback_sql}] for completed write steps
    current_op_index: int         # index into operation_plan for step-by-step execution

    # Security / RBAC fields (Phase A)
    principal: Any           # Principal | None — resolved by PrincipalProvider at entry; read-only downstream
    auth_decision: Any       # AuthDecision | None — written by authorize_node; read by audit/format nodes
    masked_columns: dict[str, str]       # column → mask method (hash|redact|null); consumed by execute_sql_node
    sql_original: str | None             # SQL before row-filter rewrite; used for audit records
