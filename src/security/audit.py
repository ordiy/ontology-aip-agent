"""Audit logging abstractions and implementations.

``AuditLogger`` emits ``AuditEvent`` records that capture who ran what SQL,
the authorisation outcome, and execution metadata.  No row data is ever stored.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.security.policy import AuthDecision
    from src.security.principal import Principal

logger = logging.getLogger(__name__)


@dataclass
class AuditEvent:
    """Immutable audit record — metadata only, never row data.

    Attributes:
        timestamp: UTC timestamp of the event.
        principal: The identity that issued the query.
        intent: Agent intent label (READ / WRITE / ANALYZE / DECIDE / OPERATE).
        sql_original: The SQL as produced by the LLM, before any policy rewrite.
        sql_rewritten: The SQL after row-filter injection; ``None`` if not rewritten.
        referenced_entities: Physical table names extracted from the SQL.
        decision: The full ``AuthDecision`` from the policy engine.
        row_count: Number of rows returned / affected; ``None`` on error or denial.
        error: Error message if execution failed; ``None`` on success.
        trace_id: Langfuse trace ID for correlated observability; optional.
    """

    timestamp: datetime
    principal: "Principal"
    intent: str
    sql_original: str | None
    sql_rewritten: str | None
    referenced_entities: list[str]
    decision: "AuthDecision"
    row_count: int | None
    error: str | None
    trace_id: str | None


def _event_to_dict(event: AuditEvent) -> dict:
    """Serialise an ``AuditEvent`` to a JSON-safe dict.

    Handles datetime → ISO string, frozenset → sorted list,
    dataclass nesting (Principal, AuthDecision).

    Args:
        event: The event to serialise.

    Returns:
        A plain dict suitable for ``json.dumps``.
    """
    from src.security.policy import AuthDecision  # local import avoids circular

    d = asdict(event)
    # datetime → ISO string
    d["timestamp"] = event.timestamp.isoformat()
    # frozenset in Principal.roles → sorted list
    if "principal" in d and isinstance(d["principal"].get("roles"), (set, frozenset)):
        d["principal"]["roles"] = sorted(d["principal"]["roles"])
    # AuthDecision.outcome Enum → string value
    if "decision" in d and isinstance(d["decision"].get("outcome"), str):
        pass  # asdict already converts Enum to its value in Python 3.11+
    # Ensure Enum is serialised — asdict doesn't always unwrap Enum
    if hasattr(event.decision, "outcome"):
        d["decision"]["outcome"] = event.decision.outcome.value
    return d


class AuditLogger(ABC):
    """Abstract audit sink.

    Subclasses implement ``emit`` to persist ``AuditEvent`` records.
    The ``fail_mode`` property controls behaviour on write errors:

    - ``"open"`` (default): warn and continue — safe for development.
    - ``"closed"``: re-raise the exception — required for production.

    Invariant 12: in production deployments ``fail_mode`` **must** be
    ``"closed"``; a failed ``emit`` must abort the query.
    """

    @abstractmethod
    def emit(self, event: AuditEvent) -> None:
        """Persist *event* to the audit sink.

        Args:
            event: The audit record to store.

        Raises:
            Exception: In ``fail_mode="closed"`` only — re-raised on write failure.
        """
        ...

    @property
    def fail_mode(self) -> str:
        """Return the failure mode: ``"open"`` or ``"closed"``."""
        return "open"


class NullAuditLogger(AuditLogger):
    """No-op audit logger — silently discards every event.

    Used as the default in OSS / test configurations.
    """

    def emit(self, event: AuditEvent) -> None:
        """Discard the event without any I/O.

        Args:
            event: Ignored.
        """


class JsonlAuditLogger(AuditLogger):
    """Append-only JSONL file audit logger.

    Each ``emit`` call appends a single JSON line and flushes immediately.
    No buffering is performed so that partial writes are recoverable.

    Args:
        path: File path to write JSONL records to. Parent directories must exist.
        fail_mode: ``"open"`` (warn on errors, continue) or
                   ``"closed"`` (re-raise on errors).  Default ``"open"``.
    """

    def __init__(self, path: str | Path, fail_mode: str = "open") -> None:
        self._path = Path(path)
        self._fail_mode = fail_mode
        # Open once for the lifetime of this logger; append mode is atomic
        # for single-process usage (sufficient for MVP).
        self._file = self._path.open("a", encoding="utf-8")

    @property
    def fail_mode(self) -> str:
        """Return the configured failure mode."""
        return self._fail_mode

    def emit(self, event: AuditEvent) -> None:
        """Serialise *event* as a JSON line and flush to disk.

        Args:
            event: The audit record to write.

        Raises:
            Exception: Only when ``fail_mode="closed"`` and the write fails.
        """
        try:
            record = _event_to_dict(event)
            line = json.dumps(record, default=str, ensure_ascii=False)
            self._file.write(line + "\n")
            self._file.flush()
        except Exception as exc:
            if self._fail_mode == "closed":
                raise
            logger.warning(
                "JsonlAuditLogger: failed to write audit event (fail_mode=open): %s", exc
            )

    def __del__(self) -> None:
        try:
            self._file.close()
        except Exception:
            pass
