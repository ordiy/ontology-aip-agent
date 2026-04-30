"""Comprehensive tests for src/security (Phase A).

Covers:
- Principal & providers
- NullPolicyEngine / NullAuditLogger
- OntologyPolicyEngine role checks
- OntologyPolicyEngine row-filter SQL rewriting (UNION, subquery, IN, JOIN)
- Placeholder safety (SQL injection, unknown attr)
- Masked columns post-processing
- JsonlAuditLogger serialisation
- authorize_node integration
- Three-state routing
"""
from __future__ import annotations

import hashlib
import json
import os
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.security.audit import AuditEvent, JsonlAuditLogger, NullAuditLogger, _event_to_dict
from src.security.context import SecurityContext
from src.security.policy import (
    AuthDecision,
    AuthOutcome,
    NullPolicyEngine,
    OntologyPolicyEngine,
)
from src.security.principal import EnvPrincipalProvider, Principal


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_principal(
    tenant_id: str = "tenant_a",
    user_id: str = "alice",
    roles: frozenset[str] | None = None,
    attrs: dict[str, str] | None = None,
    session_id: str = "sess_001",
) -> Principal:
    return Principal(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=roles if roles is not None else frozenset(),
        attrs=attrs or {},
        session_id=session_id,
    )


def _make_ontology_ctx(
    entity: str = "Order",
    physical_table: str = "orders",
    required_roles: frozenset[str] | None = None,
    row_filter: str | None = None,
    masked_columns: dict[str, str] | None = None,
) -> object:
    """Build a minimal OntologyContext stub with a single entity that has a security policy."""
    from src.ontology.provider import OntologyContext, PhysicalMapping, SecurityPolicy

    policy = None
    if required_roles or row_filter or masked_columns:
        policy = SecurityPolicy(
            required_roles=required_roles or frozenset(),
            row_filter_template=row_filter,
            masked_columns=masked_columns or {},
        )
    mappings = {entity: PhysicalMapping(physical_table=physical_table, policy=policy)}
    return OntologyContext(
        schema_for_llm="",
        rules={},
        physical_mappings=mappings,
    )


def _make_auth_decision(outcome: AuthOutcome = AuthOutcome.ALLOW) -> AuthDecision:
    return AuthDecision(outcome=outcome, reason="test", rewritten_sql=None, masked_columns={})


# ─────────────────────────────────────────────────────────────────────────────
# Principal & providers
# ─────────────────────────────────────────────────────────────────────────────


class TestEnvPrincipalProvider:
    def test_reads_default_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EnvPrincipalProvider reads DEFAULT_TENANT_ID from env."""
        monkeypatch.setenv("DEFAULT_TENANT_ID", "acme_corp")
        provider = EnvPrincipalProvider()
        principal = provider.get()
        assert principal.tenant_id == "acme_corp"

    def test_defaults_when_env_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Falls back to 'default' tenant when env var not set."""
        monkeypatch.delenv("DEFAULT_TENANT_ID", raising=False)
        monkeypatch.delenv("USER", raising=False)
        provider = EnvPrincipalProvider()
        principal = provider.get()
        assert principal.tenant_id == "default"
        assert principal.user_id == "anonymous"

    def test_session_id_stable_within_provider_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """session_id is generated once and reused on subsequent get() calls."""
        monkeypatch.delenv("DEFAULT_TENANT_ID", raising=False)
        provider = EnvPrincipalProvider()
        first = provider.get()
        second = provider.get()
        assert first.session_id == second.session_id
        assert len(first.session_id) == 32  # uuid4().hex

    def test_different_instances_different_session_ids(self) -> None:
        """Each provider instance gets its own session_id."""
        p1 = EnvPrincipalProvider()
        p2 = EnvPrincipalProvider()
        assert p1.get().session_id != p2.get().session_id

    def test_reads_roles_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFAULT_ROLES", "analyst,finance")
        provider = EnvPrincipalProvider()
        principal = provider.get()
        assert principal.roles == frozenset({"analyst", "finance"})

    def test_reads_attrs_json_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFAULT_ATTRS_JSON", '{"region": "APAC"}')
        provider = EnvPrincipalProvider()
        principal = provider.get()
        assert principal.attrs == {"region": "APAC"}

    def test_invalid_attrs_json_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFAULT_ATTRS_JSON", "not-json")
        provider = EnvPrincipalProvider()
        principal = provider.get()
        assert principal.attrs == {}


# ─────────────────────────────────────────────────────────────────────────────
# NullPolicyEngine / NullAuditLogger
# ─────────────────────────────────────────────────────────────────────────────


