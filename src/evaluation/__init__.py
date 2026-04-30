"""Public API for the evaluation framework (Phase B).

Exports:
    EvalCase: Single evaluation case data model.
    ExpectedAnswer: Discriminated union of all expectation types.
    IntentOnlyExpectation: Expectation that only asserts intent.
    SQLEquivalentExpectation: Expectation that asserts SQL AST equivalence.
    ResultSetExpectation: Expectation that asserts a specific result set.
    SubstringContainsExpectation: Expectation that asserts response substrings.
    LLMJudgeExpectation: Expectation evaluated by an LLM judge.
    EvalDataset: Immutable collection of EvalCase objects.
    load_dataset: Load an EvalDataset from YAML files.
    list_suites: List unique suite tags from a dataset path.
    AgentOutput: Snapshot of agent pipeline output for a single case.
    JudgeOutcome: PASS / FAIL / SKIP enum.
    JudgeResult: Immutable result produced by a judge.
    Judge: Protocol for all judges.
    IntentJudge: Zero-cost intent classification judge.
    SQLEquivalenceJudge: Zero-cost SQL canonical-form judge.
    SubstringContainsJudge: Zero-cost substring presence judge.
    ResultSetJudge: SQL-executing result-set judge.
    LLMJudge: LLM-scoring rubric judge.
    get_judge_for: Dispatcher that maps expectation kind → judge instance.
"""
from __future__ import annotations

from src.evaluation.case import (
    EvalCase,
    ExpectedAnswer,
    IntentOnlyExpectation,
    LLMJudgeExpectation,
    ResultSetExpectation,
    SQLEquivalentExpectation,
    SubstringContainsExpectation,
)
from src.evaluation.dataset import EvalDataset, list_suites, load_dataset
from src.evaluation.judges import (
    AgentOutput,
    IntentJudge,
    Judge,
    JudgeOutcome,
    JudgeResult,
    LLMJudge,
    ResultSetJudge,
    SQLEquivalenceJudge,
    SubstringContainsJudge,
    get_judge_for,
)

__all__ = [
    "EvalCase",
    "ExpectedAnswer",
    "IntentOnlyExpectation",
    "SQLEquivalentExpectation",
    "ResultSetExpectation",
    "SubstringContainsExpectation",
    "LLMJudgeExpectation",
    "EvalDataset",
    "load_dataset",
    "list_suites",
    "AgentOutput",
    "JudgeOutcome",
    "JudgeResult",
    "Judge",
    "IntentJudge",
    "SQLEquivalenceJudge",
    "SubstringContainsJudge",
    "ResultSetJudge",
    "LLMJudge",
    "get_judge_for",
]
