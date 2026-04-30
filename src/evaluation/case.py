"""EvalCase and ExpectedAnswer data models for the evaluation framework.

Each EvalCase captures a single natural-language question, its expected
intent, and a typed expectation about the agent's output.  Cases are
immutable (``frozen=True``) so they can be safely shared across parallel
runner workers.
"""
from __future__ import annotations

import logging
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

_VALID_DOMAINS: frozenset[str] = frozenset(
    {"ecommerce", "finance", "healthcare", "manufacturing", "retail", "education"}
)
_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")
_TAG_PATTERN = re.compile(r"^[a-z0-9_-]+$")


# ---------------------------------------------------------------------------
# ExpectedAnswer discriminated union
# ---------------------------------------------------------------------------


class IntentOnlyExpectation(BaseModel):
    """Assert that the agent classifies the intent correctly; SQL/result are best-effort.

    Attributes:
        kind: Discriminator literal ``"intent_only"``.
        expected_intent: The intent label the agent should emit.
    """

    kind: Literal["intent_only"] = "intent_only"
    expected_intent: str

    model_config = ConfigDict(frozen=True)


class SQLEquivalentExpectation(BaseModel):
    """Assert that the agent generates SQL equivalent (via AST) to a canonical query.

    Attributes:
        kind: Discriminator literal ``"sql_equivalent"``.
        expected_sql: Canonical SQL statement for AST-level comparison.
    """

    kind: Literal["sql_equivalent"] = "sql_equivalent"
    expected_sql: str

    model_config = ConfigDict(frozen=True)


class ResultSetExpectation(BaseModel):
    """Assert that the agent's query returns a specific result set.

    Attributes:
        kind: Discriminator literal ``"result_set"``.
        expected_rows: List of row dicts the query must return.
        order_sensitive: When ``True`` row order is checked; default ``False``.
    """

    kind: Literal["result_set"] = "result_set"
    expected_rows: list[dict[str, Any]]
    order_sensitive: bool = False

    model_config = ConfigDict(frozen=True)


class SubstringContainsExpectation(BaseModel):
    """Assert that the agent's natural-language response contains all listed substrings.

    Comparison is case-insensitive.

    Attributes:
        kind: Discriminator literal ``"substring_contains"``.
        must_contain: Substrings that must all appear in the response.
    """

    kind: Literal["substring_contains"] = "substring_contains"
    must_contain: list[str]

    model_config = ConfigDict(frozen=True)

    @field_validator("must_contain")
    @classmethod
    def _must_contain_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("must_contain must have at least one entry")
        return v


class LLMJudgeExpectation(BaseModel):
    """Assert quality of the response via an LLM judge using a rubric (B-2).

    Attributes:
        kind: Discriminator literal ``"llm_judge"``.
        rubric: Evaluation rubric passed to the LLM judge.
    """

    kind: Literal["llm_judge"] = "llm_judge"
    rubric: str

    model_config = ConfigDict(frozen=True)

    @field_validator("rubric")
    @classmethod
    def _rubric_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("rubric must be a non-empty string")
        return v


ExpectedAnswer = Annotated[
    IntentOnlyExpectation
    | SQLEquivalentExpectation
    | ResultSetExpectation
    | SubstringContainsExpectation
    | LLMJudgeExpectation,
    Field(discriminator="kind"),
]
"""Discriminated union of all supported expectation types."""


# ---------------------------------------------------------------------------
# EvalCase
# ---------------------------------------------------------------------------


class EvalCase(BaseModel):
    """A single evaluation case pairing a natural-language question with expected outputs.

    Attributes:
        id: Globally unique slug matching ``^[a-z0-9_-]+$``.
        question: The user's natural-language input to the agent.
        domain: Ontology domain (must be a known domain under ``ontologies/``).
        expected_intent: The intent the agent should classify.
        expected: Typed expectation about the agent's output.
        tags: Set of lowercase tags (e.g. ``"smoke"``, ``"regression"``).
        skip_reason: When set, runners must skip this case with this reason.
    """

    id: str
    question: str
    domain: str
    expected_intent: Literal["READ", "WRITE", "ANALYZE", "DECIDE", "OPERATE", "UNCLEAR"]
    expected: ExpectedAnswer
    tags: frozenset[str] = frozenset()
    skip_reason: str | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not v:
            raise ValueError("id must be non-empty")
        if not _ID_PATTERN.match(v):
            raise ValueError(
                f"id must match ^[a-z0-9_-]+$ but got: {v!r}"
            )
        return v

    @field_validator("domain")
    @classmethod
    def _validate_domain(cls, v: str) -> str:
        if v not in _VALID_DOMAINS:
            raise ValueError(
                f"domain must be one of {sorted(_VALID_DOMAINS)}, got: {v!r}"
            )
        return v

    @field_validator("tags", mode="before")
    @classmethod
    def _validate_tags(cls, v: Any) -> frozenset[str]:
        if isinstance(v, frozenset):
            tags = v
        elif isinstance(v, (list, tuple, set)):
            tags = frozenset(v)
        elif v is None:
            return frozenset()
        else:
            tags = frozenset({v})
        for tag in tags:
            if not isinstance(tag, str):
                raise ValueError(f"tag must be a string, got: {tag!r}")
            if not _TAG_PATTERN.match(tag):
                raise ValueError(
                    f"tag must be lowercase alphanumeric+_- only, got: {tag!r}"
                )
        return tags
