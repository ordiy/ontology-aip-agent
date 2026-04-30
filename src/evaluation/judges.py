"""Judges for the evaluation framework (Phase B-2).

Each judge is a pure callable that receives an ``EvalCase`` and an
``AgentOutput`` and returns an immutable ``JudgeResult``.  No I/O side-effects;
all external interactions (SQL execution, LLM calls) are injected as
dependencies.

Cost ordering used by the Phase B-3 runner:

+---------------------+----------+---------------------------------------+
| Judge               | Cost     | Used for                              |
+---------------------+----------+---------------------------------------+
| IntentJudge         | 0        | intent classification only            |
| SQLEquivalenceJudge | 0        | sqlglot canonical string comparison   |
| SubstringContains   | 0        | response substring matching           |
| ResultSetJudge      | 1 SQL    | execute SQL, compare rows             |
| LLMJudge            | 1 LLM    | rubric-based answer quality scoring   |
+---------------------+----------+---------------------------------------+

Phase B-3 will call ``get_judge_for(case.expected.kind, executor=..., llm=...)``
and also always run ``IntentJudge`` independently as a base check on every case.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

import sqlglot
import sqlglot.expressions as exp

from src.database.executor import BaseExecutor
from src.evaluation.case import EvalCase
from src.llm.base import LLMClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core result types
# ---------------------------------------------------------------------------


class JudgeOutcome(str, Enum):
    """Outcome of a single judge evaluation."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"  # case skipped or judge cannot evaluate


@dataclass(frozen=True)
class JudgeResult:
    """Immutable result produced by a judge.

    Attributes:
        case_id: ID of the evaluated ``EvalCase``.
        outcome: PASS, FAIL, or SKIP.
        reason: Short human-readable explanation.
        judge_name: Name of the judge that produced this result.
        metadata: Judge-specific extras for debugging (e.g. canonical SQL strings).
    """

    case_id: str
    outcome: JudgeOutcome
    reason: str
    judge_name: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentOutput:
    """Snapshot of what the agent pipeline produced for a single case.

    Populated by the Phase B-3 runner from ``AgentState``.

    Attributes:
        intent: Intent label emitted by ``classify_intent``.
        generated_sql: Post-rewrite SQL, if any.
        query_result: Rows returned by the executor, if execution happened.
        response: Final natural-language answer.
        error: Populated if the pipeline raised an exception.
        masked_columns: Mapping of original → masked column names (RBAC).
    """

    intent: str | None
    generated_sql: str | None
    query_result: list[dict] | None
    response: str | None
    error: str | None = None
    masked_columns: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Judge Protocol
# ---------------------------------------------------------------------------


class Judge(Protocol):
    """Pure function over (case, agent_output) → JudgeResult."""

    name: str

    def evaluate(self, case: EvalCase, agent_output: AgentOutput) -> JudgeResult:
        """Evaluate the agent output against the case expectations.

        Args:
            case: The evaluation case with question and expected answer.
            agent_output: What the agent actually produced.

        Returns:
            An immutable ``JudgeResult``.
        """
        ...


# ---------------------------------------------------------------------------
# 2.1  IntentJudge
# ---------------------------------------------------------------------------


class IntentJudge:
    """Zero-cost judge that checks only the intent classification.

    Used as a universal base check — Phase B-3 runs this on *every* case
    regardless of the ``expected.kind``.
    """

    name = "IntentJudge"

    def evaluate(self, case: EvalCase, output: AgentOutput) -> JudgeResult:
        """Compare ``output.intent`` with ``case.expected_intent``.

        Args:
            case: The evaluation case.
            output: Agent output to evaluate.

        Returns:
            SKIP if no intent was produced; PASS if it matches; FAIL otherwise.
        """
        if case.skip_reason:
            return JudgeResult(case.id, JudgeOutcome.SKIP, case.skip_reason, self.name)
        if output.error:
            return JudgeResult(
                case.id, JudgeOutcome.SKIP, f"pipeline error: {output.error}", self.name
            )
        if output.intent is None:
            return JudgeResult(
                case.id, JudgeOutcome.SKIP, "no intent produced", self.name
            )
        if output.intent == case.expected_intent:
            return JudgeResult(case.id, JudgeOutcome.PASS, "intent matched", self.name)
        return JudgeResult(
            case.id,
            JudgeOutcome.FAIL,
            f"expected {case.expected_intent!r}, got {output.intent!r}",
            self.name,
            metadata={"expected": case.expected_intent, "actual": output.intent},
        )


