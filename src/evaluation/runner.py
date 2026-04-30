"""EvalRunner: executes the real agent on each EvalCase and collects results.

Each case is independent — an exception in case-N is caught, recorded as
CaseResult.error, marked FAIL with reason ``"agent_crashed"``, and the run
continues.  Langfuse trace per case is optional: if ``obs`` is disabled or
trace-open raises, the run is not aborted.

Phase B-3 runner is sequential (concurrency=1).  The ``--concurrency N`` CLI
flag is accepted as a stub; full async support is deferred to B-4.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from src.evaluation.case import EvalCase
from src.evaluation.dataset import EvalDataset
from src.evaluation.judges import (
    AgentOutput,
    IntentJudge,
    Judge,
    JudgeOutcome,
    JudgeResult,
    get_judge_for,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CaseResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseResult:
    """Result of running a single EvalCase through the agent.

    Attributes:
        case: The original EvalCase.
        intent_result: Result from the IntentJudge (always populated).
        primary_result: Result from the kind-specific judge.
        agent_output: Snapshot of agent pipeline output.
        duration_ms: Wall-clock time for this case in milliseconds.
        trace_url: Langfuse trace URL if observability is enabled, else None.
        error: Exception message if the agent or judge crashed, else None.
    """

    case: EvalCase
    intent_result: JudgeResult
    primary_result: JudgeResult
    agent_output: AgentOutput
    duration_ms: int
    trace_url: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# RunSummary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunSummary:
    """Aggregated statistics for an eval run.

    Attributes:
        total: Total cases processed (excludes skipped-before-run cases).
        passed: Cases where both IntentJudge AND primary judge passed.
        failed: Cases that failed on either judge or crashed.
        skipped: Cases skipped (skip_reason set or both judges returned SKIP).
        by_intent: Per-intent breakdown ``{"READ": {"pass": 10, "fail": 2, "skip": 0}, ...}``.
        by_domain: Per-domain breakdown with same structure.
        duration_ms: Total wall-clock time for the run in milliseconds.
    """

    total: int
    passed: int
    failed: int
    skipped: int
    by_intent: dict[str, dict[str, int]]
    by_domain: dict[str, dict[str, int]]
    duration_ms: int

    @property
    def accuracy(self) -> float:
        """Pass rate over non-skipped cases; 0.0 when total == 0."""
        non_skip = self.passed + self.failed
        return self.passed / non_skip if non_skip > 0 else 0.0


# ---------------------------------------------------------------------------
# EvalRunner
# ---------------------------------------------------------------------------


class EvalRunner:
    """Runs EvalCases through the agent and returns an EvalReport.

    Dependencies are injected via the constructor so the runner is fully
    testable with fakes — no module-level singletons.

    Args:
        graph_factory: ``Callable[[str], object]`` — takes a domain name and
            returns a compiled LangGraph.  Called once per case so the graph
            can be rebuilt for different domains.
        intent_judge: ``IntentJudge`` instance used on every case.
        get_judge: Dispatcher mapping expectation kind → Judge instance.
            Defaults to ``get_judge_for`` from ``src.evaluation.judges``.
        obs: ``ObservabilityClient`` or any object with an ``enabled: bool``
            property and a ``start_trace(...)`` context manager.  ``None``
            disables tracing without error.
    """

    def __init__(
        self,
        graph_factory: Callable[[str], Any],
        intent_judge: IntentJudge | None = None,
        get_judge: Callable[[str], Judge] | None = None,
        obs: Any | None = None,
    ) -> None:
        self._graph_factory = graph_factory
        self._intent_judge: IntentJudge = intent_judge or IntentJudge()
        self._get_judge: Callable[[str], Judge] = get_judge or get_judge_for
        self._obs = obs

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_one(self, case: EvalCase) -> CaseResult:
        """Execute a single EvalCase and return a CaseResult.

        The case is always run even if ``case.skip_reason`` is set —
        skip detection happens inside the judges.  Callers that want to
        skip before running should check ``case.skip_reason`` beforehand.

        Args:
            case: The evaluation case to run.

        Returns:
            A ``CaseResult`` with both judge results and timing.
        """
        trace_url: str | None = None
        t_start = time.monotonic()

        # --- 1. Optional Langfuse trace -----------------------------------------------
        try:
            if self._obs is not None and getattr(self._obs, "enabled", False):
                trace_url = self._open_trace(case)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse trace open failed for case %s: %s", case.id, exc)

        # --- 2. Run the agent ---------------------------------------------------------
        agent_output: AgentOutput
        crash_error: str | None = None
        try:
            graph = self._graph_factory(case.domain)
            raw_state = graph.invoke({"user_query": case.question, "approved": True})
            agent_output = AgentOutput(
                intent=raw_state.get("intent"),
                generated_sql=raw_state.get("generated_sql"),
                query_result=raw_state.get("query_result"),
                response=raw_state.get("response"),
                error=raw_state.get("error"),
            )
        except Exception as exc:  # noqa: BLE001
            crash_error = str(exc)
            logger.warning("Agent crashed on case %s: %s", case.id, exc)
            agent_output = AgentOutput(
                intent=None,
                generated_sql=None,
                query_result=None,
                response=None,
                error=crash_error,
            )

        # --- 3. Run IntentJudge -------------------------------------------------------
        intent_result = self._intent_judge.evaluate(case, agent_output)

        # --- 4. Run primary judge -----------------------------------------------------
        try:
            primary_judge = self._get_judge(case.expected.kind)
            primary_result = primary_judge.evaluate(case, agent_output)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Primary judge crashed on case %s: %s", case.id, exc)
            primary_result = JudgeResult(
                case_id=case.id,
                outcome=JudgeOutcome.FAIL,
                reason=f"judge_crashed: {exc}",
                judge_name="unknown",
            )

        duration_ms = int((time.monotonic() - t_start) * 1000)

        return CaseResult(
            case=case,
            intent_result=intent_result,
            primary_result=primary_result,
            agent_output=agent_output,
            duration_ms=duration_ms,
            trace_url=trace_url,
            error=crash_error,
        )

    def run(self, dataset: EvalDataset) -> "EvalReport":  # noqa: F821
        """Run all cases in *dataset* and return an EvalReport.

        Cases with ``skip_reason`` set are recorded as SKIP without invoking
        the agent.  All other cases are executed sequentially.

        Args:
            dataset: The collection of cases to evaluate.

        Returns:
            An ``EvalReport`` with all case results and aggregated statistics.
        """
        from src.evaluation.report import EvalReport  # avoid circular import

        started_at = datetime.now(tz=timezone.utc)
        run_start = time.monotonic()

        case_results: list[CaseResult] = []

        for case in dataset:
            if case.skip_reason is not None:
                # Skip without invoking agent — create synthetic SKIP result.
                skipped_output = AgentOutput(
                    intent=None, generated_sql=None, query_result=None, response=None
                )
                skip_judge = JudgeResult(
                    case_id=case.id,
                    outcome=JudgeOutcome.SKIP,
                    reason=case.skip_reason,
                    judge_name="runner",
                )
                case_results.append(
                    CaseResult(
                        case=case,
                        intent_result=skip_judge,
                        primary_result=skip_judge,
                        agent_output=skipped_output,
                        duration_ms=0,
                    )
                )
                logger.info("Skipped case %s: %s", case.id, case.skip_reason)
                continue

            logger.info("Running case %s", case.id)
            try:
                result = self.run_one(case)
            except Exception as exc:  # noqa: BLE001
                # Last-resort catch — run_one should never surface unhandled exceptions.
                logger.error("run_one raised unexpectedly for case %s: %s", case.id, exc)
                crashed_output = AgentOutput(
                    intent=None, generated_sql=None, query_result=None,
                    response=None, error=str(exc)
                )
                crash_judge = JudgeResult(
                    case_id=case.id,
                    outcome=JudgeOutcome.FAIL,
                    reason="agent_crashed",
                    judge_name="runner",
                )
                result = CaseResult(
                    case=case,
                    intent_result=crash_judge,
                    primary_result=crash_judge,
                    agent_output=crashed_output,
                    duration_ms=0,
                    error=str(exc),
                )

            case_results.append(result)
            _outcome = _combine_outcome(result.intent_result.outcome, result.primary_result.outcome)
            logger.info(
                "Case %s → %s (intent=%s, primary=%s, %dms)",
                case.id, _outcome.value,
                result.intent_result.outcome.value,
                result.primary_result.outcome.value,
                result.duration_ms,
            )

        total_ms = int((time.monotonic() - run_start) * 1000)
        summary = _build_summary(case_results, total_ms)
        completed_at = datetime.now(tz=timezone.utc)

        # Determine LLM model name if possible via the graph_factory's closure.
        llm_model = "unknown"
        if hasattr(self._graph_factory, "__self__"):
            llm_model = str(self._graph_factory.__self__)

        suite_name = "unnamed"

        return EvalReport(
            case_results=case_results,
            summary=summary,
            started_at=started_at,
            completed_at=completed_at,
            suite_name=suite_name,
            llm_model=llm_model,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _open_trace(self, case: EvalCase) -> str | None:
        """Open a Langfuse trace for *case*.

        Args:
            case: The evaluation case being run.

        Returns:
            The trace URL as a string, or ``None`` if unavailable.
        """
        tags = ["eval", f"suite=eval", f"case={case.id}", f"domain={case.domain}"]
        try:
            with self._obs.start_trace(  # type: ignore[union-attr]
                session_id=f"eval-{case.id}",
                name=f"eval:{case.id}",
                metadata={"case_id": case.id, "domain": case.domain, "tags": tags},
            ):
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse start_trace raised: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _combine_outcome(
    intent: JudgeOutcome,
    primary: JudgeOutcome,
) -> JudgeOutcome:
    """Combine intent + primary outcomes into a single case outcome.

    Rules:
    - PASS only when both are PASS.
    - SKIP when either is SKIP and the other is not FAIL.
    - FAIL otherwise.

    Args:
        intent: Outcome from the IntentJudge.
        primary: Outcome from the primary judge.

    Returns:
        Combined JudgeOutcome.
    """
    if intent == JudgeOutcome.PASS and primary == JudgeOutcome.PASS:
        return JudgeOutcome.PASS
    if JudgeOutcome.FAIL in (intent, primary):
        return JudgeOutcome.FAIL
    return JudgeOutcome.SKIP


def _build_summary(case_results: list[CaseResult], total_ms: int) -> RunSummary:
    """Aggregate CaseResult list into a RunSummary.

    Args:
        case_results: All case results produced by the run.
        total_ms: Total wall-clock duration in milliseconds.

    Returns:
        Populated RunSummary.
    """
    passed = failed = skipped = 0
    by_intent: dict[str, dict[str, int]] = {}
    by_domain: dict[str, dict[str, int]] = {}

    for cr in case_results:
        outcome = _combine_outcome(cr.intent_result.outcome, cr.primary_result.outcome)
        key = outcome.value  # "pass" | "fail" | "skip"

        if outcome == JudgeOutcome.PASS:
            passed += 1
        elif outcome == JudgeOutcome.FAIL:
            failed += 1
        else:
            skipped += 1

        intent = cr.case.expected_intent
        if intent not in by_intent:
            by_intent[intent] = {"pass": 0, "fail": 0, "skip": 0}
        by_intent[intent][key] += 1

        domain = cr.case.domain
        if domain not in by_domain:
            by_domain[domain] = {"pass": 0, "fail": 0, "skip": 0}
        by_domain[domain][key] += 1

    return RunSummary(
        total=len(case_results),
        passed=passed,
        failed=failed,
        skipped=skipped,
        by_intent=by_intent,
        by_domain=by_domain,
        duration_ms=total_ms,
    )
