"""Policy engine abstractions and implementations.

``PolicyEngine`` decides whether a principal is allowed to run a SQL statement
against the referenced entities, and optionally rewrites the SQL to enforce
row-level security filters.
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

import sqlglot
import sqlglot.expressions as exp

from src.security.principal import Principal

if TYPE_CHECKING:
    from src.ontology.provider import OntologyContext

logger = logging.getLogger(__name__)

# Matches $principal.tenant_id, $principal.user_id, $principal.attrs.<key>
# Capture group 1: the dotted path after "$principal."
_PLACEHOLDER_RE = re.compile(r"\$principal\.([\w]+(?:\.[\w]+)*)")

# Only attribute key names matching this pattern are allowed in attrs lookups
_ATTR_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AuthOutcome(Enum):
    """Three-state authorisation result."""

    ALLOW = "allow"
    DENY = "deny"
    NEEDS_USER_APPROVAL = "needs_user_approval"


@dataclass
class AuthDecision:
    """Result of a policy evaluation.

    Attributes:
        outcome: The three-state result.
        reason: Human-readable explanation (always present; used in audit & user msgs).
        rewritten_sql: SQL with row-filters injected; ``None`` when no rewrite needed.
        masked_columns: Mapping from column name to mask method (``hash|redact|null``).
    """

    outcome: AuthOutcome
    reason: str
    rewritten_sql: str | None = None
    masked_columns: dict[str, str] = field(default_factory=dict)


class PolicyEngine(ABC):
    """Abstract policy decision point.

    Args:
        principal: The authenticated identity.
        sql: The raw SQL produced by the LLM (before any rewrite).
        referenced_entities: Physical table names parsed from the SQL.

    Returns:
        An ``AuthDecision`` describing the outcome and any SQL rewrite.
    """

    @abstractmethod
    def authorize(
        self,
        principal: Principal,
        sql: str,
        referenced_entities: list[str],
    ) -> AuthDecision:
        """Evaluate whether *principal* may execute *sql*.

        Args:
            principal: Authenticated identity.
            sql: Raw SQL to evaluate (before any row-filter rewrite).
            referenced_entities: Physical table names extracted from the SQL.

        Returns:
            An ``AuthDecision`` with outcome, optional rewritten SQL, and mask info.
        """
        ...


class NullPolicyEngine(PolicyEngine):
    """No-op policy engine — always allows every request without rewriting.

    Used as the default in OSS / test configurations so that existing tests
    require zero changes.
    """

    def authorize(
        self,
        principal: Principal,
        sql: str,
        referenced_entities: list[str],
    ) -> AuthDecision:
        """Always return ALLOW with no SQL rewrite.

        Args:
            principal: Ignored.
            sql: Ignored.
            referenced_entities: Ignored.

        Returns:
            ``AuthDecision(outcome=ALLOW, reason="null_policy_allow")`` with no rewrite.
        """
        return AuthDecision(
            outcome=AuthOutcome.ALLOW,
            reason="null_policy_allow",
            rewritten_sql=None,
            masked_columns={},
        )


# ---------------------------------------------------------------------------
# Placeholder resolution helpers
# ---------------------------------------------------------------------------


class _MissingPrincipalAttr(Exception):
    """Raised when a template references an attr the principal doesn't have."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def _resolve_placeholder(name: str, principal: Principal) -> exp.Expression:
    """Resolve a ``$principal.<name>`` placeholder to a sqlglot Literal.

    Supported paths:
    - ``tenant_id``
    - ``user_id``
    - ``attrs.<key>`` where key matches ``[A-Za-z_][A-Za-z0-9_]*``

    Args:
        name: Dotted path after ``$principal.`` (e.g. ``"tenant_id"`` or ``"attrs.region"``).
        principal: The authenticated identity whose fields are read.

    Returns:
        A ``sqlglot.exp.Literal`` (string type) with the resolved value.

    Raises:
        _MissingPrincipalAttr: When the referenced attribute is absent from the principal.
        ValueError: When the placeholder path is not whitelisted.
    """
    if name == "tenant_id":
        return exp.Literal.string(principal.tenant_id)
    if name == "user_id":
        return exp.Literal.string(principal.user_id)
    if name.startswith("attrs."):
        key = name[len("attrs."):]
        if not _ATTR_KEY_RE.match(key):
            raise ValueError(f"Invalid attrs key syntax: {key!r}")
        if key not in principal.attrs:
            raise _MissingPrincipalAttr(name)
        return exp.Literal.string(principal.attrs[key])
    raise ValueError(f"Unknown $principal placeholder: {name!r}")


