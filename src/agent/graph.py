from langgraph.graph import StateGraph, END
from src.agent.state import AgentState
from src.agent.nodes import (
    load_ontology_context,
    classify_intent,
    generate_sql,
    execute_sql_node,
    format_result,
    clarify_question,
)
from src.llm.base import LLMClient
from src.database.executor import SQLExecutor


def _route_after_intent(state: AgentState) -> str:
    intent = state.get("intent", "UNCLEAR")
    if intent in ("READ", "WRITE"):
        return "generate_sql"
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


def build_graph(llm: LLMClient, executor: SQLExecutor, ontology_context: str):
    graph = StateGraph(AgentState)

    # Node wrappers that close over dependencies
    graph.add_node("load_context", lambda state: load_ontology_context(state, ontology_context))
    graph.add_node("classify_intent", lambda state: classify_intent(state, llm))
    graph.add_node("generate_sql", lambda state: generate_sql(state, llm))
    graph.add_node("retry_sql", lambda state: generate_sql(state, llm))
    graph.add_node("execute_sql", lambda state: execute_sql_node(state, executor))
    graph.add_node("format_result", lambda state: format_result(state, llm))
    graph.add_node("clarify", lambda state: clarify_question(state, llm))
    graph.add_node("give_up", lambda state: {"response": "I'm unable to understand your request after multiple attempts. Please try rephrasing, or use .tables to see available data."})

    # Edges
    graph.set_entry_point("load_context")
    graph.add_edge("load_context", "classify_intent")
    graph.add_conditional_edges("classify_intent", _route_after_intent, {
        "generate_sql": "generate_sql",
        "clarify": "clarify",
        "give_up": "give_up",
    })
    graph.add_edge("generate_sql", "execute_sql")
    graph.add_edge("retry_sql", "execute_sql")
    graph.add_conditional_edges("execute_sql", _route_after_execute, {
        "format_result": "format_result",
        "needs_approval": END,
        "retry_sql": "retry_sql",
    })
    graph.add_edge("format_result", END)
    graph.add_edge("clarify", END)
    graph.add_edge("give_up", END)

    return graph.compile()