# ---------------------------------------------------------------------------
# 2.2  SQLEquivalenceJudge
# ---------------------------------------------------------------------------


def _canonical_sql(sql: str) -> str:
    """Return a canonical form of *sql* using sqlglot.

    Approach: parse with sqlglot, then re-generate with
    ``dialect="sqlite", normalize=True, identify=False`` to produce a
    deterministic lowercase string.  This handles:
    - Whitespace/comment differences
    - Keyword case differences (SELECT vs select)
    - Table/column identifier case differences

    Args:
        sql: Raw SQL string.

    Returns:
        Canonical SQL string.

    Raises:
        sqlglot.errors.SqlglotError: If parsing fails.
    """
    tree = sqlglot.parse_one(sql, error_level=sqlglot.ErrorLevel.RAISE)
    return tree.sql(dialect="sqlite", normalize=True, identify=False)


def _normalize_in_args(node: exp.Expression) -> exp.Expression:
    """Sort IN(...) argument lists for set-semantics comparison.

    Mutates a *copy* of the node tree — safe to call before comparison.

    Args:
        node: Root expression to normalise.

    Returns:
        A new expression tree with IN argument lists sorted.
    """
    for in_expr in node.find_all(exp.In):
        if in_expr.args.get("query") is None:
            # It's a literal list — sort by string representation
            exprs = in_expr.args.get("expressions", [])
            if exprs:
                sorted_exprs = sorted(exprs, key=lambda e: e.sql(dialect="sqlite"))
                in_expr.set("expressions", sorted_exprs)
    return node


class SQLEquivalenceJudge:
    """Zero-cost judge that checks SQL semantic equivalence via sqlglot.

    Implementation strategy:
    1. Parse both expected and actual SQL with sqlglot.
    2. Normalise IN(...) argument lists to set semantics (order-insensitive).
    3. Re-generate both as canonical strings and compare.

    This covers:
    - Whitespace/comment/keyword-case differences → PASS
    - Table/column case differences → PASS
    - IN(1,2,3) vs IN(3,2,1) → PASS
    - Different column projections → FAIL
    - Different ORDER BY sequences → FAIL

    Alias differences in SELECT are *not* ignored — if the test expectation
    uses ``AS total`` and the output omits the alias, they differ.
    """

    name = "SQLEquivalenceJudge"

    def evaluate(self, case: EvalCase, output: AgentOutput) -> JudgeResult:
        """Compare generated SQL with expected SQL.

        Args:
            case: Must have ``expected.kind == "sql_equivalent"``.
            output: Agent output; ``generated_sql`` is used.

        Returns:
            SKIP if wrong kind or unparseable; PASS/FAIL based on comparison.
        """
        if case.expected.kind != "sql_equivalent":
            return JudgeResult(
                case.id, JudgeOutcome.SKIP, "wrong expectation kind", self.name
            )
        if case.skip_reason:
            return JudgeResult(case.id, JudgeOutcome.SKIP, case.skip_reason, self.name)
        if output.error:
            return JudgeResult(
                case.id, JudgeOutcome.SKIP, f"pipeline error: {output.error}", self.name
            )
        if output.generated_sql is None:
            return JudgeResult(
                case.id, JudgeOutcome.SKIP, "no SQL generated", self.name
            )

        expected_sql = case.expected.expected_sql
        actual_sql = output.generated_sql

        # Parse expected SQL — this should never fail for well-authored cases
        try:
            expected_tree = sqlglot.parse_one(
                expected_sql, error_level=sqlglot.ErrorLevel.RAISE
            )
        except Exception as exc:
            return JudgeResult(
                case.id,
                JudgeOutcome.SKIP,
                f"cannot parse expected SQL: {exc}",
                self.name,
            )

        # Parse actual SQL — failures here are agent bugs → FAIL
        try:
            actual_tree = sqlglot.parse_one(
                actual_sql, error_level=sqlglot.ErrorLevel.RAISE
            )
        except Exception as exc:
            return JudgeResult(
                case.id,
                JudgeOutcome.FAIL,
                f"cannot parse generated SQL: {exc}",
                self.name,
                metadata={"parse_error": str(exc), "actual_sql": actual_sql},
            )

        # Apply IN-list normalisation (set semantics)
        expected_tree = _normalize_in_args(expected_tree)
        actual_tree = _normalize_in_args(actual_tree)

        canonical_expected = expected_tree.sql(
            dialect="sqlite", normalize=True, identify=False
        )
        canonical_actual = actual_tree.sql(
            dialect="sqlite", normalize=True, identify=False
        )

        logger.debug(
            "SQLEquivalenceJudge case=%s canonical_expected=%r canonical_actual=%r",
            case.id,
            canonical_expected,
            canonical_actual,
        )

        if canonical_expected == canonical_actual:
            return JudgeResult(
                case.id,
                JudgeOutcome.PASS,
                "SQL equivalent",
                self.name,
                metadata={
                    "canonical_expected": canonical_expected,
                    "canonical_actual": canonical_actual,
                },
            )
        return JudgeResult(
            case.id,
            JudgeOutcome.FAIL,
            "SQL not equivalent",
            self.name,
            metadata={
                "canonical_expected": canonical_expected,
                "canonical_actual": canonical_actual,
            },
        )


