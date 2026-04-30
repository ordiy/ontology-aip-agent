"""Authorization node for the LangGraph pipeline.

The ``authorize_node`` sits between ``generate_sql`` and ``execute_sql``.
It resolves the current principal, evaluates the SQL against the policy engine,
and either rewrites the SQL (ALLOW), blocks execution (DENY), or signals that
user approval is required (NEEDS_USER_APPROVAL).
"""
from __future__ import annotations

import logging

import sqlglot
import sqlglot.expressions as exp

from src.agent.state import AgentState
from src.security.context import SecurityContext
from src.security.policy import AuthOutcome

logger = logging.getLogger(__name__)


def _parse_referenced_entities(sql: str) -> list[str]:
    """Extract all physical table names referenced in a SQL statement.

    Uses sqlglot to parse the SQL and collect every ``exp.Table`` node.
    Returns an empty list on parse failure (policy engine then has nothing
    to check, which is safe — NullPolicyEngine always allows).

    Args:
        sql: Raw SQL statement to parse.

    Returns:
        List of table names found in the SQL (may contain duplicates).
    """
    if not sql:
        return []
    try:
        tree = sqlglot.parse_one(sql)
        return [table.name for table in tree.find_all(exp.Table) if table.name]
    except Exception as exc:  # noqa: BLE001
        logger.debug("sqlglot parse failure in _parse_referenced_entities: %s", exc)
        return []


def authorize_node(state: AgentState, security: SecurityContext) -> dict:
    """Evaluate the generated SQL against the policy engine and rewrite if needed.

    Steps:
    1. Read ``generated_sql`` from state.
    2. Resolve (or inherit) the current ``Principal`` from state / provider.
    3. Parse referenced entities (physical table names) from the SQL.
    4. Call ``security.policy.authorize(principal, sql, entities)``.
    5. On ALLOW: optionally replace ``generated_sql`` with the rewritten SQL.
    6. On DENY: write ``error`` so the graph routes to the deny path.
    7. On NEEDS_USER_APPROVAL: leave state as-is; routing ends at END.

    Args:
        state: Current ``AgentState``; must contain ``generated_sql``.
        security: Security context carrying principal provider, policy, and audit.

    Returns:
        Partial state update dict.
    """
    sql = state.get("generated_sql", "")
    entities = _parse_referenced_entities(sql)

    principal = state.get("principal") or security.principal_provider.get()
    decision = security.policy.authorize(principal, sql, entities)

    out: dict = {
        "principal": principal,
        "auth_decision": decision,
        "sql_original": sql,
        "masked_columns": decision.masked_columns,
    }

    if decision.outcome == AuthOutcome.ALLOW:
        if decision.rewritten_sql:
            out["generated_sql"] = decision.rewritten_sql
    elif decision.outcome == AuthOutcome.DENY:
        out["error"] = decision.reason

    return out
