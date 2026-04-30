"""Principal identity model and provider abstractions.

A ``Principal`` represents the authenticated identity that issues a query.
``PrincipalProvider`` is an ABC for the various entry points (CLI, Web, API)
that know how to resolve the current principal.
"""
from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    """Immutable identity value object.

    Attributes:
        tenant_id: Multi-tenant hard-isolation unit.
        user_id: Unique identifier for the human or service principal.
        roles: Set of granted roles, e.g. ``frozenset({"analyst", "finance"})``.
        attrs: Arbitrary string claims such as ``{"region": "APAC"}``.
            Treated as read-only by convention; never mutated downstream.
        session_id: Opaque token linking this request to audit and memory records.
    """

    tenant_id: str
    user_id: str
    roles: frozenset[str]
    attrs: dict[str, str]
    session_id: str


class PrincipalProvider(ABC):
    """Abstract factory for resolving the current principal.

    Each application entry point (CLI, Streamlit, REST) has its own
    concrete implementation that knows how to read identity information
    from the appropriate source (env vars, session state, JWT header, …).
    """

    @abstractmethod
    def get(self) -> Principal:
        """Return the current ``Principal``.

        Returns:
            A fully-populated immutable ``Principal`` instance.
        """
        ...


class EnvPrincipalProvider(PrincipalProvider):
    """Read principal from environment variables; for CLI / single-tenant deployments.

    Environment variables consulted:
    - ``DEFAULT_TENANT_ID`` (default: ``"default"``)
    - ``USER`` (default: ``"anonymous"``)
    - ``DEFAULT_ROLES`` — comma-separated roles (default: empty)
    - ``DEFAULT_ATTRS_JSON`` — JSON object for custom attrs (default: ``{}``)

    The ``session_id`` is generated once on the first ``get()`` call and
    cached for the lifetime of this provider instance.
    """

    def __init__(self) -> None:
        self._session_id: str | None = None

    def get(self) -> Principal:
        """Construct and return a ``Principal`` from environment variables.

        Returns:
            Principal populated from env vars.
        """
        if self._session_id is None:
            self._session_id = uuid4().hex

        tenant_id = os.environ.get("DEFAULT_TENANT_ID", "default")
        user_id = os.environ.get("USER", "anonymous")
        roles_raw = os.environ.get("DEFAULT_ROLES", "")
        roles: frozenset[str] = frozenset(r for r in roles_raw.split(",") if r)

        attrs: dict[str, str] = {}
        attrs_json = os.environ.get("DEFAULT_ATTRS_JSON", "")
        if attrs_json:
            try:
                parsed = json.loads(attrs_json)
                attrs = {str(k): str(v) for k, v in parsed.items()}
            except (json.JSONDecodeError, AttributeError):
                logger.warning("DEFAULT_ATTRS_JSON is not valid JSON; ignoring.")

        return Principal(
            tenant_id=tenant_id,
            user_id=user_id,
            roles=roles,
            attrs=attrs,
            session_id=self._session_id,
        )


class StreamlitSessionPrincipalProvider(PrincipalProvider):
    """Read principal from ``st.session_state``; falls back to ``EnvPrincipalProvider``.

    Expected session state keys:
    - ``"tenant_id"``
    - ``"user_id"``
    - ``"roles"`` — list[str] or comma-separated string
    - ``"attrs"`` — dict[str, str]
    - ``"session_id"``

    Any missing key falls back to the ``EnvPrincipalProvider`` value.
    Streamlit is imported lazily inside ``get()`` to avoid a hard dependency
    at module import time (keeps CLI / test environments unaffected).
    """

    def __init__(self) -> None:
        self._env_fallback = EnvPrincipalProvider()

    def get(self) -> Principal:
        """Resolve principal from Streamlit session state with env fallback.

        Returns:
            Principal populated from session state or environment variables.
        """
        try:
            import streamlit as st  # lazy import — optional dependency

            env = self._env_fallback.get()
            ss = st.session_state

            tenant_id: str = ss.get("tenant_id", env.tenant_id)
            user_id: str = ss.get("user_id", env.user_id)

            raw_roles = ss.get("roles", env.roles)
            if isinstance(raw_roles, str):
                roles: frozenset[str] = frozenset(r for r in raw_roles.split(",") if r)
            elif isinstance(raw_roles, (list, set, frozenset)):
                roles = frozenset(str(r) for r in raw_roles)
            else:
                roles = env.roles

            attrs: dict[str, str] = ss.get("attrs", env.attrs)
            if not isinstance(attrs, dict):
                attrs = env.attrs

            session_id: str = ss.get("session_id", env.session_id)

            return Principal(
                tenant_id=tenant_id,
                user_id=user_id,
                roles=roles,
                attrs=attrs,
                session_id=session_id,
            )
        except ImportError:
            logger.debug("Streamlit not available; falling back to EnvPrincipalProvider.")
            return self._env_fallback.get()
        except Exception as exc:  # noqa: BLE001
            logger.warning("StreamlitSessionPrincipalProvider.get() failed: %s; using env fallback.", exc)
            return self._env_fallback.get()