# ---------------------------------------------------------------------------
# 2.3  ResultSetJudge
# ---------------------------------------------------------------------------


def _stable_row_key(row: dict) -> str:
    """Produce a stable sort key from a row dict.

    Args:
        row: A result-set row with arbitrary keys.

    Returns:
        A deterministic string representation of the row.
    """
    return str(sorted((str(k).lower(), v) for k, v in row.items()))


def _normalize_row(row: dict) -> dict:
    """Lowercase all keys in a row dict.

    Args:
        row: Input row.

    Returns:
        Row with all keys lowercased.
    """
    return {str(k).lower(): v for k, v in row.items()}


def _rows_equal(
    actual: list[dict], expected: list[dict], order_sensitive: bool
) -> tuple[bool, str]:
    """Compare two result sets.

    Args:
        actual: Rows returned by the executor.
        expected: Rows defined in the test case.
        order_sensitive: When False, row order is ignored.

    Returns:
        Tuple of (match: bool, reason: str).
    """
    actual_norm = [_normalize_row(r) for r in actual]
    expected_norm = [_normalize_row(r) for r in expected]

    if len(actual_norm) != len(expected_norm):
        return (
            False,
            f"row count mismatch: expected {len(expected_norm)}, got {len(actual_norm)}",
        )

    if not order_sensitive:
        actual_norm = sorted(actual_norm, key=_stable_row_key)
        expected_norm = sorted(expected_norm, key=_stable_row_key)

    for i, (a_row, e_row) in enumerate(zip(actual_norm, expected_norm)):
        if a_row.keys() != e_row.keys():
            return (
                False,
                f"row {i} column mismatch: expected keys {sorted(e_row.keys())}, "
                f"got {sorted(a_row.keys())}",
            )
        for key in e_row:
            a_val = a_row[key]
            e_val = e_row[key]
            if isinstance(e_val, float) or isinstance(a_val, float):
                try:
                    if not math.isclose(float(a_val), float(e_val), rel_tol=1e-9):
                        return (
                            False,
                            f"row {i} column {key!r}: expected {e_val}, got {a_val}",
                        )
                except (TypeError, ValueError):
                    if a_val != e_val:
                        return (
                            False,
                            f"row {i} column {key!r}: expected {e_val}, got {a_val}",
                        )
            else:
                if a_val != e_val:
                    return (
                        False,
                        f"row {i} column {key!r}: expected {e_val!r}, got {a_val!r}",
                    )
    return True, "rows matched"


