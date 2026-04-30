"""Tests for EvalReport, DiffReport, and diff_reports (Phase B-3).

All tests are self-contained — no real LLM or I/O.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from src.evaluation.case import EvalCase, IntentOnlyExpectation
from src.evaluation.judges import AgentOutput, JudgeOutcome, JudgeResult
from src.evaluation.report import DiffReport, EvalReport, diff_reports
from src.evaluation.runner import CaseResult, RunSummary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_judge_result(
    case_id: str = "c001",
    outcome: JudgeOutcome = JudgeOutcome.PASS,
    reason: str = "ok",
    judge_name: str = "TestJudge",
) -> JudgeResult:
    return JudgeResult(
        case_id=case_id,
        outcome=outcome,
        reason=reason,
        judge_name=judge_name,
    )


def _make_case(
    case_id: str = "c001",
    expected_intent: str = "READ",
    domain: str = "ecommerce",
) -> EvalCase:
    return EvalCase(
        id=case_id,
        question="How many orders?",
        domain=domain,
        expected_intent=expected_intent,
        expected=IntentOnlyExpectation(expected_intent=expected_intent),
        tags=frozenset({"smoke"}),
    )


def _make_agent_output(intent: str = "READ") -> AgentOutput:
    return AgentOutput(
        intent=intent,
        generated_sql="SELECT 1",
        query_result=[{"n": 1}],
        response="Result: 1",
    )


def _make_case_result(
    case_id: str = "c001",
    intent_outcome: JudgeOutcome = JudgeOutcome.PASS,
    primary_outcome: JudgeOutcome = JudgeOutcome.PASS,
    domain: str = "ecommerce",
    expected_intent: str = "READ",
    duration_ms: int = 123,
) -> CaseResult:
    case = _make_case(case_id, expected_intent=expected_intent, domain=domain)
    return CaseResult(
        case=case,
        intent_result=_make_judge_result(case_id, intent_outcome),
        primary_result=_make_judge_result(case_id, primary_outcome),
        agent_output=_make_agent_output(),
        duration_ms=duration_ms,
    )


def _make_summary(
    total: int = 3,
    passed: int = 2,
    failed: int = 1,
    skipped: int = 0,
    by_intent: dict | None = None,
    by_domain: dict | None = None,
    duration_ms: int = 5000,
) -> RunSummary:
    return RunSummary(
        total=total,
        passed=passed,
        failed=failed,
        skipped=skipped,
        by_intent=by_intent or {"READ": {"pass": 2, "fail": 1, "skip": 0}},
        by_domain=by_domain or {"ecommerce": {"pass": 2, "fail": 1, "skip": 0}},
        duration_ms=duration_ms,
    )


def _make_report(
    suite_name: str = "smoke",
    llm_model: str = "fake-model",
    case_results: list[CaseResult] | None = None,
    summary: RunSummary | None = None,
) -> EvalReport:
    now = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    cr = case_results or [
        _make_case_result("c001", JudgeOutcome.PASS, JudgeOutcome.PASS),
        _make_case_result("c002", JudgeOutcome.PASS, JudgeOutcome.PASS),
        _make_case_result("c003", JudgeOutcome.FAIL, JudgeOutcome.FAIL),
    ]
    s = summary or _make_summary()
    return EvalReport(
        case_results=cr,
        summary=s,
        started_at=now,
        completed_at=now,
        suite_name=suite_name,
        llm_model=llm_model,
    )


# ---------------------------------------------------------------------------
# to_json / from_json round-trip
# ---------------------------------------------------------------------------


def test_to_json_round_trip():
    """EvalReport → to_json() → from_json() preserves key fields."""
    report = _make_report()
    data = report.to_json()
    restored = EvalReport.from_json(data)

    assert restored.suite_name == report.suite_name
    assert restored.llm_model == report.llm_model
    assert restored.summary.total == report.summary.total
    assert restored.summary.passed == report.summary.passed
    assert restored.summary.failed == report.summary.failed
    assert len(restored.case_results) == len(report.case_results)


def test_to_json_is_json_serializable():
    """to_json() output must be accepted by json.dumps without error."""
    report = _make_report()
    data = report.to_json()
    # Must not raise
    serialised = json.dumps(data)
    assert len(serialised) > 0


def test_to_json_datetimes_are_strings():
    """Datetime fields in to_json() output must be ISO strings."""
    report = _make_report()
    data = report.to_json()
    assert isinstance(data["started_at"], str)
    assert isinstance(data["completed_at"], str)
    # Validate ISO format by round-tripping through datetime
    datetime.fromisoformat(data["started_at"])
    datetime.fromisoformat(data["completed_at"])


def test_to_json_case_results_count():
    """to_json() encodes all case results."""
    report = _make_report()
    data = report.to_json()
    assert len(data["case_results"]) == len(report.case_results)


# ---------------------------------------------------------------------------
# to_markdown
# ---------------------------------------------------------------------------


def test_to_markdown_includes_summary_table():
    """to_markdown() output contains intent breakdown table header."""
    report = _make_report()
    md = report.to_markdown()
    assert "| Intent |" in md
    assert "READ" in md


def test_to_markdown_contains_accuracy():
    """to_markdown() includes the overall accuracy percentage."""
    report = _make_report()
    md = report.to_markdown()
    # 2 passed, 1 failed → 66.7%
    assert "%" in md


def test_to_markdown_caps_lines():
    """to_markdown() output does not exceed 50 lines."""
    # Build a report with many failures to exercise the cap
    many_failures = [
        _make_case_result(f"fail-{i:03d}", JudgeOutcome.FAIL, JudgeOutcome.FAIL)
        for i in range(60)
    ]
    summary = RunSummary(
        total=60, passed=0, failed=60, skipped=0,
        by_intent={"READ": {"pass": 0, "fail": 60, "skip": 0}},
        by_domain={"ecommerce": {"pass": 0, "fail": 60, "skip": 0}},
        duration_ms=1000,
    )
    report = _make_report(case_results=many_failures, summary=summary)
    md = report.to_markdown()
    lines = md.splitlines()
    assert len(lines) <= 50


def test_to_markdown_has_domain_table():
    """to_markdown() includes domain breakdown when by_domain is populated."""
    report = _make_report()
    md = report.to_markdown()
    assert "| Domain |" in md


# ---------------------------------------------------------------------------
# diff_reports
# ---------------------------------------------------------------------------


def test_diff_reports_detects_new_failures():
    """Cases that passed in baseline but fail in head are listed as regressions."""
    baseline_cr = [
        _make_case_result("c001", JudgeOutcome.PASS, JudgeOutcome.PASS),
        _make_case_result("c002", JudgeOutcome.PASS, JudgeOutcome.PASS),
    ]
    head_cr = [
        _make_case_result("c001", JudgeOutcome.PASS, JudgeOutcome.PASS),
        _make_case_result("c002", JudgeOutcome.FAIL, JudgeOutcome.FAIL),  # regression
    ]
    baseline = _make_report(
        case_results=baseline_cr,
        summary=RunSummary(2, 2, 0, 0, {"READ": {"pass": 2, "fail": 0, "skip": 0}},
                           {"ecommerce": {"pass": 2, "fail": 0, "skip": 0}}, 1000),
    )
    head = _make_report(
        case_results=head_cr,
        summary=RunSummary(2, 1, 1, 0, {"READ": {"pass": 1, "fail": 1, "skip": 0}},
                           {"ecommerce": {"pass": 1, "fail": 1, "skip": 0}}, 1000),
    )
    diff = diff_reports(baseline, head)

    assert "c002" in diff.new_failures
    assert len(diff.new_passes) == 0


def test_diff_reports_detects_new_passes():
    """Cases that failed in baseline but pass in head are listed as improvements."""
    baseline_cr = [
        _make_case_result("c001", JudgeOutcome.FAIL, JudgeOutcome.FAIL),
    ]
    head_cr = [
        _make_case_result("c001", JudgeOutcome.PASS, JudgeOutcome.PASS),
    ]
    baseline = _make_report(
        case_results=baseline_cr,
        summary=RunSummary(1, 0, 1, 0, {"READ": {"pass": 0, "fail": 1, "skip": 0}},
                           {"ecommerce": {"pass": 0, "fail": 1, "skip": 0}}, 500),
    )
    head = _make_report(
        case_results=head_cr,
        summary=RunSummary(1, 1, 0, 0, {"READ": {"pass": 1, "fail": 0, "skip": 0}},
                           {"ecommerce": {"pass": 1, "fail": 0, "skip": 0}}, 500),
    )
    diff = diff_reports(baseline, head)

    assert "c001" in diff.new_passes
    assert len(diff.new_failures) == 0


def test_diff_reports_zero_delta_for_identical():
    """Diffing a report against itself yields zero accuracy_delta."""
    report = _make_report()
    diff = diff_reports(report, report)

    assert diff.accuracy_delta == pytest.approx(0.0)
    assert len(diff.new_failures) == 0
    assert len(diff.new_passes) == 0


def test_diff_reports_intent_deltas():
    """Per-intent accuracy deltas are computed correctly."""
    baseline_cr = [
        _make_case_result("c001", JudgeOutcome.PASS, JudgeOutcome.PASS, expected_intent="READ"),
        _make_case_result("c002", JudgeOutcome.FAIL, JudgeOutcome.FAIL, expected_intent="READ"),
    ]
    head_cr = [
        _make_case_result("c001", JudgeOutcome.PASS, JudgeOutcome.PASS, expected_intent="READ"),
        _make_case_result("c002", JudgeOutcome.PASS, JudgeOutcome.PASS, expected_intent="READ"),
    ]
    baseline = _make_report(
        case_results=baseline_cr,
        summary=RunSummary(2, 1, 1, 0, {"READ": {"pass": 1, "fail": 1, "skip": 0}},
                           {"ecommerce": {"pass": 1, "fail": 1, "skip": 0}}, 1000),
    )
    head = _make_report(
        case_results=head_cr,
        summary=RunSummary(2, 2, 0, 0, {"READ": {"pass": 2, "fail": 0, "skip": 0}},
                           {"ecommerce": {"pass": 2, "fail": 0, "skip": 0}}, 1000),
    )
    diff = diff_reports(baseline, head)

    assert "READ" in diff.intent_deltas
    # baseline READ accuracy = 0.5, head READ accuracy = 1.0 → delta = 0.5
    assert diff.intent_deltas["READ"] == pytest.approx(0.5)


def test_diff_to_markdown_renders():
    """DiffReport.to_markdown() returns a non-empty string."""
    baseline_cr = [
        _make_case_result("c001", JudgeOutcome.PASS, JudgeOutcome.PASS),
        _make_case_result("c002", JudgeOutcome.PASS, JudgeOutcome.PASS),
    ]
    head_cr = [
        _make_case_result("c001", JudgeOutcome.PASS, JudgeOutcome.PASS),
        _make_case_result("c002", JudgeOutcome.FAIL, JudgeOutcome.FAIL),
    ]
    baseline = _make_report(
        case_results=baseline_cr,
        summary=RunSummary(2, 2, 0, 0, {"READ": {"pass": 2, "fail": 0, "skip": 0}},
                           {"ecommerce": {"pass": 2, "fail": 0, "skip": 0}}, 500),
    )
    head = _make_report(
        case_results=head_cr,
        summary=RunSummary(2, 1, 1, 0, {"READ": {"pass": 1, "fail": 1, "skip": 0}},
                           {"ecommerce": {"pass": 1, "fail": 1, "skip": 0}}, 500),
    )
    diff = diff_reports(baseline, head)
    md = diff.to_markdown()

    assert "## Eval Diff" in md
    assert "c002" in md
    assert len(md) > 0


def test_diff_to_markdown_no_changes():
    """DiffReport with no regressions/improvements renders a clean no-op message."""
    report = _make_report()
    diff = diff_reports(report, report)
    md = diff.to_markdown()

    assert "No regressions" in md


# ---------------------------------------------------------------------------
# accuracy property
# ---------------------------------------------------------------------------


def test_report_accuracy_property():
    """EvalReport.accuracy delegates to RunSummary.accuracy."""
    report = _make_report(
        summary=RunSummary(4, 3, 1, 0, {}, {}, 1000)
    )
    assert report.accuracy == pytest.approx(0.75)


def test_run_summary_accuracy_zero_when_no_cases():
    s = RunSummary(0, 0, 0, 0, {}, {}, 0)
    assert s.accuracy == 0.0
