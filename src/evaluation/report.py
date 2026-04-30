"""EvalReport: structured result container with JSON/Markdown output and diff support.

``EvalReport`` is produced by ``EvalRunner.run()`` and captures all case
results, summary statistics, and run metadata.  It can be serialised to JSON
(round-trip safe), rendered as GitHub-PR-friendly Markdown, and diffed against
a baseline via ``diff_reports()``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.evaluation.judges import JudgeOutcome, JudgeResult
from src.evaluation.runner import CaseResult, RunSummary, _combine_outcome

logger = logging.getLogger(__name__)

_MAX_MARKDOWN_LINES = 50


# ---------------------------------------------------------------------------
# EvalReport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalReport:
    """Immutable container for a full evaluation run.

    Attributes:
        case_results: Ordered list of per-case results.
        summary: Aggregated pass/fail/skip statistics.
        started_at: UTC datetime when the run started.
        completed_at: UTC datetime when the run finished.
        suite_name: Tag/suite that was evaluated (e.g. ``"smoke"``).
        llm_model: Model name used during the run.
        metadata: Free-form key/value extras for provenance.
    """

    case_results: list[CaseResult]
    summary: RunSummary
    started_at: datetime
    completed_at: datetime
    suite_name: str
    llm_model: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def accuracy(self) -> float:
        """Overall pass rate (0.0–1.0); excludes skipped cases."""
        return self.summary.accuracy

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_json(self) -> dict:
        """Serialise to a plain JSON-safe dict.

        All datetime values are converted to ISO-8601 strings.
        frozenset values are converted to sorted lists.

        Returns:
            A dict that is safe to pass to ``json.dumps``.
        """
        return {
            "suite_name": self.suite_name,
            "llm_model": self.llm_model,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "summary": {
                "total": self.summary.total,
                "passed": self.summary.passed,
                "failed": self.summary.failed,
                "skipped": self.summary.skipped,
                "accuracy": self.summary.accuracy,
                "duration_ms": self.summary.duration_ms,
                "by_intent": self.summary.by_intent,
                "by_domain": self.summary.by_domain,
            },
            "case_results": [_case_result_to_dict(cr) for cr in self.case_results],
            "metadata": self.metadata,
        }

    def to_markdown(self) -> str:
        """Render a GitHub-PR-friendly Markdown summary.

        Output is capped at ~50 lines.

        Returns:
            Multi-line Markdown string.
        """
        lines: list[str] = []
        acc_pct = f"{self.accuracy * 100:.1f}%"
        lines.append(
            f"## Eval Report — {self.suite_name} | "
            f"Accuracy: {acc_pct} "
            f"({self.summary.passed}/{self.summary.passed + self.summary.failed} non-skip)"
        )
        lines.append("")
        lines.append(
            f"- **Model**: {self.llm_model}"
        )
        lines.append(
            f"- **Total**: {self.summary.total} "
            f"| Pass: {self.summary.passed} "
            f"| Fail: {self.summary.failed} "
            f"| Skip: {self.summary.skipped}"
        )
        lines.append(
            f"- **Duration**: {self.summary.duration_ms / 1000:.1f}s"
        )
        lines.append("")

        # Intent breakdown table
        if self.summary.by_intent:
            lines.append("### By Intent")
            lines.append("")
            lines.append("| Intent | Pass | Fail | Skip | Accuracy |")
            lines.append("|--------|------|------|------|----------|")
            for intent, counts in sorted(self.summary.by_intent.items()):
                p = counts.get("pass", 0)
                f = counts.get("fail", 0)
                s = counts.get("skip", 0)
                non_skip = p + f
                acc = f"{p / non_skip * 100:.1f}%" if non_skip else "—"
                lines.append(f"| {intent} | {p} | {f} | {s} | {acc} |")
            lines.append("")

        # Domain breakdown table
        if self.summary.by_domain:
            lines.append("### By Domain")
            lines.append("")
            lines.append("| Domain | Pass | Fail | Skip | Accuracy |")
            lines.append("|--------|------|------|------|----------|")
            for domain, counts in sorted(self.summary.by_domain.items()):
                p = counts.get("pass", 0)
                f = counts.get("fail", 0)
                s = counts.get("skip", 0)
                non_skip = p + f
                acc = f"{p / non_skip * 100:.1f}%" if non_skip else "—"
                lines.append(f"| {domain} | {p} | {f} | {s} | {acc} |")
            lines.append("")

        # Failures list (up to cap)
        failures = [
            cr for cr in self.case_results
            if _combine_outcome(cr.intent_result.outcome, cr.primary_result.outcome)
            == JudgeOutcome.FAIL
        ]
        if failures:
            lines.append("### Failures")
            lines.append("")
            for cr in failures:
                if len(lines) >= _MAX_MARKDOWN_LINES - 2:
                    remaining = len(failures) - failures.index(cr)
                    lines.append(f"_…{remaining} more failures not shown_")
                    break
                reason = cr.primary_result.reason or cr.intent_result.reason
                lines.append(f"- **{cr.case.id}**: {reason}")
            lines.append("")

        return "\n".join(lines[:_MAX_MARKDOWN_LINES])

    @classmethod
    def from_json(cls, data: dict) -> "EvalReport":
        """Reconstruct an EvalReport from a ``to_json()`` dict.

        Args:
            data: Dict produced by ``to_json()``.

        Returns:
            Reconstituted ``EvalReport``.
        """
        summary_raw = data["summary"]
        summary = RunSummary(
            total=summary_raw["total"],
            passed=summary_raw["passed"],
            failed=summary_raw["failed"],
            skipped=summary_raw["skipped"],
            by_intent=summary_raw.get("by_intent", {}),
            by_domain=summary_raw.get("by_domain", {}),
            duration_ms=summary_raw.get("duration_ms", 0),
        )

        case_results = [_case_result_from_dict(cr) for cr in data.get("case_results", [])]

        return cls(
            case_results=case_results,
            summary=summary,
            started_at=datetime.fromisoformat(data["started_at"]),
            completed_at=datetime.fromisoformat(data["completed_at"]),
            suite_name=data["suite_name"],
            llm_model=data["llm_model"],
            metadata=data.get("metadata", {}),
        )


# ---------------------------------------------------------------------------
# DiffReport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiffReport:
    """Diff between a baseline EvalReport and a head EvalReport.

    Attributes:
        new_failures: Case IDs that passed in baseline but fail in head (regressions).
        new_passes: Case IDs that failed in baseline but pass in head (improvements).
        accuracy_delta: ``head.accuracy - baseline.accuracy`` (positive = improvement).
        intent_deltas: Per-intent accuracy change (positive = improvement).
    """

    new_failures: list[str]
    new_passes: list[str]
    accuracy_delta: float
    intent_deltas: dict[str, float]

    def to_markdown(self) -> str:
        """Render a short Markdown summary suitable for PR comments.

        Returns:
            Multi-line Markdown string.
        """
        lines: list[str] = []
        delta_sign = "+" if self.accuracy_delta >= 0 else ""
        lines.append(
            f"## Eval Diff | Accuracy Δ: {delta_sign}{self.accuracy_delta * 100:.1f}%"
        )
        lines.append("")

        if self.new_failures:
            lines.append(f"### 🔴 Regressions ({len(self.new_failures)})")
            for cid in self.new_failures[:10]:
                lines.append(f"- {cid}")
            if len(self.new_failures) > 10:
                lines.append(f"_…{len(self.new_failures) - 10} more_")
            lines.append("")

        if self.new_passes:
            lines.append(f"### 🟢 Improvements ({len(self.new_passes)})")
            for cid in self.new_passes[:10]:
                lines.append(f"- {cid}")
            if len(self.new_passes) > 10:
                lines.append(f"_…{len(self.new_passes) - 10} more_")
            lines.append("")

        if self.intent_deltas:
            lines.append("### Per-Intent Δ")
            lines.append("")
            lines.append("| Intent | Accuracy Δ |")
            lines.append("|--------|-----------|")
            for intent, delta in sorted(self.intent_deltas.items()):
                sign = "+" if delta >= 0 else ""
                lines.append(f"| {intent} | {sign}{delta * 100:.1f}% |")
            lines.append("")

        if not self.new_failures and not self.new_passes:
            lines.append("_No regressions or improvements detected._")

        return "\n".join(lines)


def diff_reports(baseline: EvalReport, head: EvalReport) -> DiffReport:
    """Compute the diff between *baseline* and *head* EvalReports.

    Args:
        baseline: The reference (older) report.
        head: The candidate (newer) report being evaluated.

    Returns:
        A ``DiffReport`` with regressions, improvements, and accuracy deltas.
    """
    def _outcomes(report: EvalReport) -> dict[str, JudgeOutcome]:
        result: dict[str, JudgeOutcome] = {}
        for cr in report.case_results:
            result[cr.case.id] = _combine_outcome(
                cr.intent_result.outcome, cr.primary_result.outcome
            )
        return result

    base_outcomes = _outcomes(baseline)
    head_outcomes = _outcomes(head)

    new_failures: list[str] = []
    new_passes: list[str] = []

    for case_id, base_out in base_outcomes.items():
        head_out = head_outcomes.get(case_id)
        if head_out is None:
            continue
        if base_out == JudgeOutcome.PASS and head_out == JudgeOutcome.FAIL:
            new_failures.append(case_id)
        elif base_out == JudgeOutcome.FAIL and head_out == JudgeOutcome.PASS:
            new_passes.append(case_id)

    accuracy_delta = head.accuracy - baseline.accuracy

    # Per-intent accuracy deltas
    intent_deltas: dict[str, float] = {}
    all_intents = set(baseline.summary.by_intent) | set(head.summary.by_intent)
    for intent in all_intents:
        base_counts = baseline.summary.by_intent.get(intent, {"pass": 0, "fail": 0})
        head_counts = head.summary.by_intent.get(intent, {"pass": 0, "fail": 0})

        def _acc(counts: dict[str, int]) -> float:
            non_skip = counts.get("pass", 0) + counts.get("fail", 0)
            return counts.get("pass", 0) / non_skip if non_skip > 0 else 0.0

        intent_deltas[intent] = _acc(head_counts) - _acc(base_counts)

    return DiffReport(
        new_failures=sorted(new_failures),
        new_passes=sorted(new_passes),
        accuracy_delta=accuracy_delta,
        intent_deltas=intent_deltas,
    )


# ---------------------------------------------------------------------------
# Private serialisation helpers
# ---------------------------------------------------------------------------


def _judge_result_to_dict(jr: JudgeResult) -> dict:
    return {
        "case_id": jr.case_id,
        "outcome": jr.outcome.value,
        "reason": jr.reason,
        "judge_name": jr.judge_name,
        "metadata": jr.metadata,
    }


def _judge_result_from_dict(d: dict) -> JudgeResult:
    return JudgeResult(
        case_id=d["case_id"],
        outcome=JudgeOutcome(d["outcome"]),
        reason=d["reason"],
        judge_name=d["judge_name"],
        metadata=d.get("metadata", {}),
    )


def _case_result_to_dict(cr: CaseResult) -> dict:
    from src.evaluation.case import EvalCase  # local to avoid cycles at module load

    return {
        "case": cr.case.model_dump(mode="json"),
        "intent_result": _judge_result_to_dict(cr.intent_result),
        "primary_result": _judge_result_to_dict(cr.primary_result),
        "agent_output": {
            "intent": cr.agent_output.intent,
            "generated_sql": cr.agent_output.generated_sql,
            "query_result": cr.agent_output.query_result,
            "response": cr.agent_output.response,
            "error": cr.agent_output.error,
            "masked_columns": cr.agent_output.masked_columns,
        },
        "duration_ms": cr.duration_ms,
        "trace_url": cr.trace_url,
        "error": cr.error,
    }


def _case_result_from_dict(d: dict) -> CaseResult:
    from src.evaluation.case import EvalCase
    from src.evaluation.judges import AgentOutput

    ao_raw = d.get("agent_output", {})
    agent_output = AgentOutput(
        intent=ao_raw.get("intent"),
        generated_sql=ao_raw.get("generated_sql"),
        query_result=ao_raw.get("query_result"),
        response=ao_raw.get("response"),
        error=ao_raw.get("error"),
        masked_columns=ao_raw.get("masked_columns", {}),
    )

    return CaseResult(
        case=EvalCase.model_validate(d["case"]),
        intent_result=_judge_result_from_dict(d["intent_result"]),
        primary_result=_judge_result_from_dict(d["primary_result"]),
        agent_output=agent_output,
        duration_ms=d.get("duration_ms", 0),
        trace_url=d.get("trace_url"),
        error=d.get("error"),
    )
