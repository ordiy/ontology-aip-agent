from src.agent.state import AgentState
from src.agent.nodes import (
    load_ontology_context,
    classify_intent,
    generate_sql,
    execute_sql_node,
    format_result,
    clarify_question,
    plan_analysis,
    execute_analysis_step,
    synthesize_results,
    extract_user_overrides,
    apply_decision,
    present_decision,
    plan_operation,
    execute_operation_step,
    rollback_operations,
)
from src.llm.base import LLMClient
from src.database.executor import BaseExecutor
from src.ontology.provider import OntologyProvider


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


def build_graph(llm: LLMClient, executor: BaseExecutor, ontology: OntologyProvider):
    ctx = ontology.context
    
    # Capture dialect once so the generate_sql closure uses the right SQL syntax.
    # When a future StarRocksExecutor is plugged in, its dialect property will
    # automatically switch the LLM prompt to MySQL-compatible syntax.
    db_dialect = executor.dialect

    graph = StateGraph(AgentState)

    # Existing nodes
    graph.add_node("load_context", lambda state: {
        "ontology_context": ctx.schema_for_llm,
        "rdf_rules": ctx.rules,
    })
    graph.add_node("classify_intent", lambda state: classify_intent(state, llm))
    graph.add_node("generate_sql", lambda state: generate_sql(state, llm, db_dialect=db_dialect))
    graph.add_node("execute_sql", lambda state: execute_sql_node(state, executor))
    graph.add_node("format_result", lambda state: format_result(state, llm))
    graph.add_node("clarify", lambda state: clarify_question(state, llm))
    graph.add_node("give_up", lambda state: {"response": "I'm unable to understand your request after multiple attempts. Please try rephrasing, or use .tables to see available data."})
    graph.add_node("retry_sql", lambda state: generate_sql(state, llm, db_dialect=db_dialect))

    # New ANALYZE nodes
    graph.add_node("plan_analysis", lambda state: plan_analysis(state, llm))
    graph.add_node("execute_analysis_step", lambda state: execute_analysis_step(state, llm, executor))
    graph.add_node("synthesize_results", lambda state: synthesize_results(state, llm))

    # Pattern D (DECIDE / OPERATE) nodes
    graph.add_node("extract_user_overrides", lambda state: extract_user_overrides(state, llm))
    graph.add_node("apply_decision", lambda state: apply_decision(state, llm))
    graph.add_node("present_decision", present_decision)
    graph.add_node("plan_operation", lambda state: plan_operation(state, llm))
    graph.add_node("execute_op_step", lambda state: execute_operation_step(state, executor))
    graph.add_node("rollback", lambda state: rollback_operations(state, executor))

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

    graph.add_edge("generate_sql", "execute_sql")
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
