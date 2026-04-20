"""Node functions for DECIDE and OPERATE intent patterns (Pattern D).

Handles business-rule-driven decision making and multi-step orchestration:
    extract_user_overrides → (DECIDE) generate_sql → apply_decision
                           → present_decision → [plan_operation]
    extract_user_overrides → (OPERATE) plan_operation → execute_op_step (loop)
                           → synthesize_results / rollback_operations
"""
from __future__ import annotations

import json
import logging
import re

from src.agent.state import AgentState
from src.database.executor import BaseExecutor
from src.llm.base import LLMClient

logger = logging.getLogger(__name__)

_OVERRIDE_DEFAULTS: dict = {
    "skip_approval": False,
    "skip_steps": [],
    "override_rules": [],
    "reason": "",
}

_DECISION_ERROR_DEFAULT: dict = {
    "decision": "error",
    "affected_entities": [],
    "excluded_entities": [],
    "reasoning": "Failed to parse decision JSON",
    "requires_approval": True,
    "confidence": 0.0,
}


def _extract_json_object(text: str) -> dict | None:
    """Extract the first JSON object from *text*, returning ``None`` on failure.

    Args:
        text: Raw LLM response that should contain a JSON object.

    Returns:
        Parsed dictionary, or ``None`` if extraction / parsing failed.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError as exc:
        logger.warning("JSON parse error: %s", exc)
        return None


def _extract_json_list(text: str) -> list | None:
    """Extract the first JSON list from *text*, returning ``None`` on failure.

    Args:
        text: Raw LLM response that should contain a JSON array.

    Returns:
        Parsed list, or ``None`` if extraction / parsing failed.
    """
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError as exc:
        logger.warning("JSON list parse error: %s", exc)
        return None


def extract_user_overrides(state: AgentState, llm: LLMClient) -> dict:
    """Extract rule-override intentions from the user prompt (Pattern D).

    Parses the user query for signals such as "skip approval", "skip step X",
    or "override rule Y", and returns them as a structured dictionary.

    Args:
        state: Agent state containing ``rdf_rules`` and ``user_query``.
        llm: LLM client for override extraction.

    Returns:
        Partial state update with ``user_overrides`` dict.
    """
    system = (
        "Extract rule override intentions from the user prompt. "
        "Output ONLY a JSON object with these fields:\n"
        "{\n"
        '  "skip_approval": bool,      -- whether to skip manual confirmation\n'
        '  "skip_steps": list[str],    -- names of steps to skip (if any)\n'
        '  "override_rules": list[str],-- specific rules to ignore or modify\n'
        '  "reason": str               -- user provided reason (if any)\n'
        "}"
    )

    rules_context = ""
    for entity, rule in state.get("rdf_rules", {}).items():
        rules_context += (
            f"Entity: {entity}\n"
            f"Rule: {rule.decision_rule}\n"
            f"Steps: {rule.operation_steps}\n"
            f"Requires Approval: {rule.requires_approval}\n\n"
        )

    messages = [
        {
            "role": "user",
            "content": (
                f"Business Rules:\n{rules_context}\n\n"
                f"User query: {state['user_query']}"
            ),
        },
    ]

    response = llm.chat(messages, system_prompt=system, temperature=0.0)
    overrides = _extract_json_object(response)
    if overrides is not None:
        return {"user_overrides": overrides}

    logger.warning(
        "extract_user_overrides: failed to parse LLM response, using defaults."
    )
    return {"user_overrides": dict(_OVERRIDE_DEFAULTS)}


def apply_decision(state: AgentState, llm: LLMClient) -> dict:
    """Generate a structured decision recommendation based on rules, overrides, and data.

    Args:
        state: Agent state containing ``rdf_rules``, ``user_overrides``,
            ``query_result``, and ``user_query``.
        llm: LLM client for decision generation.

    Returns:
        Partial state update with ``decision`` dict.
    """
    system = (
        "You are a business decision assistant. Based on RDF rules, user overrides, "
        "and query data, generate a structured decision recommendation.\n"
        "Output ONLY a JSON object:\n"
        "{\n"
        '  "decision": "recommend_cancel | hold | partial | ...",\n'
        '  "affected_entities": [ID list],\n'
        '  "excluded_entities": [{"id": ..., "reason": "..."}],\n'
        '  "reasoning": "explanation citing rules",\n'
        '  "requires_approval": bool,\n'
        '  "confidence": float\n'
        "}"
    )

    rules = state.get("rdf_rules", {})
    overrides = state.get("user_overrides", {})
    data = state.get("query_result", [])

    rules_str = "\n".join(
        [f"- {entity}: {rule.decision_rule}" for entity, rule in rules.items()]
    )

    messages = [
        {
            "role": "user",
            "content": (
                f"Rules:\n{rules_str}\n\n"
                f"Overrides:\n{overrides}\n\n"
                f"Data:\n{str(data[:30])}\n\n"
                f"Original query: {state['user_query']}"
            ),
        },
    ]

    response = llm.chat(messages, system_prompt=system, temperature=0.0)
    decision = _extract_json_object(response)
    if decision is not None:
        # Global override logic: honour skip_approval if specified by the user.
        if overrides.get("skip_approval"):
            decision["requires_approval"] = False
        return {"decision": decision}

    logger.warning(
        "apply_decision: failed to parse LLM response, returning error decision."
    )
    return {"decision": dict(_DECISION_ERROR_DEFAULT)}


def present_decision(state: AgentState) -> dict:
    """Format the decision recommendation for user presentation.

    Args:
        state: Agent state containing ``decision``.

    Returns:
        Partial state update with a human-readable ``response`` string.
    """
    decision = state.get("decision", {})
    affected = decision.get("affected_entities", [])
    excluded = decision.get("excluded_entities", [])

    lines: list[str] = [
        f"📊 Decision: {decision.get('decision', 'N/A').upper()}",
        f"Reasoning: {decision.get('reasoning', '')}",
        "",
    ]
    if affected:
        suffix = "..." if len(affected) > 10 else ""
        lines.append(
            f"✅ Affected: {', '.join(map(str, affected[:10]))}{suffix}"
        )
    if excluded:
        first = excluded[0]
        lines.append(
            f"❌ Excluded: {len(excluded)} entities "
            f"(e.g. {first['id']} - {first['reason']})"
        )

    return {"response": "\n".join(lines)}


def plan_operation(state: AgentState, llm: LLMClient) -> dict:
    """Turn RDF operation steps into a concrete, idempotent SQL execution plan.

    Args:
        state: Agent state containing ``rdf_rules``, ``user_overrides``,
            ``decision``, ``ontology_context``, and ``user_query``.
        llm: LLM client for plan generation.

    Returns:
        Partial state update with ``operation_plan``, ``current_op_index``,
        ``operation_results``, and ``rollback_stack`` reset to initial values.
    """
    system = (
        "You are an operation planner. Turn RDF operation step scaffolding into a "
        "concrete SQL plan. Each step MUST include a SQL statement and an idempotent "
        "rollback SQL (if applicable).\n"
        "Output ONLY a JSON list of step objects:\n"
        "[\n"
        "  {\n"
        '    "step_name": str,\n'
        '    "description": str,\n'
        '    "sql": str,\n'
        '    "skipped": bool,\n'
        '    "skip_reason": str,\n'
        '    "rollback_sql": str\n'
        "  }\n"
        "]"
    )

    rules = state.get("rdf_rules", {})
    overrides = state.get("user_overrides", {})
    decision = state.get("decision", {})

    rdf_steps: list = []
    for rule in rules.values():
        if rule.entity.lower() in state["user_query"].lower():
            rdf_steps = rule.operation_steps
            break

    messages = [
        {
            "role": "user",
            "content": (
                f"Schema:\n{state['ontology_context']}\n\n"
                f"RDF Scaffold: {rdf_steps}\n\n"
                f"Target IDs: {decision.get('affected_entities', [])}\n\n"
                f"User Skip Steps: {overrides.get('skip_steps', [])}\n\n"
                f"Original query: {state['user_query']}"
            ),
        },
    ]

    response = llm.chat(messages, system_prompt=system, temperature=0.0)
    plan = _extract_json_list(response)
    if plan is not None:
        return {
            "operation_plan": plan,
            "current_op_index": 0,
            "operation_results": [],
            "rollback_stack": [],
        }

    logger.warning("plan_operation: failed to parse operation plan from LLM response.")
    return {"operation_plan": [], "error": "Failed to generate operation plan"}


def execute_operation_step(state: AgentState, executor: BaseExecutor) -> dict:
    """Execute a single step of the operation plan and manage the rollback stack.

    Args:
        state: Agent state containing ``operation_plan``, ``current_op_index``,
            ``operation_results``, and ``rollback_stack``.
        executor: Database executor for running the step SQL.

    Returns:
        Partial state update with updated ``operation_results``,
        ``rollback_stack``, and ``current_op_index``.
    """
    plan: list[dict] = state.get("operation_plan", [])
    idx: int = state.get("current_op_index", 0)
    results: list[dict] = list(state.get("operation_results", []))
    rollback_stack: list[dict] = list(state.get("rollback_stack", []))

    if idx >= len(plan):
        return {}

    step = plan[idx]

    if step.get("skipped"):
        results.append(
            {
                "step": step["step_name"],
                "status": "skipped",
                "reason": step.get("skip_reason", ""),
            }
        )
        return {"operation_results": results, "current_op_index": idx + 1}

    try:
        # Approval is assumed to have been obtained before the operation loop starts.
        result = executor.execute(step["sql"], approved=True)

        if result.error:
            return {"error": result.error, "current_op_index": idx}

        results.append(
            {
                "step": step["step_name"],
                "affected_rows": result.affected_rows,
                "status": "success",
            }
        )
        if step.get("rollback_sql"):
            rollback_stack.append(
                {
                    "step": step["step_name"],
                    "rollback_sql": step["rollback_sql"],
                }
            )
        return {
            "operation_results": results,
            "rollback_stack": rollback_stack,
            "current_op_index": idx + 1,
        }
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "execute_operation_step failed at step '%s': %s",
            step.get("step_name"),
            exc,
        )
        return {"error": str(exc), "current_op_index": idx}


def rollback_operations(state: AgentState, executor: BaseExecutor) -> dict:
    """Undo completed write steps in reverse order with detailed reporting.

    Args:
        state: Agent state containing ``rollback_stack``, ``error``, and
            ``current_op_index``.
        executor: Database executor for running rollback SQL statements.

    Returns:
        Partial state update with ``response``, and ``operation_results`` /
        ``rollback_stack`` cleared.
    """
    rollback_stack: list[dict] = list(state.get("rollback_stack", []))
    error = state.get("error", "Unknown error")
    rollback_log: list[str] = []

    for entry in reversed(rollback_stack):
        try:
            executor.execute(entry["rollback_sql"], approved=True)
            rollback_log.append(f"✅ Rolled back {entry['step']}")
        except Exception as exc:  # noqa: BLE001
            rollback_log.append(
                f"❌ Failed to rollback {entry['step']}: {exc}"
            )

    summary = (
        f"⚠️ Operation failed at step {state.get('current_op_index', '?')}: "
        f"{error}\n"
    )
    if rollback_log:
        summary += "\nRollback details:\n" + "\n".join(rollback_log)
    else:
        summary += "\nNo write operations to rollback."

    return {"response": summary, "operation_results": [], "rollback_stack": []}
