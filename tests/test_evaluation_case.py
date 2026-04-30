"""Unit tests for EvalCase and ExpectedAnswer data models (Phase B-1)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.evaluation import (
    EvalCase,
    IntentOnlyExpectation,
    LLMJudgeExpectation,
    ResultSetExpectation,
    SQLEquivalentExpectation,
    SubstringContainsExpectation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_case(**overrides) -> dict:
    base = {
        "id": "ecommerce-001",
        "question": "How many buyers are there?",
        "domain": "ecommerce",
        "expected_intent": "READ",
        "expected": {"kind": "intent_only", "expected_intent": "READ"},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# EvalCase construction
# ---------------------------------------------------------------------------


def test_eval_case_minimal_construction():
    """EvalCase builds successfully with only required fields and correct defaults."""
    case = EvalCase.model_validate(_minimal_case())
    assert case.id == "ecommerce-001"
    assert case.expected_intent == "READ"
    assert case.tags == frozenset()
    assert case.skip_reason is None
    assert isinstance(case.expected, IntentOnlyExpectation)


def test_eval_case_id_validates_format():
    """EvalCase rejects ids that contain uppercase letters or special chars."""
    good = ["abc", "abc-123", "a_b_c", "x1y2z3"]
    for gid in good:
        case = EvalCase.model_validate(_minimal_case(id=gid))
        assert case.id == gid

    bad = ["ABC", "abc 123", "abc.def", "abc/def", ""]
    for bid in bad:
        with pytest.raises(ValidationError):
            EvalCase.model_validate(_minimal_case(id=bid))


def test_eval_case_domain_must_be_known():
    """EvalCase rejects domains not in the whitelist."""
    for valid_domain in ("ecommerce", "finance", "healthcare", "manufacturing", "retail", "education"):
        case = EvalCase.model_validate(_minimal_case(domain=valid_domain))
        assert case.domain == valid_domain

    with pytest.raises(ValidationError):
        EvalCase.model_validate(_minimal_case(domain="unknown_domain"))

    with pytest.raises(ValidationError):
        EvalCase.model_validate(_minimal_case(domain="ECOMMERCE"))


def test_eval_case_tags_lowercase_only():
    """EvalCase accepts lowercase tags and rejects uppercase or spaces."""
    case = EvalCase.model_validate(_minimal_case(tags=["smoke", "regression"]))
    assert case.tags == frozenset({"smoke", "regression"})

    with pytest.raises(ValidationError):
        EvalCase.model_validate(_minimal_case(tags=["Smoke"]))

    with pytest.raises(ValidationError):
        EvalCase.model_validate(_minimal_case(tags=["bad tag"]))


def test_eval_case_frozen_immutable():
    """EvalCase is frozen — assigning to any field must raise."""
    case = EvalCase.model_validate(_minimal_case())
    with pytest.raises(Exception):  # pydantic raises ValidationError or TypeError
        case.id = "new-id"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Expectation round-trips
# ---------------------------------------------------------------------------


def test_intent_only_expectation_round_trip():
    """IntentOnlyExpectation serialises and deserialises via model_dump/validate."""
    exp = IntentOnlyExpectation(expected_intent="ANALYZE")
    data = exp.model_dump()
    assert data["kind"] == "intent_only"
    assert data["expected_intent"] == "ANALYZE"

    restored = IntentOnlyExpectation.model_validate(data)
    assert restored == exp


def test_sql_equivalent_expectation_required_fields():
    """SQLEquivalentExpectation requires expected_sql."""
    exp = SQLEquivalentExpectation(expected_sql="SELECT 1")
    assert exp.expected_sql == "SELECT 1"

    with pytest.raises(ValidationError):
        SQLEquivalentExpectation.model_validate({"kind": "sql_equivalent"})


def test_result_set_expectation_default_order_insensitive():
    """ResultSetExpectation defaults order_sensitive to False."""
    exp = ResultSetExpectation(expected_rows=[{"count": 10}])
    assert exp.order_sensitive is False


def test_substring_contains_expectation_must_contain_nonempty():
    """SubstringContainsExpectation rejects an empty must_contain list."""
    exp = SubstringContainsExpectation(must_contain=["buyer", "total"])
    assert exp.must_contain == ["buyer", "total"]

    with pytest.raises(ValidationError):
        SubstringContainsExpectation(must_contain=[])


def test_llm_judge_expectation_rubric_required():
    """LLMJudgeExpectation requires a non-empty rubric string."""
    exp = LLMJudgeExpectation(rubric="Score 1-5 for relevance.")
    assert "relevance" in exp.rubric

    with pytest.raises(ValidationError):
        LLMJudgeExpectation(rubric="   ")  # whitespace-only


def test_discriminated_union_dispatches_on_kind():
    """feed each kind into EvalCase.expected and verify the correct subclass is returned."""
    kinds = [
        ({"kind": "intent_only", "expected_intent": "READ"}, IntentOnlyExpectation),
        ({"kind": "sql_equivalent", "expected_sql": "SELECT 1"}, SQLEquivalentExpectation),
        ({"kind": "result_set", "expected_rows": [{"n": 1}]}, ResultSetExpectation),
        ({"kind": "substring_contains", "must_contain": ["x"]}, SubstringContainsExpectation),
        ({"kind": "llm_judge", "rubric": "score it"}, LLMJudgeExpectation),
    ]
    for expected_dict, expected_cls in kinds:
        case = EvalCase.model_validate(_minimal_case(expected=expected_dict))
        assert isinstance(case.expected, expected_cls), (
            f"Expected {expected_cls.__name__} for kind={expected_dict['kind']!r}"
        )
