"""Public API for the ``src.security`` package.

Re-exports all public symbols so callers can do::

    from src.security import Principal, SecurityContext, OntologyPolicyEngine
"""
from src.security.audit import AuditEvent, AuditLogger, JsonlAuditLogger, NullAuditLogger
from src.security.context import SecurityContext
from src.security.policy import (
    AuthDecision,
    AuthOutcome,
    NullPolicyEngine,
    OntologyPolicyEngine,
    PolicyEngine,
)
from src.security.principal import (
    EnvPrincipalProvider,
    Principal,
    PrincipalProvider,
    StreamlitSessionPrincipalProvider,
)

__all__ = [
    # principal
    "Principal",
    "PrincipalProvider",
    "EnvPrincipalProvider",
    "StreamlitSessionPrincipalProvider",
    # policy
    "PolicyEngine",
    "OntologyPolicyEngine",
    "NullPolicyEngine",
    "AuthOutcome",
    "AuthDecision",
    # audit
    "AuditLogger",
    "JsonlAuditLogger",
    "NullAuditLogger",
    "AuditEvent",
    # context
    "SecurityContext",
]