class TestNullPolicyEngine:
    def test_null_policy_always_allows_no_rewrite(self) -> None:
        engine = NullPolicyEngine()
        principal = _make_principal()
        decision = engine.authorize(principal, "SELECT * FROM orders", ["orders"])
        assert decision.outcome == AuthOutcome.ALLOW
        assert decision.rewritten_sql is None
        assert decision.masked_columns == {}

    def test_null_policy_allow_for_any_sql(self) -> None:
        engine = NullPolicyEngine()
        principal = _make_principal()
        decision = engine.authorize(principal, "DELETE FROM orders", ["orders"])
        assert decision.outcome == AuthOutcome.ALLOW


class TestNullAuditLogger:
    def test_null_audit_logger_discards(self) -> None:
        """NullAuditLogger.emit must not raise and must not write anything."""
        logger = NullAuditLogger()
        principal = _make_principal()
        decision = _make_auth_decision()
        event = AuditEvent(
            timestamp=datetime.now(tz=timezone.utc),
            principal=principal,
            intent="READ",
            sql_original="SELECT 1",
            sql_rewritten=None,
            referenced_entities=[],
            decision=decision,
            row_count=1,
            error=None,
            trace_id=None,
        )
        logger.emit(event)  # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# OntologyPolicyEngine — role checks
# ─────────────────────────────────────────────────────────────────────────────


class TestOntologyPolicyEngineRoles:
    def test_role_required_principal_lacks_role_denies(self) -> None:
        ctx = _make_ontology_ctx(required_roles=frozenset({"analyst"}))
        engine = OntologyPolicyEngine(ctx)
        principal = _make_principal(roles=frozenset())  # no roles
        decision = engine.authorize(principal, "SELECT * FROM orders", ["orders"])
        assert decision.outcome == AuthOutcome.DENY
        assert "analyst" in decision.reason

    def test_role_required_principal_has_role_allows(self) -> None:
        ctx = _make_ontology_ctx(required_roles=frozenset({"analyst"}))
        engine = OntologyPolicyEngine(ctx)
        principal = _make_principal(roles=frozenset({"analyst", "admin"}))
        decision = engine.authorize(principal, "SELECT * FROM orders", ["orders"])
        assert decision.outcome == AuthOutcome.ALLOW

    def test_no_role_required_allows_for_any_principal(self) -> None:
        ctx = _make_ontology_ctx(required_roles=frozenset())
        engine = OntologyPolicyEngine(ctx)
        principal = _make_principal(roles=frozenset())
        decision = engine.authorize(principal, "SELECT * FROM orders", ["orders"])
        assert decision.outcome == AuthOutcome.ALLOW

    def test_multiple_required_roles_any_missing_denies(self) -> None:
        ctx = _make_ontology_ctx(required_roles=frozenset({"analyst", "finance"}))
        engine = OntologyPolicyEngine(ctx)
        principal = _make_principal(roles=frozenset({"analyst"}))  # missing finance
        decision = engine.authorize(principal, "SELECT * FROM orders", ["orders"])
        assert decision.outcome == AuthOutcome.DENY


# ─────────────────────────────────────────────────────────────────────────────
# OntologyPolicyEngine — row filter rewriting (security-critical)
# ─────────────────────────────────────────────────────────────────────────────


