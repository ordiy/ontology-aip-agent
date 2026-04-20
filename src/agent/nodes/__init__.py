"""Public API of the ``src.agent.nodes`` package.

Re-exports every node function so that existing ``from src.agent.nodes import …``
statements continue to work without modification after the package refactor.
"""
from src.agent.nodes.analyze import (
    execute_analysis_step,
    plan_analysis,
    synthesize_results,
)
from src.agent.nodes.decide_operate import (
    apply_decision,
    execute_operation_step,
    extract_user_overrides,
    plan_operation,
    present_decision,
    rollback_operations,
)
from src.agent.nodes.read_write import (
    clarify_question,
    execute_sql_node,
    format_result,
    generate_sql,
    classify_intent,
    load_ontology_context,
)

__all__ = [
    # read_write
    "load_ontology_context",
    "classify_intent",
    "generate_sql",
    "execute_sql_node",
    "format_result",
    "clarify_question",
    # analyze
    "plan_analysis",
    "execute_analysis_step",
    "synthesize_results",
    # decide_operate
    "extract_user_overrides",
    "apply_decision",
    "present_decision",
    "plan_operation",
    "execute_operation_step",
    "rollback_operations",
]
