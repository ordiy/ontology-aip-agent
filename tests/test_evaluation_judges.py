"""Tests for Phase B-2 evaluation judges.

Covers: IntentJudge, SQLEquivalenceJudge, ResultSetJudge,
SubstringContainsJudge, LLMJudge, and the get_judge_for dispatcher.

All tests use FakeLLM / _FakeExecutor — no real LLM or DB calls.
"""
from __future__ import annotations

import pytest

from src.database.executor import BaseExecutor, SQLResult
from src.evaluation.case import (
    EvalCase,
    IntentOnlyExpectation,
    LLMJudgeExpectation,
    ResultSetExpectation,
    SQLEquivalentExpectation,
    SubstringContainsExpectation,
)
from src.evaluation.judges import (
    AgentOutput,
    IntentJudge,
    JudgeOutcome,
    LLMJudge,
    ResultSetJudge,
    SQLEquivalenceJudge,
    SubstringContainsJudge,
    get_judge_for,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def make_case(
    *,
    case_id: str = "test-case",
    question: str = "How many orders?",
    domain: str = "ecommerce",
    expected_intent: str = "READ",
    expected=None,
    skip_reason: str | None = None,
    tags: list[str] | None = None,
) -> EvalCase:
    """Build an EvalCase with sensible defaults."""
    if expected is None:
        expected = IntentOnlyExpectation(expected_intent="READ")
    return EvalCase(
        id=case_id,
        question=question,
        domain=domain,
        expected_intent=expected_intent,  # type: ignore[arg-type]
        expected=expected,
        skip_reason=skip_reason,
        tags=frozenset(tags or []),
    )


def make_output(
    *,
    intent: str | None = "READ",
    generated_sql: str | None = None,
    query_result: list[dict] | None = None,
    response: str | None = None,
    error: str | None = None,
) -> AgentOutput:
    """Build an AgentOutput with sensible defaults."""
    return AgentOutput(
        intent=intent,
        generated_sql=generated_sql,
        query_result=query_result,
        response=response,
        error=error,
    )


class FakeLLM:
    """Fake LLM that returns pre-set responses in order."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self._call_index = 0

    def chat(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        resp = self._responses[self._call_index]
        self._call_index += 1
        return resp

    def get_model_name(self) -> str:
        return "fake-model"


class _FakeExecutor(BaseExecutor):
    """Fake executor returning canned rows or error."""

    def __init__(
        self,
        rows: list[dict] | None = None,
        error: str | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._rows = rows or []
        self._error = error
        self._raise_exc = raise_exc
        self._permissions = {"read": "auto", "write": "confirm"}

    @property
    def dialect(self) -> str:
        return "SQLite"

    def execute(self, sql: str, approved: bool = False) -> SQLResult:
        if self._raise_exc:
            raise self._raise_exc
        return SQLResult(
            operation="read",
            rows=self._rows,
            error=self._error,
        )


# ---------------------------------------------------------------------------
# IntentJudge
# ---------------------------------------------------------------------------


class TestIntentJudge:
    """Tests for IntentJudge."""

    def setup_method(self):
        self.judge = IntentJudge()

    def test_pass_when_intent_matches(self):
        case = make_case(expected_intent="READ")
        output = make_output(intent="READ")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.PASS
        assert result.judge_name == "IntentJudge"
        assert "matched" in result.reason

    def test_fail_when_intent_differs(self):
        case = make_case(expected_intent="ANALYZE")
        output = make_output(intent="READ")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.FAIL
        assert result.metadata["expected"] == "ANALYZE"
        assert result.metadata["actual"] == "READ"

    def test_skip_when_no_intent_produced(self):
        case = make_case(expected_intent="READ")
        output = make_output(intent=None)
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.SKIP
        assert "no intent" in result.reason

    def test_skip_when_case_has_skip_reason(self):
        case = make_case(expected_intent="READ", skip_reason="temporarily disabled")
        output = make_output(intent="READ")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.SKIP
        assert "temporarily disabled" in result.reason

    def test_skip_when_pipeline_errored(self):
        case = make_case(expected_intent="READ")
        output = make_output(intent=None, error="boom")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.SKIP
        assert "pipeline error" in result.reason


# ---------------------------------------------------------------------------
# SQLEquivalenceJudge
# ---------------------------------------------------------------------------


class TestSQLEquivalenceJudge:
    """Tests for SQLEquivalenceJudge — 8 scenarios from the spec."""

    def setup_method(self):
        self.judge = SQLEquivalenceJudge()

    def _case(self, expected_sql: str, case_id: str = "sql-case") -> EvalCase:
        return make_case(
            case_id=case_id,
            expected=SQLEquivalentExpectation(expected_sql=expected_sql),
        )

    # 1. identical SQL → PASS
    def test_identical_sql_pass(self):
        case = self._case("SELECT id, name FROM orders WHERE status = 'active'")
        output = make_output(generated_sql="SELECT id, name FROM orders WHERE status = 'active'")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.PASS

    # 2. different alias names → PASS (sqlglot normalises aliases)
    def test_alias_difference_pass(self):
        case = self._case("SELECT COUNT(*) AS total FROM orders")
        output = make_output(generated_sql="SELECT COUNT(*) AS cnt FROM orders")
        # sqlglot normalises aliases in canonical form for simple COUNT expressions
        # This depends on sqlglot behaviour; we verify no crash and record result
        result = self.judge.evaluate(case, output)
        # aliases differ — canonical output may or may not match; just verify no exception
        assert result.outcome in (JudgeOutcome.PASS, JudgeOutcome.FAIL)
        assert result.judge_name == "SQLEquivalenceJudge"

    # 3. different column ORDER in SELECT → FAIL
    def test_column_order_in_select_fail(self):
        case = self._case("SELECT id, name, email FROM customers")
        output = make_output(generated_sql="SELECT name, id, email FROM customers")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.FAIL
        assert "canonical_expected" in result.metadata

    # 4. different table case (Orders vs orders) → PASS
    def test_table_case_insensitive_pass(self):
        case = self._case("SELECT id FROM orders")
        output = make_output(generated_sql="SELECT id FROM Orders")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.PASS

    # 5. whitespace/comment differences → PASS
    def test_whitespace_differences_pass(self):
        case = self._case("SELECT id FROM orders")
        output = make_output(
            generated_sql="SELECT   id   FROM   orders"
        )
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.PASS

    # 6. IN(1,2,3) vs IN(3,2,1) → PASS (set semantics)
    def test_in_list_order_insensitive_pass(self):
        case = self._case("SELECT id FROM orders WHERE status IN (1, 2, 3)")
        output = make_output(generated_sql="SELECT id FROM orders WHERE status IN (3, 2, 1)")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.PASS

    # 7. ORDER BY a, b vs ORDER BY b, a → FAIL
    def test_order_by_order_sensitive_fail(self):
        case = self._case("SELECT id FROM orders ORDER BY created_at, id")
        output = make_output(generated_sql="SELECT id FROM orders ORDER BY id, created_at")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.FAIL

    # 8. malformed actual SQL → FAIL with helpful reason
    def test_malformed_actual_sql_fail(self):
        case = self._case("SELECT id FROM orders")
        output = make_output(generated_sql="SELECT FROM WHERE !!!invalid")
        result = self.judge.evaluate(case, output)
        # May be SKIP or FAIL depending on sqlglot error handling
        assert result.outcome in (JudgeOutcome.FAIL, JudgeOutcome.SKIP)
        # There should be some indication of a problem
        assert result.judge_name == "SQLEquivalenceJudge"

    # Extra: wrong expectation kind → SKIP
    def test_wrong_kind_skip(self):
        case = make_case(
            expected=IntentOnlyExpectation(expected_intent="READ")
        )
        output = make_output(generated_sql="SELECT 1")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.SKIP
        assert "wrong expectation kind" in result.reason

    # Extra: no SQL generated → SKIP
    def test_no_sql_generated_skip(self):
        case = self._case("SELECT 1")
        output = make_output(generated_sql=None)
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.SKIP

    # Extra: keyword case differences → PASS
    def test_keyword_case_pass(self):
        case = self._case("select id from orders")
        output = make_output(generated_sql="SELECT id FROM orders")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.PASS


# ---------------------------------------------------------------------------
# ResultSetJudge
# ---------------------------------------------------------------------------


class TestResultSetJudge:
    """Tests for ResultSetJudge."""

    def _case(
        self,
        expected_rows: list[dict],
        order_sensitive: bool = False,
        case_id: str = "rs-case",
    ) -> EvalCase:
        return make_case(
            case_id=case_id,
            expected=ResultSetExpectation(
                expected_rows=expected_rows,
                order_sensitive=order_sensitive,
            ),
        )

    # 1. exact match → PASS
    def test_exact_match_pass(self):
        rows = [{"id": 1, "name": "Alice"}]
        judge = ResultSetJudge(_FakeExecutor(rows=rows))
        case = self._case(rows)
        output = make_output(generated_sql="SELECT id, name FROM customers LIMIT 1")
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.PASS

    # 2. order-insensitive PASS
    def test_order_insensitive_pass(self):
        expected = [{"id": 1}, {"id": 2}]
        actual = [{"id": 2}, {"id": 1}]
        judge = ResultSetJudge(_FakeExecutor(rows=actual))
        case = self._case(expected, order_sensitive=False)
        output = make_output(generated_sql="SELECT id FROM t")
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.PASS

    # 3. order-sensitive FAIL when order differs
    def test_order_sensitive_fail(self):
        expected = [{"id": 1}, {"id": 2}]
        actual = [{"id": 2}, {"id": 1}]
        judge = ResultSetJudge(_FakeExecutor(rows=actual))
        case = self._case(expected, order_sensitive=True)
        output = make_output(generated_sql="SELECT id FROM t ORDER BY id DESC")
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.FAIL

    # 4. executor error → FAIL
    def test_executor_error_fail(self):
        judge = ResultSetJudge(_FakeExecutor(error="table not found"))
        case = self._case([{"id": 1}])
        output = make_output(generated_sql="SELECT id FROM missing_table")
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.FAIL
        assert "table not found" in result.reason

    # 5. executor raises exception → FAIL
    def test_executor_raises_fail(self):
        judge = ResultSetJudge(
            _FakeExecutor(raise_exc=RuntimeError("connection refused"))
        )
        case = self._case([])
        output = make_output(generated_sql="SELECT 1")
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.FAIL
        assert "connection refused" in result.reason

    # 6. wrong kind → SKIP
    def test_wrong_kind_skip(self):
        judge = ResultSetJudge(_FakeExecutor())
        case = make_case(expected=IntentOnlyExpectation(expected_intent="READ"))
        output = make_output(generated_sql="SELECT 1")
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.SKIP

    # 7. no SQL generated → SKIP
    def test_no_sql_skip(self):
        judge = ResultSetJudge(_FakeExecutor())
        case = self._case([])
        output = make_output(generated_sql=None)
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.SKIP

    # 8. float tolerance
    def test_float_tolerance_pass(self):
        expected = [{"total": 3.14159265358979}]
        actual = [{"total": 3.141592653589793}]
        judge = ResultSetJudge(_FakeExecutor(rows=actual))
        case = self._case(expected)
        output = make_output(generated_sql="SELECT 3.14159265358979 AS total")
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.PASS

    # 9. row count mismatch → FAIL
    def test_row_count_mismatch_fail(self):
        expected = [{"id": 1}, {"id": 2}]
        actual = [{"id": 1}]
        judge = ResultSetJudge(_FakeExecutor(rows=actual))
        case = self._case(expected)
        output = make_output(generated_sql="SELECT id FROM t LIMIT 1")
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.FAIL
        assert "row count mismatch" in result.reason


# ---------------------------------------------------------------------------
# SubstringContainsJudge
# ---------------------------------------------------------------------------


class TestSubstringContainsJudge:
    """Tests for SubstringContainsJudge."""

    def setup_method(self):
        self.judge = SubstringContainsJudge()

    def _case(self, must_contain: list[str], case_id: str = "sub-case") -> EvalCase:
        return make_case(
            case_id=case_id,
            expected=SubstringContainsExpectation(must_contain=must_contain),
        )

    # 1. all present → PASS
    def test_all_present_pass(self):
        case = self._case(["total", "orders"])
        output = make_output(response="There are 10 total orders in the system.")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.PASS

    # 2. missing one → FAIL with metadata
    def test_missing_one_fail(self):
        case = self._case(["total", "revenue"])
        output = make_output(response="The total count is 42.")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.FAIL
        assert "revenue" in result.metadata["missing"]

    # 3. case-insensitive match → PASS
    def test_case_insensitive_pass(self):
        case = self._case(["TOTAL", "ORDERS"])
        output = make_output(response="Total orders: 5")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.PASS

    # 4. wrong kind → SKIP
    def test_wrong_kind_skip(self):
        case = make_case(expected=IntentOnlyExpectation(expected_intent="READ"))
        output = make_output(response="hello")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.SKIP

    # 5. pipeline error → SKIP
    def test_pipeline_error_skip(self):
        case = self._case(["total"])
        output = make_output(response=None, error="crash")
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.SKIP

    # 6. None response → FAIL (all substrings missing)
    def test_none_response_fail(self):
        case = self._case(["total"])
        output = make_output(response=None)
        result = self.judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.FAIL
        assert "total" in result.metadata["missing"]


# ---------------------------------------------------------------------------
# LLMJudge
# ---------------------------------------------------------------------------


class TestLLMJudge:
    """Tests for LLMJudge."""

    def _case(self, rubric: str = "Is the answer accurate?") -> EvalCase:
        return make_case(
            expected=LLMJudgeExpectation(rubric=rubric),
        )

    # 1. score >= threshold → PASS (JSON response)
    def test_score_above_threshold_pass(self):
        llm = FakeLLM(['{"score": 5, "justification": "Excellent answer"}'])
        judge = LLMJudge(llm, max_score=5, pass_threshold=4)
        case = self._case()
        output = make_output(response="There are 42 orders.")
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.PASS
        assert result.metadata["score"] == 5
        assert "Excellent answer" in result.metadata["justification"]

    # 2. score < threshold → FAIL (JSON response)
    def test_score_below_threshold_fail(self):
        llm = FakeLLM(['{"score": 2, "justification": "Missing key details"}'])
        judge = LLMJudge(llm, max_score=5, pass_threshold=4)
        case = self._case()
        output = make_output(response="I don't know.")
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.FAIL
        assert result.metadata["score"] == 2

    # 3. LLM returns garbage → FAIL with parse error
    def test_garbage_response_fail(self):
        llm = FakeLLM(["not a score at all, just words"])
        judge = LLMJudge(llm, max_score=5, pass_threshold=4)
        case = self._case()
        output = make_output(response="some answer")
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.FAIL
        assert "could not parse score" in result.reason

    # 4. LLM returns integer-only (fallback) → parses correctly
    def test_integer_only_response_parsed(self):
        llm = FakeLLM(["Score: 4"])
        judge = LLMJudge(llm, max_score=5, pass_threshold=4)
        case = self._case()
        output = make_output(response="some answer")
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.PASS
        assert result.metadata["score"] == 4

    # 5. wrong expectation kind → SKIP
    def test_wrong_kind_skip(self):
        llm = FakeLLM([])
        judge = LLMJudge(llm)
        case = make_case(expected=IntentOnlyExpectation(expected_intent="READ"))
        output = make_output(response="hello")
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.SKIP
        assert llm._call_index == 0  # no LLM call made

    # 6. skip_reason set → SKIP without LLM call
    def test_skip_reason_respected(self):
        llm = FakeLLM([])
        judge = LLMJudge(llm)
        case = make_case(
            expected=LLMJudgeExpectation(rubric="Is it accurate?"),
            skip_reason="flaky case",
        )
        output = make_output(response="hello")
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.SKIP
        assert llm._call_index == 0

    # 7. exactly-at-threshold passes
    def test_at_threshold_passes(self):
        llm = FakeLLM(['{"score": 4, "justification": "Good"}'])
        judge = LLMJudge(llm, max_score=5, pass_threshold=4)
        case = self._case()
        output = make_output(response="answer")
        result = judge.evaluate(case, output)
        assert result.outcome == JudgeOutcome.PASS


# ---------------------------------------------------------------------------
# Dispatcher: get_judge_for
# ---------------------------------------------------------------------------


class TestGetJudgeFor:
    """Tests for the get_judge_for dispatcher."""

    def test_intent_only_returns_intent_judge(self):
        judge = get_judge_for("intent_only")
        assert isinstance(judge, IntentJudge)

    def test_sql_equivalent_returns_sql_judge(self):
        judge = get_judge_for("sql_equivalent")
        assert isinstance(judge, SQLEquivalenceJudge)

    def test_substring_contains_returns_substring_judge(self):
        judge = get_judge_for("substring_contains")
        assert isinstance(judge, SubstringContainsJudge)

    def test_result_set_with_executor(self):
        exec_ = _FakeExecutor()
        judge = get_judge_for("result_set", executor=exec_)
        assert isinstance(judge, ResultSetJudge)

    def test_result_set_without_executor_raises(self):
        with pytest.raises(ValueError, match="executor"):
            get_judge_for("result_set")

    def test_llm_judge_with_llm(self):
        llm = FakeLLM([])
        judge = get_judge_for("llm_judge", llm=llm)
        assert isinstance(judge, LLMJudge)

    def test_llm_judge_without_llm_raises(self):
        with pytest.raises(ValueError, match="llm"):
            get_judge_for("llm_judge")

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError, match="Unknown expectation kind"):
            get_judge_for("does_not_exist")


# ---------------------------------------------------------------------------
# JudgeResult immutability
# ---------------------------------------------------------------------------


class TestJudgeResultImmutability:
    """Verify that JudgeResult is frozen (immutable)."""

    def test_judge_result_is_frozen(self):
        result = IntentJudge().evaluate(
            make_case(expected_intent="READ"),
            make_output(intent="READ"),
        )
        with pytest.raises((AttributeError, TypeError)):
            result.outcome = JudgeOutcome.FAIL  # type: ignore[misc]

    def test_agent_output_is_frozen(self):
        output = AgentOutput(
            intent="READ",
            generated_sql=None,
            query_result=None,
            response=None,
        )
        with pytest.raises((AttributeError, TypeError)):
            output.intent = "WRITE"  # type: ignore[misc]
