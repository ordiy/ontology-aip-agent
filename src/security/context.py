"""Security context bundle.

``SecurityContext`` is the single dependency-injection unit that groups
together the three security components:  ``PrincipalProvider``,
``PolicyEngine``, and ``AuditLogger``.

It is passed to ``build_graph`` and captured by node closures, keeping all
security dependencies explicit and testable.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.security.audit import AuditLogger, NullAuditLogger
from src.security.policy import NullPolicyEngine, PolicyEngine
from src.security.principal import EnvPrincipalProvider, PrincipalProvider


@dataclass
class SecurityContext:
    """Bundle of ``(PrincipalProvider, PolicyEngine, AuditLogger)``.

    Each component has a no-op default so that tests and OSS deployments
    do not need to wire them up explicitly.

    Attributes:
        principal_provider: Resolves the current authenticated identity.
        policy: Evaluates whether a principal may execute a SQL statement.
        audit: Persists audit events for compliance and forensics.
    """

    principal_provider: PrincipalProvider
    policy: PolicyEngine
    audit: AuditLogger

    @classmethod
    def null(cls) -> "SecurityContext":
        """Return a no-op context suitable for tests and the OSS default.

        Returns:
            ``SecurityContext`` with ``EnvPrincipalProvider``, ``NullPolicyEngine``,
            and ``NullAuditLogger``.
        """
        return cls(
            principal_provider=EnvPrincipalProvider(),
            policy=NullPolicyEngine(),
            audit=NullAuditLogger(),
        )