class TestOntologyPolicyEngineRowFilter:
    def _engine_with_filter(
        self,
        row_filter: str = "tenant_id = $principal.tenant_id",
    ) -> tuple[OntologyPolicyEngine, Principal]:
        ctx = _make_ontology_ctx(
            entity="Order",
            physical_table="orders",
            required_roles=frozenset(),
            row_filter=row_filter,
        )
        engine = OntologyPolicyEngine(ctx)
        principal = _make_principal(tenant_id="tenant_a")
        return engine, principal

    def test_simple_select_gets_row_filter_wrapped(self) -> None:
        engine, principal = self._engine_with_filter()
        decision = engine.authorize(principal, "SELECT * FROM orders", ["orders"])
        assert decision.outcome == AuthOutcome.ALLOW
        assert decision.rewritten_sql is not None
        sql = decision.rewritten_sql
        # Subquery wrapper should be present
        assert "SELECT" in sql.upper()
        assert "orders" in sql
        assert "tenant_id" in sql
        assert "tenant_a" in sql

    def test_union_query_both_sides_wrapped(self) -> None:
        """Both tables in a UNION that match the policy must be wrapped."""
        ctx = _make_ontology_ctx(
            entity="Order",
            physical_table="orders",
            required_roles=frozenset(),
            row_filter="tenant_id = $principal.tenant_id",
        )
        engine = OntologyPolicyEngine(ctx)
        principal = _make_principal(tenant_id="tenant_a")
        sql = "SELECT * FROM orders UNION SELECT * FROM orders"
        decision = engine.authorize(principal, sql, ["orders"])
        assert decision.outcome == AuthOutcome.ALLOW
        assert decision.rewritten_sql is not None
        rewritten = decision.rewritten_sql
        # Both occurrences of the filter should appear (count how many times
        # the injected literal 'tenant_a' appears — should be ≥ 2)
        assert rewritten.count("tenant_a") >= 2

    def test_subquery_in_from_clause_wrapped(self) -> None:
        """A Table inside a derived table is also wrapped."""
        engine, principal = self._engine_with_filter()
        sql = "SELECT * FROM (SELECT * FROM orders) AS sub"
        decision = engine.authorize(principal, sql, ["orders"])
        assert decision.outcome == AuthOutcome.ALLOW
        assert decision.rewritten_sql is not None
        assert "tenant_a" in decision.rewritten_sql

    def test_in_clause_subquery_wrapped(self) -> None:
        """Table inside an IN subquery is wrapped."""
        engine, principal = self._engine_with_filter()
        sql = "SELECT * FROM customers WHERE id IN (SELECT id FROM orders)"
        decision = engine.authorize(principal, sql, ["orders"])
        assert decision.outcome == AuthOutcome.ALLOW
        assert decision.rewritten_sql is not None
        assert "tenant_a" in decision.rewritten_sql

    def test_join_target_wrapped(self) -> None:
        """Table on the right side of a JOIN is wrapped."""
        engine, principal = self._engine_with_filter()
        sql = "SELECT * FROM customers JOIN orders ON customers.id = orders.customer_id"
        decision = engine.authorize(principal, sql, ["orders"])
        assert decision.outcome == AuthOutcome.ALLOW
        assert decision.rewritten_sql is not None
        assert "tenant_a" in decision.rewritten_sql

    def test_idempotent_when_no_policies(self) -> None:
        """Entities without row_filter_template cause no SQL rewrite."""
        ctx = _make_ontology_ctx(
            entity="Order",
            physical_table="orders",
            required_roles=frozenset(),
            row_filter=None,  # no filter
        )
        engine = OntologyPolicyEngine(ctx)
        principal = _make_principal()
        sql = "SELECT * FROM orders"
        decision = engine.authorize(principal, sql, ["orders"])
        assert decision.outcome == AuthOutcome.ALLOW
        # No rewrite when no filter template
        assert decision.rewritten_sql is None

    def test_unrelated_table_not_wrapped(self) -> None:
        """Tables without a matching policy pass through untouched."""
        ctx = _make_ontology_ctx(
            entity="Order",
            physical_table="orders",
            row_filter="tenant_id = $principal.tenant_id",
        )
        engine = OntologyPolicyEngine(ctx)
        principal = _make_principal()
        # customers has no policy
        sql = "SELECT * FROM customers"
        decision = engine.authorize(principal, sql, ["customers"])
        assert decision.outcome == AuthOutcome.ALLOW
        # No rewrite (no filter applicable)
        assert decision.rewritten_sql is None


# ─────────────────────────────────────────────────────────────────────────────
# OntologyPolicyEngine — placeholder safety
# ─────────────────────────────────────────────────────────────────────────────