def _build_filter_expression(template: str, principal: Principal) -> exp.Expression:
    """Parse ``template`` as a SQL WHERE clause body and inject principal values.

    Placeholders of the form ``$principal.<path>`` are replaced with properly
    quoted ``sqlglot.exp.Literal`` nodes — never via string concatenation.

    Args:
        template: Raw filter template, e.g. ``"tenant_id = $principal.tenant_id"``.
        principal: Source of placeholder values.

    Returns:
        A sqlglot expression tree representing the fully-resolved filter condition.

    Raises:
        _MissingPrincipalAttr: When a placeholder references an absent attr.
        sqlglot.errors.ParseError: When the template is not valid SQL.
    """
    # Map each placeholder to a unique safe token so we can parse the template
    # as valid SQL first, then substitute real Literal nodes.
    token_map: dict[str, str] = {}  # token_string -> placeholder name
    counter = [0]

    def _make_token(m: re.Match) -> str:
        idx = counter[0]
        counter[0] += 1
        token = f"__PHTOKEN{idx}__"
        token_map[token] = m.group(1)
        return f"'{token}'"

    safe_template = _PLACEHOLDER_RE.sub(_make_token, template)

    # Parse the safe template as a WHERE clause expression.
    filter_expr = sqlglot.parse_one(f"SELECT 1 WHERE {safe_template}").find(exp.Where)
    if filter_expr is None:
        raise ValueError(f"Could not parse row_filter_template as WHERE clause: {template!r}")
    condition = filter_expr.this

    # Walk the AST and replace token literals with proper principal Literals.
    def _replace_tokens(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Literal) and node.this in token_map:
            placeholder_name = token_map[node.this]
            return _resolve_placeholder(placeholder_name, principal)
        return node

    return condition.transform(_replace_tokens)


def _wrap_table_with_filter(
    table_node: exp.Table,
    filter_expr: exp.Expression,
) -> exp.Subquery:
    """Wrap a single ``exp.Table`` node in a filtered subquery.

    Produces:  ``(SELECT * FROM <table> WHERE <filter>) AS <alias>``

    The alias is the original table alias if present; otherwise the table name.

    Args:
        table_node: The AST node to wrap.
        filter_expr: The WHERE condition expression to inject.

    Returns:
        A ``exp.Subquery`` node that replaces the original ``exp.Table``.
    """
    original_alias = table_node.alias or table_node.name

    inner_select = exp.select("*").from_(
        exp.Table(this=exp.to_identifier(table_node.name))
    ).where(filter_expr)

    return exp.Subquery(
        this=inner_select,
        alias=exp.TableAlias(this=exp.to_identifier(original_alias)),
    )


