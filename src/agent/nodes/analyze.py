"""Node functions for the ANALYZE intent pattern.

Handles multi-step analytical queries:
    plan_analysis → execute_analysis_step (loop) → synthesize_results
"""
from __future__ import annotations

import logging
import re

from src.agent.nodes._sql_utils import clean_sql
from src.agent.state import AgentState
from src.database.executor import BaseExecutor
from src.llm.base import LLMClient

logger = logging.getLogger(__name__)

# Maximum number of sub-steps allowed in one analysis plan.
# LLMs sometimes generate 6-8 steps for broad questions; 4 provides
# meaningful analysis without excessive wait times.
_MAX_ANALYSIS_STEPS = 4


def plan_analysis(state: AgentState, llm: LLMClient) -> dict:
    """Break a complex analytical question into focused sub-queries.

    Uses the LLM to decompose the question into 2–4 individual steps, each
    answerable with a single SQL query.  Capped at ``_MAX_ANALYSIS_STEPS``
    to bound execution time.

    Args:
        state: Agent state containing ``ontology_context`` and ``user_query``.
        llm: LLM client for query decomposition.

    Returns:
        Partial state update with ``analysis_plan`` (list of step descriptions)
        and ``sub_results`` reset to an empty list.
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

    steps: list[str] = []
    for line in response.strip().splitlines():
        line = line.strip()
        if line and line[0].isdigit():
            step = re.sub(r"^\d+\.\s*", "", line).strip()
            if step:
                steps.append(step)

    steps = steps[:_MAX_ANALYSIS_STEPS]

    # Fallback: if parsing failed, treat the whole question as a single step.
    if not steps:
        logger.warning(
            "plan_analysis failed to parse LLM output into steps; "
            "falling back to single-step plan."
        )
        steps = [state["user_query"]]

    return {"analysis_plan": steps, "sub_results": []}


def execute_analysis_step(
    state: AgentState, llm: LLMClient, executor: BaseExecutor
) -> dict:
    """Execute the next pending sub-query in the analysis plan.

    Picks the first step not yet represented in ``sub_results``, generates
    SQL for it, executes it, and appends the result.  The graph routes back
    here until all steps are complete, then moves to ``synthesize_results``.

    Analysis sub-steps are always treated as READ operations — analysis never
    writes data.

    Args:
        state: Agent state containing ``analysis_plan`` and ``sub_results``.
        llm: LLM client for per-step SQL generation.
        executor: Database executor for running each sub-query.

    Returns:
        Partial state update with the appended ``sub_results``.
    """
    plan = state.get("analysis_plan", [])
    sub_results: list[dict] = list(state.get("sub_results", []))

    step_index = len(sub_results)
    if step_index >= len(plan):
        # All steps done — graph routing should prevent this, but guard anyway.
        logger.warning(
            "execute_analysis_step called with no remaining steps; "
            "step_index=%d plan_len=%d",
            step_index,
            len(plan),
        )
        return {"sub_results": sub_results}

    step_query = plan[step_index]

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
    sql = clean_sql(sql_response)

    # Sub-steps are always READ — analysis doesn't write data.
    exec_result = executor.execute(sql, approved=False)

    sub_results.append(
        {
            "step": step_query,
            "sql": sql,
            "rows": exec_result.rows or [],
            "error": exec_result.error,
        }
    )
    return {"sub_results": sub_results}


def synthesize_results(state: AgentState, llm: LLMClient) -> dict:
    """Synthesize all sub-query results into a coherent final answer.

    Collects all ``sub_results`` and asks the LLM to produce a unified,
    insightful response that directly answers the original complex question.

    Args:
        state: Agent state containing ``sub_results`` and ``user_query``.
        llm: LLM client for synthesis.

    Returns:
        Partial state update with ``response`` and ``result_summary``.
    """
    system = (
        "You are a data analyst. You have executed multiple queries to answer a complex "
        "question. Synthesize the results into a clear, insightful answer. "
        "Be specific with numbers. Point out key findings, comparisons, or trends."
    )

    sub_results: list[dict] = state.get("sub_results", [])
    sub_results_text = ""
    for i, sr in enumerate(sub_results, 1):
        sub_results_text += f"\nStep {i}: {sr['step']}\n"
        sub_results_text += f"SQL: {sr['sql']}\n"
        if sr.get("error"):
            sub_results_text += f"Error: {sr['error']}\n"
        elif sr.get("rows"):
            # Show up to 5 rows per sub-result to avoid exceeding the context window.
            sub_results_text += (
                f"Results ({len(sr['rows'])} rows): {str(sr['rows'][:5])}\n"
            )
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
    # questions can reference what was analyzed.
    step_summaries: list[str] = []
    for sr in sub_results:
        if sr.get("rows"):
            first_row = (
                str(list(sr["rows"][0].values())[:3]) if sr["rows"] else ""
            )
            step_summaries.append(f"{sr['step'][:60]}: {first_row}")
    result_summary = (
        "; ".join(step_summaries)[:150] if step_summaries else "Analysis complete."
    )

    return {"response": response.strip(), "result_summary": result_summary}