class TestPlaceholderSafety:
    def test_placeholder_tenant_id_interpolated_as_literal(self) -> None:
        """The resolved value appears as a quoted SQL string literal."""
        ctx = _make_ontology_ctx(row_filter="tenant_id = $principal.tenant_id")
        engine = OntologyPolicyEngine(ctx)
        principal = _make_principal(tenant_id="my_tenant")
        decision = engine.authorize(principal, "SELECT * FROM orders", ["orders"])
        assert decision.rewritten_sql is not None
        # Value must appear as a quoted literal, not bare token
        assert "'my_tenant'" in decision.rewritten_sql

    def test_placeholder_user_id_interpolated(self) -> None:
        ctx = _make_ontology_ctx(row_filter="user_id = $principal.user_id")
        engine = OntologyPolicyEngine(ctx)
        principal = _make_principal(user_id="bob")
        decision = engine.authorize(principal, "SELECT * FROM orders", ["orders"])
        assert decision.rewritten_sql is not None
        assert "'bob'" in decision.rewritten_sql

    def test_placeholder_attr_with_sql_injection_chars_escaped(self) -> None:
        """An attr value containing SQL metacharacters must not break the SQL."""
        import sqlglot

        ctx = _make_ontology_ctx(row_filter="region = $principal.attrs.region")
        engine = OntologyPolicyEngine(ctx)
        # Injection attempt in attr value
        malicious = "APAC'); DROP TABLE orders;--"
        principal = _make_principal(attrs={"region": malicious})
        decision = engine.authorize(principal, "SELECT * FROM orders", ["orders"])
        assert decision.outcome == AuthOutcome.ALLOW
        assert decision.rewritten_sql is not None
        # Rewritten SQL must still be parseable (no injection breakout)
        reparsed = sqlglot.parse_one(decision.rewritten_sql)
        assert reparsed is not None
        # Injection safety: sqlglot must parse exactly ONE statement (no breakout)
        statements = sqlglot.parse(decision.rewritten_sql)
        assert len(statements) == 1, (
            f"SQL injection produced {len(statements)} statements: {decision.rewritten_sql}"
        )
        # The malicious value must appear verbatim inside the single string literal
        assert "APAC" in decision.rewritten_sql

    def test_unknown_placeholder_returns_deny(self) -> None:
        """A template referencing an absent attrs key → DENY with reason."""
        ctx = _make_ontology_ctx(row_filter="cost_center = $principal.attrs.unknown_key")
        engine = OntologyPolicyEngine(ctx)
        principal = _make_principal(attrs={})  # no 'unknown_key'
        decision = engine.authorize(principal, "SELECT * FROM orders", ["orders"])
        assert decision.outcome == AuthOutcome.DENY
        assert "missing_principal_attr" in decision.reason
        assert "unknown_key" in decision.reason


# ─────────────────────────────────────────────────────────────────────────────
# Masked columns — post-processing
# ─────────────────────────────────────────────────────────────────────────────


class TestMaskedColumnsPostProcessing:
    def test_masked_columns_post_processed_in_execute_node(self) -> None:
        """_apply_masked_columns replaces column values according to the mask method."""
        from src.agent.nodes.read_write import _apply_masked_columns

        rows = [
            {"id": 1, "email": "alice@example.com", "ssn": "123-45-6789", "name": "Alice"},
            {"id": 2, "email": "bob@example.com", "ssn": "987-65-4321", "name": "Bob"},
        ]
        masked = _apply_masked_columns(rows, {"email": "hash", "ssn": "redact"})

        # hash: sha256[:16]
        expected_hash = hashlib.sha256("alice@example.com".encode()).hexdigest()[:16]
        assert masked[0]["email"] == expected_hash
        # redact
        assert masked[0]["ssn"] == "***REDACTED***"
        assert masked[1]["ssn"] == "***REDACTED***"
        # untouched columns preserved
        assert masked[0]["name"] == "Alice"
        assert masked[0]["id"] == 1

    def test_mask_null_method(self) -> None:
        from src.agent.nodes.read_write import _apply_masked_columns

        rows = [{"secret": "value123"}]
        masked = _apply_masked_columns(rows, {"secret": "null"})
        assert masked[0]["secret"] is None

    def test_mask_unknown_method_passthrough(self) -> None:
        from src.agent.nodes.read_write import _apply_masked_columns

        rows = [{"col": "data"}]
        masked = _apply_masked_columns(rows, {"col": "unknown_method"})
        assert masked[0]["col"] == "data"

    def test_no_masked_columns_returns_rows_unchanged(self) -> None:
        from src.agent.nodes.read_write import _apply_masked_columns

        rows = [{"a": 1}, {"a": 2}]
        assert _apply_masked_columns(rows, {}) is rows

    def test_empty_rows_returns_empty(self) -> None:
        from src.agent.nodes.read_write import _apply_masked_columns

        assert _apply_masked_columns([], {"email": "hash"}) == []

    def test_masked_columns_from_policy_propagated(self) -> None:
        """OntologyPolicyEngine propagates masked_columns from SecurityPolicy."""
        ctx = _make_ontology_ctx(
            masked_columns={"email": "hash", "ssn": "redact"},
            row_filter=None,
        )
        engine = OntologyPolicyEngine(ctx)
        principal = _make_principal()
        decision = engine.authorize(principal, "SELECT * FROM orders", ["orders"])
        assert decision.masked_columns == {"email": "hash", "ssn": "redact"}


