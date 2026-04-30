from langgraph.graph import END, StateGraph

from src.agent.nodes import (
    apply_decision,
    authorize_node,
    clarify_question,
    classify_intent,
    execute_analysis_step,
    execute_operation_step,
    execute_sql_node,
    extract_user_overrides,
    format_result,
    generate_sql,
    plan_analysis,
    plan_operation,
    present_decision,
    rollback_operations,
    synthesize_results,
)
from src.agent.state import AgentState
from src.database.executor import BaseExecutor
from src.federation.executor_registry import ExecutorRegistry
from src.federation.planner import QueryPlanner
from src.llm.base import LLMClient
from src.ontology.provider import OntologyProvider
from src.security.context import SecurityContext
from src.security.policy import AuthOutcome


def _route_after_intent(state: AgentState) -> str:
    """Route after intent classification.

    READ/WRITE → generate_sql (single query path)
    ANALYZE → plan_analysis (multi-step path)
    DECIDE → extract_user_overrides
    OPERATE → extract_user_overrides
    UNCLEAR → clarify or give_up
    """
    intent = state.get("intent", "UNCLEAR")
    if intent in ("READ", "WRITE"):
        return "generate_sql"
    elif intent == "ANALYZE":
        return "plan_analysis"
    elif intent in ("DECIDE", "OPERATE"):
        return "extract_user_overrides"
    else:
        if state.get("clarify_count", 0) >= 2:
            return "give_up"
        return "clarify"


def _route_after_execute(state: AgentState) -> str:
    """Route after SQL execution.

    - needs_approval: write operation waiting for user confirmation
    - retry_sql: SQL failed on first attempt, retry generation with error feedback
    - apply_decision: if intent is DECIDE, proceed to apply decision logic
    - format_result: success or final failure (after retry)
    """
    if state.get("sql_error_message"):
        return "retry_sql"

    if state.get("approved") is None and state.get("error") is None and state.get("query_result") is None:
        # Needs approval — handled externally by CLI
        return "needs_approval"
        
    if state.get("intent") == "DECIDE" and state.get("query_result") is not None:
        return "apply_decision"

    return "format_result"


def _route_after_analysis_step(state: AgentState) -> str:
    """Route after each analysis step execution.

    If there are more steps in the plan, execute the next one.
    When all steps are done, synthesize the results.
    """
    plan = state.get("analysis_plan", [])
    sub_results = state.get("sub_results", [])

    if len(sub_results) < len(plan):
        # More steps remaining
        return "execute_analysis_step"
    return "synthesize_results"


def _route_after_decision(state: AgentState) -> str:
    """Route after decision is presented."""
    decision = state.get("decision", {})
    # If requires_approval is False and we have affected entities, proceed to operation
    if not decision.get("requires_approval") and decision.get("affected_entities"):
        return "plan_operation"
    # Otherwise, wait for user confirmation (handled by CLI)
    return "end"


def _route_after_op_step(state: AgentState) -> str:
    """Route after each operation step execution."""
    if state.get("error"):
        return "rollback"
        
    plan = state.get("operation_plan", [])
    idx = state.get("current_op_index", 0)
    
    if idx < len(plan):
        return "execute_step"
    return "synthesize"


def _route_after_authorize(state: AgentState) -> str:
    """Route after authorisation evaluation.

    - ``execute_sql``: ALLOW outcome — continue with execution.
    - ``deny``: DENY outcome — emit audit and surface error to user.
    - ``needs_user_approval``: NEEDS_USER_APPROVAL — hand control back to CLI/Web.

    Args:
        state: Current agent state containing ``auth_decision``.

    Returns:
        One of ``"execute_sql"``, ``"deny"``, or ``"needs_user_approval"``.
    """
    auth_decision = state.get("auth_decision")
    if auth_decision is None:
        return "execute_sql"
    outcome = auth_decision.outcome
    if outcome == AuthOutcome.ALLOW:
        return "execute_sql"
    if outcome == AuthOutcome.DENY:
        return "deny"
    return "needs_user_approval"