class ResultSetJudge:
    """Judge that executes the generated SQL and compares the result set.

    Attributes:
        name: Judge identifier.
    """

    name = "ResultSetJudge"

    def __init__(self, executor: BaseExecutor) -> None:
        """Initialise with a SQL executor.

        Args:
            executor: Any ``BaseExecutor`` implementation.
        """
        self._executor = executor

    def evaluate(self, case: EvalCase, output: AgentOutput) -> JudgeResult:
        """Execute generated SQL and compare rows against ``case.expected``.

        Args:
            case: Must have ``expected.kind == "result_set"``.
            output: Agent output; ``generated_sql`` is used.

        Returns:
            SKIP if wrong kind or no SQL; FAIL on execution error or mismatch;
            PASS on match.
        """
        if case.expected.kind != "result_set":
            return JudgeResult(
                case.id, JudgeOutcome.SKIP, "wrong expectation kind", self.name
            )
        if case.skip_reason:
            return JudgeResult(case.id, JudgeOutcome.SKIP, case.skip_reason, self.name)
        if output.error:
            return JudgeResult(
                case.id, JudgeOutcome.SKIP, f"pipeline error: {output.error}", self.name
            )
        if output.generated_sql is None:
            return JudgeResult(
                case.id, JudgeOutcome.SKIP, "no SQL generated", self.name
            )

        try:
            sql_result = self._executor.execute(output.generated_sql, approved=True)
        except Exception as exc:
            return JudgeResult(
                case.id,
                JudgeOutcome.FAIL,
                f"executor raised: {exc}",
                self.name,
                metadata={"executor_error": str(exc)},
            )

        if sql_result.error:
            return JudgeResult(
                case.id,
                JudgeOutcome.FAIL,
                f"SQL execution error: {sql_result.error}",
                self.name,
                metadata={"sql_error": sql_result.error},
            )

        actual_rows: list[dict] = sql_result.rows or []
        expected_rows: list[dict] = list(case.expected.expected_rows)

        match, reason = _rows_equal(
            actual_rows, expected_rows, case.expected.order_sensitive
        )
        if match:
            return JudgeResult(case.id, JudgeOutcome.PASS, reason, self.name)
        return JudgeResult(
            case.id,
            JudgeOutcome.FAIL,
            reason,
            self.name,
            metadata={
                "actual_rows": actual_rows,
                "expected_rows": expected_rows,
            },
        )


# ---------------------------------------------------------------------------
# 2.4  SubstringContainsJudge
# ---------------------------------------------------------------------------


class SubstringContainsJudge:
    """Zero-cost judge that checks for required substrings in the response.

    All substring matches are case-insensitive.
    """

    name = "SubstringContainsJudge"

    def evaluate(self, case: EvalCase, output: AgentOutput) -> JudgeResult:
        """Check that all required substrings appear in ``output.response``.

        Args:
            case: Must have ``expected.kind == "substring_contains"``.
            output: Agent output; ``response`` is checked.

        Returns:
            SKIP if wrong kind; FAIL if any substring is missing; PASS if all found.
        """
        if case.expected.kind != "substring_contains":
            return JudgeResult(
                case.id, JudgeOutcome.SKIP, "wrong expectation kind", self.name
            )
        if case.skip_reason:
            return JudgeResult(case.id, JudgeOutcome.SKIP, case.skip_reason, self.name)
        if output.error:
            return JudgeResult(
                case.id, JudgeOutcome.SKIP, f"pipeline error: {output.error}", self.name
            )

        response_lower = (output.response or "").lower()
        missing = [
            s for s in case.expected.must_contain if s.lower() not in response_lower
        ]
        if missing:
            return JudgeResult(
                case.id,
                JudgeOutcome.FAIL,
                f"missing substrings: {missing}",
                self.name,
                metadata={"missing": missing, "must_contain": list(case.expected.must_contain)},
            )
        return JudgeResult(
            case.id,
            JudgeOutcome.PASS,
            "all substrings found",
            self.name,
        )