# ─────────────────────────────────────────────────────────────────────────────
# JsonlAuditLogger
# ─────────────────────────────────────────────────────────────────────────────


class TestJsonlAuditLogger:
    def _make_event(self) -> AuditEvent:
        return AuditEvent(
            timestamp=datetime.now(tz=timezone.utc),
            principal=_make_principal(),
            intent="READ",
            sql_original="SELECT * FROM orders",
            sql_rewritten=None,
            referenced_entities=["orders"],
            decision=_make_auth_decision(),
            row_count=42,
            error=None,
            trace_id="trace_abc123",
        )

    def test_jsonl_audit_writes_metadata_no_rows(self, tmp_path: Path) -> None:
        """Emitted records contain metadata but NOT row data."""
        log_path = tmp_path / "audit.jsonl"
        logger = JsonlAuditLogger(path=log_path)
        logger.emit(self._make_event())

        line = log_path.read_text(encoding="utf-8").strip()
        record = json.loads(line)
        # Must have metadata
        assert "timestamp" in record
        assert "principal" in record
        assert "intent" in record
        assert "decision" in record
        # Must NOT have row data
        assert "rows" not in record
        assert "row_data" not in record
        assert "query_result" not in record

    def test_jsonl_audit_event_serializable(self, tmp_path: Path) -> None:
        """Written line round-trips back to a dict with expected fields."""
        log_path = tmp_path / "audit.jsonl"
        logger = JsonlAuditLogger(path=log_path)
        event = self._make_event()
        logger.emit(event)

        line = log_path.read_text(encoding="utf-8").strip()
        record = json.loads(line)

        assert record["intent"] == "READ"
        assert record["row_count"] == 42
        assert record["trace_id"] == "trace_abc123"
        assert record["sql_original"] == "SELECT * FROM orders"
        assert record["principal"]["tenant_id"] == "tenant_a"
        assert record["decision"]["outcome"] == "allow"

    def test_jsonl_multiple_events_appended(self, tmp_path: Path) -> None:
        """Each emit appends a separate JSONL line."""
        log_path = tmp_path / "audit.jsonl"
        logger = JsonlAuditLogger(path=log_path)
        logger.emit(self._make_event())
        logger.emit(self._make_event())

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

    def test_jsonl_fail_mode_open_continues_on_error(self, tmp_path: Path) -> None:
        """fail_mode='open' warns but does not raise on write failure."""
        log_path = tmp_path / "audit.jsonl"
        logger = JsonlAuditLogger(path=log_path, fail_mode="open")
        # Force a write error by closing the file handle
        logger._file.close()
        # Should not raise
        logger.emit(self._make_event())  # warn only

    def test_jsonl_fail_mode_closed_raises_on_error(self, tmp_path: Path) -> None:
        """fail_mode='closed' re-raises exceptions from emit."""
        log_path = tmp_path / "audit.jsonl"
        logger = JsonlAuditLogger(path=log_path, fail_mode="closed")
        logger._file.close()
        with pytest.raises(Exception):
            logger.emit(self._make_event())

    def test_event_dict_serialises_enum(self) -> None:
        """_event_to_dict serialises AuthOutcome enum to its string value."""
        event = self._make_event()
        d = _event_to_dict(event)
        assert d["decision"]["outcome"] == "allow"

    def test_event_dict_timestamp_is_string(self) -> None:
        event = self._make_event()
        d = _event_to_dict(event)
        assert isinstance(d["timestamp"], str)


# ─────────────────────────────────────────────────────────────────────────────
# authorize_node integration
# ─────────────────────────────────────────────────────────────────────────────