class OntologyPolicyEngine(PolicyEngine):
    """RDF-driven policy engine that reads ``SecurityPolicy`` from ``OntologyContext``.

    Algorithm:
    1. Build a reverse lookup ``physical_table_name → SecurityPolicy``.
    2. For each referenced entity (physical table), check required roles.
       If any required role is missing → DENY.
    3. Parse the SQL with sqlglot.  For every ``exp.Table`` node whose name
       appears in the policy map, wrap the table in a filtered subquery.
    4. Resolve ``$principal.*`` placeholders as ``sqlglot.exp.Literal`` nodes.
    5. Aggregate ``masked_columns`` from all relevant policies.
    6. Return ``AuthDecision(ALLOW, rewritten_sql=..., masked_columns=...)``.

    Args:
        ontology: The ``OntologyContext`` whose ``physical_mappings`` carry
                  ``SecurityPolicy`` objects.
    """

    def __init__(self, ontology: "OntologyContext") -> None:
        self._ontology = ontology
        # Build reverse map: physical_table_name (lower) -> SecurityPolicy
        self._policy_map: dict[str, object] = self._build_policy_map(ontology)

    @staticmethod
    def _build_policy_map(ontology: "OntologyContext") -> dict[str, object]:
        """Build physical_table_name → SecurityPolicy reverse map.

        Args:
            ontology: Source of physical mappings.

        Returns:
            Dict mapping lowercase physical table name to its ``SecurityPolicy``.
        """
        result: dict[str, object] = {}
        for _entity_name, mapping in ontology.physical_mappings.items():
            if mapping.policy is not None:
                # Normalise to the bare table name (strip schema prefixes) and
                # also keep the full physical_table for exact matching.
                full = mapping.physical_table
                bare = full.rsplit(".", 1)[-1]  # last segment after final dot
                result[full.lower()] = mapping.policy
                result[bare.lower()] = mapping.policy
        return result

    def authorize(
        self,
        principal: Principal,
        sql: str,
        referenced_entities: list[str],
    ) -> AuthDecision:
        """Evaluate and optionally rewrite *sql* according to ontology policies.

        Args:
            principal: Authenticated identity.
            sql: Raw SQL statement to evaluate and possibly rewrite.
            referenced_entities: Physical table names parsed from the SQL.

        Returns:
            ``AuthDecision`` with ALLOW/DENY/NEEDS_USER_APPROVAL, optional
            rewritten SQL, and masked column map.
        """
        # ── 1. Collect applicable policies ──────────────────────────────────
        applicable: dict[str, object] = {}  # table_name (as in SQL) -> SecurityPolicy
        for table_name in referenced_entities:
            policy = self._policy_map.get(table_name.lower())
            if policy is not None:
                applicable[table_name] = policy

        # ── 2. Role checks ───────────────────────────────────────────────────
        for table_name, policy in applicable.items():
            missing = policy.required_roles - principal.roles  # type: ignore[attr-defined]
            if missing:
                return AuthDecision(
                    outcome=AuthOutcome.DENY,
                    reason=f"missing_roles:{','.join(sorted(missing))} for table {table_name!r}",
                )

        # ── 3. Aggregate masked_columns ──────────────────────────────────────
        aggregated_masks: dict[str, str] = {}
        for policy in applicable.values():
            aggregated_masks.update(policy.masked_columns)  # type: ignore[attr-defined]

        # ── 4. SQL rewrite (per-Table subquery wrapping) ─────────────────────
        # Only rewrite tables that have a row_filter_template.
        tables_needing_filter: dict[str, object] = {
            t: p
            for t, p in applicable.items()
            if p.row_filter_template is not None  # type: ignore[attr-defined]
        }

        if not tables_needing_filter:
            # No filters needed — return ALLOW without any SQL change.
            return AuthDecision(
                outcome=AuthOutcome.ALLOW,
                reason="ontology_allow",
                rewritten_sql=None,
                masked_columns=aggregated_masks,
            )

        # Parse SQL and rewrite using sqlglot tree transformation.
        try:
            tree = sqlglot.parse_one(sql)
        except Exception as exc:
            logger.warning("sqlglot parse failure during authorize rewrite: %s", exc)
            return AuthDecision(
                outcome=AuthOutcome.DENY,
                reason=f"sql_parse_error:{exc}",
            )

        # Build a lookup of normalised table name → filter expression.
        filter_exprs: dict[str, exp.Expression] = {}
        for table_name, policy in tables_needing_filter.items():
            try:
                filter_exprs[table_name.lower()] = _build_filter_expression(
                    policy.row_filter_template,  # type: ignore[attr-defined]
                    principal,
                )
            except _MissingPrincipalAttr as exc:
                return AuthDecision(
                    outcome=AuthOutcome.DENY,
                    reason=f"missing_principal_attr:{exc.name}",
                )
            except Exception as exc:
                return AuthDecision(
                    outcome=AuthOutcome.DENY,
                    reason=f"filter_build_error:{exc}",
                )

        def _transform_table(node: exp.Expression) -> exp.Expression:
            if not isinstance(node, exp.Table):
                return node
            # Match against both bare name and any qualified form.
            bare = node.name.lower()
            if bare in filter_exprs:
                return _wrap_table_with_filter(node, filter_exprs[bare])
            return node

        rewritten_tree = tree.transform(_transform_table)
        rewritten_sql = rewritten_tree.sql()

        return AuthDecision(
            outcome=AuthOutcome.ALLOW,
            reason="ontology_allow_with_rls",
            rewritten_sql=rewritten_sql,
            masked_columns=aggregated_masks,
        )
