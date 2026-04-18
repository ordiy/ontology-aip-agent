from langgraph.graph import StateGraph, END
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
)
from src.llm.base import LLMClient
from src.database.executor import SQLExecutor


def _route_after_intent(state: AgentState) -> str:
    """Route after intent classification.

    READ/WRITE → generate_sql (single query path)
    ANALYZE → plan_analysis (multi-step path)
    UNCLEAR → clarify or give_up
    """
    intent = state.get("intent", "UNCLEAR")
    if intent in ("READ", "WRITE"):
        return "generate_sql"
    elif intent == "ANALYZE":
        return "plan_analysis"
    else:
        if state.get("clarify_count", 0) >= 2:
            return "give_up"
        return "clarify"


def _route_after_execute(state: AgentState) -> str:
    """Route after SQL execution.

    - needs_approval: write operation waiting for user confirmation
    - retry_sql: SQL failed on first attempt, retry generation with error feedback
    - format_result: success or final failure (after retry)
    """
    if state.get("sql_error_message"):
        return "retry_sql"

    if state.get("approved") is None and state.get("error") is None and state.get("query_result") is None:
        # Needs approval — handled externally by CLI
        return "needs_approval"

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


def build_graph(llm: LLMClient, executor: SQLExecutor, ontology_context: str):
    graph = StateGraph(AgentState)

    # Existing nodes
    graph.add_node("load_context", lambda state: load_ontology_context(state, ontology_context))
    graph.add_node("classify_intent", lambda state: classify_intent(state, llm))
    graph.add_node("generate_sql", lambda state: generate_sql(state, llm))
    graph.add_node("execute_sql", lambda state: execute_sql_node(state, executor))
    graph.add_node("format_result", lambda state: format_result(state, llm))
    graph.add_node("clarify", lambda state: clarify_question(state, llm))
    graph.add_node("give_up", lambda state: {"response": "I'm unable to understand your request after multiple attempts. Please try rephrasing, or use .tables to see available data."})
    graph.add_node("retry_sql", lambda state: generate_sql(state, llm))

    # New ANALYZE nodes
    graph.add_node("plan_analysis", lambda state: plan_analysis(state, llm))
    graph.add_node("execute_analysis_step", lambda state: execute_analysis_step(state, llm, executor))
    graph.add_node("synthesize_results", lambda state: synthesize_results(state, llm))

    # Existing edges
    graph.set_entry_point("load_context")
    graph.add_edge("load_context", "classify_intent")
    graph.add_conditional_edges("classify_intent", _route_after_intent, {
        "generate_sql": "generate_sql",
        "plan_analysis": "plan_analysis",
        "clarify": "clarify",
        "give_up": "give_up",
    })
    graph.add_edge("generate_sql", "execute_sql")
    graph.add_conditional_edges("execute_sql", _route_after_execute, {
        "format_result": "format_result",
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