class _FakeAuditLogger(NullAuditLogger):
    """Captures emitted events for test assertions."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


class TestAuthorizeNodeIntegration:
    def _state(self, sql: str = "SELECT * FROM orders", intent: str = "READ") -> dict:
        return {"generated_sql": sql, "intent": intent}

    def test_authorize_node_allow_passes_rewritten_sql_through_state(self) -> None:
        """ALLOW with a rewritten SQL updates state['generated_sql']."""
        from src.agent.nodes.authorize import authorize_node

        ctx = _make_ontology_ctx(
            row_filter="tenant_id = $principal.tenant_id",
            required_roles=frozenset(),
        )
        engine = OntologyPolicyEngine(ctx)
        security = SecurityContext(
            principal_provider=EnvPrincipalProvider(),
            policy=engine,
            audit=NullAuditLogger(),
        )
        state = self._state()
        result = authorize_node(state, security)

        assert result["auth_decision"].outcome == AuthOutcome.ALLOW
        # generated_sql should be updated if rewritten
        if result["auth_decision"].rewritten_sql:
            assert result["generated_sql"] == result["auth_decision"].rewritten_sql
        assert result["sql_original"] == "SELECT * FROM orders"
        assert "principal" in result

    def test_authorize_node_null_engine_allow_no_rewrite(self) -> None:
        """NullPolicyEngine → ALLOW, generated_sql unchanged."""
        from src.agent.nodes.authorize import authorize_node

        security = SecurityContext.null()
        state = self._state()
        result = authorize_node(state, security)

        assert result["auth_decision"].outcome == AuthOutcome.ALLOW
        # NullPolicyEngine never rewrites
        assert "generated_sql" not in result  # unchanged
        assert "error" not in result

    def test_authorize_node_deny_writes_error_and_audit(self) -> None:
        """DENY outcome writes error to state and emits audit."""
        from src.agent.nodes.authorize import authorize_node
        from src.agent.graph import _finalize_deny

        ctx = _make_ontology_ctx(required_roles=frozenset({"admin"}))
        engine = OntologyPolicyEngine(ctx)
        fake_audit = _FakeAuditLogger()
        security = SecurityContext(
            principal_provider=EnvPrincipalProvider(),
            policy=engine,
            audit=fake_audit,
        )
        state = self._state()
        result = authorize_node(state, security)

        assert result["auth_decision"].outcome == AuthOutcome.DENY
        assert "error" in result

        # Simulate the deny node (finalize_deny)
        combined_state = {**state, **result}
        deny_result = _finalize_deny(combined_state, security)
        assert "Access denied" in deny_result["response"]
        # Audit should have been emitted
        assert len(fake_audit.events) == 1
        assert fake_audit.events[0].decision.outcome == AuthOutcome.DENY

    def test_authorize_node_sets_masked_columns_in_state(self) -> None:
        """authorize_node copies masked_columns from decision to state."""
        from src.agent.nodes.authorize import authorize_node

        ctx = _make_ontology_ctx(
            masked_columns={"email": "hash"},
            row_filter=None,
        )
        engine = OntologyPolicyEngine(ctx)
        security = SecurityContext(
            principal_provider=EnvPrincipalProvider(),
            policy=engine,
            audit=NullAuditLogger(),
        )
        state = self._state()
        result = authorize_node(state, security)
        assert result.get("masked_columns") == {"email": "hash"}

    def test_authorize_node_three_state_routing(self) -> None:
        """_route_after_authorize returns correct next node for each outcome."""
        from src.agent.graph import _route_after_authorize

        allow_state = {
            "auth_decision": AuthDecision(outcome=AuthOutcome.ALLOW, reason="ok")
        }
        deny_state = {
            "auth_decision": AuthDecision(outcome=AuthOutcome.DENY, reason="blocked")
        }
        approval_state = {
            "auth_decision": AuthDecision(
                outcome=AuthOutcome.NEEDS_USER_APPROVAL, reason="write"
            )
        }
        no_auth_state: dict = {}

        assert _route_after_authorize(allow_state) == "execute_sql"
        assert _route_after_authorize(deny_state) == "deny"
        assert _route_after_authorize(approval_state) == "needs_user_approval"
        assert _route_after_authorize(no_auth_state) == "execute_sql"  # default


# ─────────────────────────────────────────────────────────────────────────────
# SecurityContext
# ─────────────────────────────────────────────────────────────────────────────


class TestSecurityContext:
    def test_null_context_has_no_op_components(self) -> None:
        ctx = SecurityContext.null()
        assert isinstance(ctx.policy, NullPolicyEngine)
        assert isinstance(ctx.audit, NullAuditLogger)
        assert isinstance(ctx.principal_provider, EnvPrincipalProvider)

    def test_null_context_principal_resolves(self) -> None:
        ctx = SecurityContext.null()
        principal = ctx.principal_provider.get()
        assert isinstance(principal, Principal)


# ─────────────────────────────────────────────────────────────────────────────
# _parse_referenced_entities helper
# ─────────────────────────────────────────────────────────────────────────────


class TestParseReferencedEntities:
    def test_simple_select(self) -> None:
        from src.agent.nodes.authorize import _parse_referenced_entities

        entities = _parse_referenced_entities("SELECT * FROM orders")
        assert "orders" in entities

    def test_join(self) -> None:
        from src.agent.nodes.authorize import _parse_referenced_entities

        entities = _parse_referenced_entities(
            "SELECT * FROM customers JOIN orders ON customers.id = orders.customer_id"
        )
        assert "customers" in entities
        assert "orders" in entities

    def test_union(self) -> None:
        from src.agent.nodes.authorize import _parse_referenced_entities

        entities = _parse_referenced_entities(
            "SELECT * FROM orders UNION SELECT * FROM archive_orders"
        )
        assert "orders" in entities
        assert "archive_orders" in entities

    def test_empty_sql(self) -> None:
        from src.agent.nodes.authorize import _parse_referenced_entities

        assert _parse_referenced_entities("") == []

    def test_invalid_sql_returns_empty(self) -> None:
        from src.agent.nodes.authorize import _parse_referenced_entities

        # Should not raise
        result = _parse_referenced_entities("NOT VALID SQL !!!@@@###")
        assert isinstance(result, list)


# ─────────────────────────────────────────────────────────────────────────────
# RDF provider: security annotations loaded from ecommerce.rdf
# ─────────────────────────────────────────────────────────────────────────────


class TestRDFSecurityAnnotations:
    def test_order_entity_has_security_policy(self) -> None:
        """ecommerce.rdf Order entity must have requiresRole=analyst and rowFilter."""
        from src.ontology.rdf_provider import RDFOntologyProvider

        rdf_path = str(
            Path(__file__).parent.parent / "ontologies" / "ecommerce.rdf"
        )
        provider = RDFOntologyProvider([rdf_path])
        ctx = provider.context
        order_mapping = ctx.physical_mappings.get("Order")
        assert order_mapping is not None, "Order mapping not found"
        assert order_mapping.policy is not None, "Order should have a SecurityPolicy"
        assert "analyst" in order_mapping.policy.required_roles
        assert order_mapping.policy.row_filter_template is not None
        assert "$principal.tenant_id" in order_mapping.policy.row_filter_template

    def test_order_mask_columns_parsed(self) -> None:
        """maskColumns annotation on Order is parsed correctly."""
        from src.ontology.rdf_provider import RDFOntologyProvider

        rdf_path = str(
            Path(__file__).parent.parent / "ontologies" / "ecommerce.rdf"
        )
        provider = RDFOntologyProvider([rdf_path])
        ctx = provider.context
        order_mapping = ctx.physical_mappings["Order"]
        assert order_mapping.policy is not None
        assert "email" in order_mapping.policy.masked_columns
        assert "ssn" in order_mapping.policy.masked_columns

    def test_entities_without_annotations_have_no_policy(self) -> None:
        """Entities without security annotations should have policy=None."""
        from src.ontology.rdf_provider import RDFOntologyProvider

        rdf_path = str(
            Path(__file__).parent.parent / "ontologies" / "ecommerce.rdf"
        )
        provider = RDFOntologyProvider([rdf_path])
        ctx = provider.context
        # Product has no security annotations (in the current ecommerce.rdf)
        product_mapping = ctx.physical_mappings.get("Product")
        if product_mapping:
            assert product_mapping.policy is None, "Product should have no security policy"


# ─────────────────────────────────────────────────────────────────────────────
# _finalize_pending: NEEDS_USER_APPROVAL audit trail
# ─────────────────────────────────────────────────────────────────────────────


class RecordingAuditLogger(NullAuditLogger):
    """In-memory audit logger that records every emitted event (test-only)."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