# ---------------------------------------------------------------------------
# 2.5  LLMJudge
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an evaluation judge.  Score the ANSWER to the QUESTION using the RUBRIC.
Respond with ONLY a JSON object on one line, e.g.: {{"score": 4, "justification": "..."}}
Score must be an integer from 1 to {max_score}.  Do not include any other text.
"""

_SCORE_RE = re.compile(r"\b([1-9][0-9]*)\b")


class LLMJudge:
    """Judge that uses an LLM to score the agent's response against a rubric.

    Attributes:
        name: Judge identifier.
    """

    name = "LLMJudge"

    def __init__(
        self, llm: LLMClient, max_score: int = 5, pass_threshold: int = 4
    ) -> None:
        """Initialise with an LLM client and scoring parameters.

        Args:
            llm: LLM client used for scoring.
            max_score: Upper bound of the scoring scale (default 5).
            pass_threshold: Minimum score to PASS (default 4).
        """
        self._llm = llm
        self._max_score = max_score
        self._pass_threshold = pass_threshold

    def evaluate(self, case: EvalCase, output: AgentOutput) -> JudgeResult:
        """Score the agent's response using an LLM judge.

        Sends a structured prompt; parses score from JSON or falls back to the
        first integer in the response.

        Args:
            case: Must have ``expected.kind == "llm_judge"``.
            output: Agent output; ``response`` is scored.

        Returns:
            SKIP if wrong kind; FAIL if unparseable or below threshold;
            PASS if score >= pass_threshold.
        """
        if case.expected.kind != "llm_judge":
            return JudgeResult(
                case.id, JudgeOutcome.SKIP, "wrong expectation kind", self.name
            )
        if case.skip_reason:
            return JudgeResult(case.id, JudgeOutcome.SKIP, case.skip_reason, self.name)
        if output.error:
            return JudgeResult(
                case.id, JudgeOutcome.SKIP, f"pipeline error: {output.error}", self.name
            )

        rubric = case.expected.rubric
        system = _SYSTEM_PROMPT.format(max_score=self._max_score)
        messages = [
            {
                "role": "user",
                "content": (
                    f"QUESTION: {case.question}\n\n"
                    f"ANSWER: {output.response or '(no response)'}\n\n"
                    f"RUBRIC: {rubric}"
                ),
            }
        ]

        raw_response = self._llm.chat(messages, system_prompt=system, temperature=0.0)
        logger.debug("LLMJudge case=%s raw_response=%r", case.id, raw_response)

        score, justification = self._parse_response(raw_response)

        if score is None:
            return JudgeResult(
                case.id,
                JudgeOutcome.FAIL,
                f"could not parse score from LLM response: {raw_response!r}",
                self.name,
                metadata={"raw_response": raw_response},
            )

        outcome = (
            JudgeOutcome.PASS if score >= self._pass_threshold else JudgeOutcome.FAIL
        )
        reason = (
            f"score {score}/{self._max_score}: {justification}"
            if justification
            else f"score {score}/{self._max_score}"
        )
        return JudgeResult(
            case.id,
            outcome,
            reason,
            self.name,
            metadata={
                "score": score,
                "max_score": self._max_score,
                "justification": justification or "",
                "raw_response": raw_response,
            },
        )

    def _parse_response(self, raw: str) -> tuple[int | None, str | None]:
        """Parse score and justification from the LLM response.

        Tries JSON first; falls back to extracting the first integer.

        Args:
            raw: Raw LLM response string.

        Returns:
            Tuple of (score, justification).  Score is None if unparseable.
        """
        import json  # noqa: PLC0415 — stdlib, but isolated import for clarity

        # Try JSON parse
        try:
            data = json.loads(raw.strip())
            score = int(data["score"])
            justification = str(data.get("justification", ""))
            return score, justification
        except Exception:
            pass

        # Fallback: find first integer in the response
        match = _SCORE_RE.search(raw)
        if match:
            return int(match.group(1)), None
        return None, None


# ---------------------------------------------------------------------------
# 2.6  Dispatcher
# ---------------------------------------------------------------------------


def get_judge_for(
    kind: str,
    *,
    executor: BaseExecutor | None = None,
    llm: LLMClient | None = None,
) -> Judge:
    """Return the appropriate judge for a given expectation kind.

    Mapping:
      - ``"intent_only"``        → :class:`IntentJudge` (always cheap)
      - ``"sql_equivalent"``     → :class:`SQLEquivalenceJudge`
      - ``"substring_contains"`` → :class:`SubstringContainsJudge`
      - ``"result_set"``         → :class:`ResultSetJudge` (requires *executor*)
      - ``"llm_judge"``          → :class:`LLMJudge` (requires *llm*)

    Phase B-3 runner contract:
      The runner calls ``get_judge_for(case.expected.kind, executor=..., llm=...)``
      and ALSO always runs ``IntentJudge`` independently as a universal base check
      on *every* case.

    Args:
        kind: The ``ExpectedAnswer.kind`` discriminator string.
        executor: Required when kind is ``"result_set"``.
        llm: Required when kind is ``"llm_judge"``.

    Returns:
        A ``Judge`` instance ready to call ``.evaluate(case, output)``.

    Raises:
        ValueError: If *kind* is unknown, or a required dependency is None.
    """
    if kind == "intent_only":
        return IntentJudge()
    if kind == "sql_equivalent":
        return SQLEquivalenceJudge()
    if kind == "substring_contains":
        return SubstringContainsJudge()
    if kind == "result_set":
        if executor is None:
            raise ValueError("ResultSetJudge requires an executor — pass executor=...")
        return ResultSetJudge(executor)
    if kind == "llm_judge":
        if llm is None:
            raise ValueError("LLMJudge requires an llm — pass llm=...")
        return LLMJudge(llm)
    raise ValueError(f"Unknown expectation kind: {kind!r}")