def _finalize_deny(state: AgentState, security: SecurityContext) -> dict:
    """Emit audit for a denied request and produce a user-facing response.

    Args:
        state: Current agent state (must contain ``error`` and ``auth_decision``).
        security: Security context for audit emission.

    Returns:
        Partial state update setting ``response``.
    """
    reason = state.get("error", "access_denied")
    try:
        from datetime import datetime, timezone

        from src.security.audit import AuditEvent
        from src.security.policy import AuthDecision, AuthOutcome

        principal = state.get("principal")
        if principal is None:
            principal = security.principal_provider.get()

        auth_decision = state.get("auth_decision") or AuthDecision(
            outcome=AuthOutcome.DENY, reason=reason
        )
        event = AuditEvent(
            timestamp=datetime.now(tz=timezone.utc),
            principal=principal,
            intent=state.get("intent", "UNKNOWN"),
            sql_original=state.get("sql_original") or state.get("generated_sql"),
            sql_rewritten=None,
            referenced_entities=[],
            decision=auth_decision,
            row_count=None,
            error=reason,
            trace_id=None,
        )
        security.audit.emit(event)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("Failed to emit deny audit event: %s", exc)

    return {"response": f"Access denied: {reason}"}


def _finalize_pending(state: AgentState, security: SecurityContext) -> dict:
    """Emit audit for a request paused awaiting user approval.

    Records an ``AuditEvent`` with the NEEDS_USER_APPROVAL decision so the
    compliance trail captures which requests were paused.  No row data is
    included; ``row_count`` and ``error`` are always ``None``.

    Args:
        state: Current agent state (must contain ``auth_decision`` and
               optionally ``sql_original`` / ``generated_sql``).
        security: Security context for audit emission.

    Returns:
        Empty dict — this node has no state side-effects beyond the audit
        side-effect on the logger.
    """
    try:
        from datetime import datetime, timezone

        from src.security.audit import AuditEvent
        from src.security.policy import AuthDecision, AuthOutcome

        principal = state.get("principal")
        if principal is None:
            principal = security.principal_provider.get()

        auth_decision = state.get("auth_decision") or AuthDecision(
            outcome=AuthOutcome.NEEDS_USER_APPROVAL, reason="awaiting_approval"
        )
        event = AuditEvent(
            timestamp=datetime.now(tz=timezone.utc),
            principal=principal,
            intent=state.get("intent", "UNKNOWN"),
            sql_original=state.get("sql_original") or state.get("generated_sql"),
            sql_rewritten=None,
            referenced_entities=[],
            decision=auth_decision,
            row_count=None,
            error=None,
            trace_id=None,
        )
        security.audit.emit(event)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("Failed to emit pending audit event: %s", exc)

    return {}