class TestFinalizePending:
    """Unit tests for _finalize_pending called directly."""

    def _make_security(self) -> tuple[RecordingAuditLogger, SecurityContext]:
        recorder = RecordingAuditLogger()
        security = SecurityContext(
            principal_provider=EnvPrincipalProvider(),
            policy=NullPolicyEngine(),
            audit=recorder,
        )
        return recorder, security

    def test_emits_exactly_one_event(self) -> None:
        from src.agent.graph import _finalize_pending

        recorder, security = self._make_security()
        state = {
            "intent": "WRITE",
            "generated_sql": "INSERT INTO orders VALUES (1)",
            "auth_decision": AuthDecision(
                outcome=AuthOutcome.NEEDS_USER_APPROVAL, reason="awaiting_approval"
            ),
        }
        result = _finalize_pending(state, security)

        assert result == {}
        assert len(recorder.events) == 1

    def test_event_outcome_is_needs_user_approval(self) -> None:
        from src.agent.graph import _finalize_pending

        recorder, security = self._make_security()
        state = {
            "intent": "WRITE",
            "sql_original": "INSERT INTO orders VALUES (1)",
            "auth_decision": AuthDecision(
                outcome=AuthOutcome.NEEDS_USER_APPROVAL, reason="awaiting_approval"
            ),
        }
        _finalize_pending(state, security)

        event = recorder.events[0]
        assert event.decision.outcome == AuthOutcome.NEEDS_USER_APPROVAL

    def test_event_error_and_row_count_are_none(self) -> None:
        from src.agent.graph import _finalize_pending

        recorder, security = self._make_security()
        state = {
            "intent": "WRITE",
            "sql_original": "DELETE FROM orders WHERE id=1",
            "auth_decision": AuthDecision(
                outcome=AuthOutcome.NEEDS_USER_APPROVAL, reason="awaiting_approval"
            ),
        }
        _finalize_pending(state, security)

        event = recorder.events[0]
        assert event.error is None
        assert event.row_count is None

    def test_sql_original_preferred_over_generated_sql(self) -> None:
        from src.agent.graph import _finalize_pending

        recorder, security = self._make_security()
        state = {
            "intent": "WRITE",
            "sql_original": "DELETE FROM orders WHERE id=1",
            "generated_sql": "something_else",
            "auth_decision": AuthDecision(
                outcome=AuthOutcome.NEEDS_USER_APPROVAL, reason="awaiting_approval"
            ),
        }
        _finalize_pending(state, security)

        event = recorder.events[0]
        assert event.sql_original == "DELETE FROM orders WHERE id=1"

    def test_returns_empty_dict_state_noop(self) -> None:
        from src.agent.graph import _finalize_pending

        _, security = self._make_security()
        state = {
            "intent": "WRITE",
            "auth_decision": AuthDecision(
                outcome=AuthOutcome.NEEDS_USER_APPROVAL, reason="awaiting_approval"
            ),
        }
        result = _finalize_pending(state, security)
        assert result == {}


class TestAuthorizeNeedsApprovalAudit:
    """Integration test: NEEDS_USER_APPROVAL path emits audit via graph."""

    def _build_fake_ontology(self) -> object:
        from src.ontology.provider import OntologyContext, OntologyProvider

        ctx = OntologyContext(schema_for_llm="", rules={}, physical_mappings={})

        class _FakeProvider(OntologyProvider):
            @property
            def context(self) -> OntologyContext:
                return ctx

            def load(self) -> OntologyContext:
                return ctx

        return _FakeProvider()

    def test_needs_user_approval_emits_audit(self) -> None:
        """End-to-end: graph run with NEEDS_USER_APPROVAL records one audit event."""
        from src.agent.graph import build_graph
        from src.database.executor import BaseExecutor, SQLResult

        # --- Fake LLM: classify → WRITE, generate_sql → INSERT ---
        class _FakeLLM:
            _calls: int = 0

            def chat(
                self,
                messages: list[dict],
                system_prompt: str | None = None,
                temperature: float = 0.0,
            ) -> str:
                self._calls += 1
                if self._calls == 1:
                    return "WRITE"
                return "INSERT INTO orders (id) VALUES (99)"

            def get_model_name(self) -> str:
                return "fake-model"

        # --- Fake policy engine: always returns NEEDS_USER_APPROVAL ---
        class _ApprovalPolicyEngine:
            def authorize(self, principal, sql, entities):  # noqa: ANN001
                return AuthDecision(
                    outcome=AuthOutcome.NEEDS_USER_APPROVAL,
                    reason="awaiting_approval",
                    rewritten_sql=None,
                    masked_columns={},
                )

        class _FakeExecutor(BaseExecutor):
            @property
            def dialect(self) -> str:
                return "sqlite"

            def execute(self, sql: str, approved: bool = False) -> SQLResult:
                return SQLResult(
                    operation="write",
                    rows=[],
                    affected_rows=0,
                    needs_approval=True,
                )

        recorder = RecordingAuditLogger()
        security = SecurityContext(
            principal_provider=EnvPrincipalProvider(),
            policy=_ApprovalPolicyEngine(),
            audit=recorder,
        )

        ontology = self._build_fake_ontology()
        graph = build_graph(
            llm=_FakeLLM(),
            executors=_FakeExecutor(),
            ontology=ontology,
            security=security,
        )

        graph.invoke({"user_query": "insert a new order"})

        # Exactly one audit event; correct outcome; metadata clean
        assert len(recorder.events) == 1, f"Expected 1 audit event, got {len(recorder.events)}"
        event = recorder.events[0]
        assert event.decision.outcome == AuthOutcome.NEEDS_USER_APPROVAL
        assert event.error is None
        assert event.row_count is None