def build_graph(
    llm: LLMClient,
    executors: dict[str, BaseExecutor] | BaseExecutor,
    ontology: OntologyProvider,
    default_engine: str = "sqlite",
    federation_config: dict | None = None,
    obs: object | None = None,
    security: SecurityContext | None = None,
):
    ctx = ontology.context

    # Default to no-op security when not provided — preserves backward compatibility.
    if security is None:
        security = SecurityContext.null()

    if isinstance(executors, BaseExecutor):
        executors = {default_engine: executors}

    registry = ExecutorRegistry(executors, default_engine=default_engine)
    join_row_limit = (federation_config or {}).get("join_row_limit")
    planner = QueryPlanner(
        ontology=ontology,
        registry=registry,
        join_row_limit=join_row_limit,
        obs=obs,
    )
    
    # Capture dialect once so the generate_sql closure uses the right SQL syntax.
    # When a future StarRocksExecutor is plugged in, its dialect property will
    # automatically switch the LLM prompt to MySQL-compatible syntax.
    default_executor = registry.default()
    db_dialect = default_executor.dialect

    graph = StateGraph(AgentState)

    # Existing nodes
    graph.add_node("load_context", lambda state: {
        "ontology_context": ctx.schema_for_llm,
        "rdf_rules": ctx.rules,
    })
    graph.add_node("classify_intent", lambda state: classify_intent(state, llm))
    graph.add_node("generate_sql", lambda state: generate_sql(state, llm, db_dialect=db_dialect))
    graph.add_node("authorize", lambda state: authorize_node(state, security))
    graph.add_node("execute_sql", lambda state: execute_sql_node(state, planner, security))
    graph.add_node("deny", lambda state: _finalize_deny(state, security))
    graph.add_node("needs_approval_audit", lambda state: _finalize_pending(state, security))
    graph.add_node("format_result", lambda state: format_result(state, llm))
    graph.add_node("clarify", lambda state: clarify_question(state, llm))
    graph.add_node("give_up", lambda state: {"response": "I'm unable to understand your request after multiple attempts. Please try rephrasing, or use .tables to see available data."})
    graph.add_node("retry_sql", lambda state: generate_sql(state, llm, db_dialect=db_dialect))

    # New ANALYZE nodes
    graph.add_node("plan_analysis", lambda state: plan_analysis(state, llm))
    graph.add_node("execute_analysis_step", lambda state: execute_analysis_step(state, llm, default_executor))
    graph.add_node("synthesize_results", lambda state: synthesize_results(state, llm))

    # Pattern D (DECIDE / OPERATE) nodes
    graph.add_node("extract_user_overrides", lambda state: extract_user_overrides(state, llm))
    graph.add_node("apply_decision", lambda state: apply_decision(state, llm))
    graph.add_node("present_decision", present_decision)
    graph.add_node("plan_operation", lambda state: plan_operation(state, llm))
    graph.add_node("execute_op_step", lambda state: execute_operation_step(state, default_executor))
    graph.add_node("rollback", lambda state: rollback_operations(state, default_executor))

    # Existing edges
    graph.set_entry_point("load_context")
    graph.add_edge("load_context", "classify_intent")
    graph.add_conditional_edges("classify_intent", _route_after_intent, {
        "generate_sql": "generate_sql",
        "plan_analysis": "plan_analysis",
        "extract_user_overrides": "extract_user_overrides",
        "clarify": "clarify",
        "give_up": "give_up",
    })
    
    # Pattern D Edges
    graph.add_conditional_edges("extract_user_overrides", lambda s: s["intent"], {
        "DECIDE": "generate_sql", # Need data first
        "OPERATE": "plan_operation"
    })
    
    # DECIDE flow
    # generate_sql -> execute_sql (reusing existing nodes)
    # Then execute_sql needs to route to apply_decision if intent is DECIDE
    
    graph.add_edge("apply_decision", "present_decision")
    graph.add_conditional_edges("present_decision", _route_after_decision, {
        "plan_operation": "plan_operation",
        "end": END
    })
    
    # OPERATE flow
    graph.add_edge("plan_operation", "execute_op_step")
    graph.add_conditional_edges("execute_op_step", _route_after_op_step, {
        "execute_step": "execute_op_step",
        "rollback": "rollback",
        "synthesize": "synthesize_results" # Reuse synthesizer
    })
    graph.add_edge("rollback", END)

    graph.add_edge("generate_sql", "authorize")
    graph.add_conditional_edges("authorize", _route_after_authorize, {
        "execute_sql": "execute_sql",
        "deny": "deny",
        "needs_user_approval": "needs_approval_audit",
    })
    graph.add_edge("deny", END)
    graph.add_edge("needs_approval_audit", END)
    graph.add_conditional_edges("execute_sql", _route_after_execute, {
        "format_result": "format_result",
        "apply_decision": "apply_decision",
        "needs_approval": END,
        "retry_sql": "retry_sql",
    })
    graph.add_edge("format_result", END)
    graph.add_edge("clarify", END)
    graph.add_edge("give_up", END)
    graph.add_edge("retry_sql", "execute_sql")

    # New ANALYZE edges
    graph.add_edge("plan_analysis", "execute_analysis_step")
    graph.add_conditional_edges("execute_analysis_step", _route_after_analysis_step, {
        "execute_analysis_step": "execute_analysis_step",
        "synthesize_results": "synthesize_results",
    })
    graph.add_edge("synthesize_results", END)

    return graph.compile()
