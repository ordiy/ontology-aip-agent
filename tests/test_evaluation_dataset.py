"""Unit tests for EvalDataset loading and filtering helpers (Phase B-1)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.evaluation import EvalCase, EvalDataset, load_dataset, list_suites


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CASE_A = {
    "id": "test-alpha",
    "question": "How many buyers?",
    "domain": "ecommerce",
    "expected_intent": "READ",
    "expected": {"kind": "intent_only", "expected_intent": "READ"},
    "tags": ["smoke"],
}
_CASE_B = {
    "id": "test-beta",
    "question": "Show all pending orders",
    "domain": "ecommerce",
    "expected_intent": "READ",
    "expected": {"kind": "sql_equivalent", "expected_sql": "SELECT * FROM orders WHERE status='pending'"},
    "tags": ["smoke", "regression"],
}
_CASE_C = {
    "id": "test-gamma",
    "question": "What is the revenue trend?",
    "domain": "finance",
    "expected_intent": "ANALYZE",
    "expected": {"kind": "llm_judge", "rubric": "Describe the trend clearly."},
    "tags": ["regression"],
}


def _write_yaml(path: Path, cases: list[dict]) -> None:
    path.write_text(yaml.dump(cases), encoding="utf-8")


# ---------------------------------------------------------------------------
# Single-file loading
# ---------------------------------------------------------------------------


def test_load_dataset_single_file(tmp_path: Path):
    """load_dataset from a single file returns the correct number of cases."""
    f = tmp_path / "cases.yaml"
    _write_yaml(f, [_CASE_A, _CASE_B])

    ds = load_dataset(f)
    assert len(ds) == 2
    ids = {c.id for c in ds}
    assert ids == {"test-alpha", "test-beta"}


def test_load_dataset_directory_recursive(tmp_path: Path):
    """load_dataset from a directory merges all YAML files recursively."""
    sub = tmp_path / "sub"
    sub.mkdir()

    _write_yaml(tmp_path / "a.yaml", [_CASE_A])
    _write_yaml(sub / "b.yaml", [_CASE_B])
    _write_yaml(sub / "c.yml", [_CASE_C])

    ds = load_dataset(tmp_path)
    assert len(ds) == 3
    ids = {c.id for c in ds}
    assert ids == {"test-alpha", "test-beta", "test-gamma"}


def test_load_dataset_rejects_duplicate_ids(tmp_path: Path):
    """load_dataset raises ValueError when the same id appears in two files."""
    _write_yaml(tmp_path / "file1.yaml", [_CASE_A])
    _write_yaml(tmp_path / "file2.yaml", [_CASE_A])  # duplicate id

    with pytest.raises(ValueError, match="Duplicate EvalCase id"):
        load_dataset(tmp_path)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def test_filter_by_tag_returns_subset(tmp_path: Path):
    """filter_by_tag returns only cases carrying that tag."""
    f = tmp_path / "cases.yaml"
    _write_yaml(f, [_CASE_A, _CASE_B, _CASE_C])

    ds = load_dataset(f)
    smoke = ds.filter_by_tag("smoke")
    assert len(smoke) == 2
    assert all("smoke" in c.tags for c in smoke)

    regression = ds.filter_by_tag("regression")
    assert len(regression) == 2

    empty = ds.filter_by_tag("nonexistent")
    assert len(empty) == 0


def test_filter_by_domain(tmp_path: Path):
    """filter_by_domain returns only cases for the given domain."""
    f = tmp_path / "cases.yaml"
    _write_yaml(f, [_CASE_A, _CASE_B, _CASE_C])

    ds = load_dataset(f)
    ec = ds.filter_by_domain("ecommerce")
    assert len(ec) == 2
    assert all(c.domain == "ecommerce" for c in ec)

    fin = ds.filter_by_domain("finance")
    assert len(fin) == 1


# ---------------------------------------------------------------------------
# case_by_id
# ---------------------------------------------------------------------------


def test_case_by_id_returns_none_for_missing(tmp_path: Path):
    """case_by_id returns None when the id is not present."""
    f = tmp_path / "cases.yaml"
    _write_yaml(f, [_CASE_A])

    ds = load_dataset(f)
    assert ds.case_by_id("test-alpha") is not None
    assert ds.case_by_id("does-not-exist") is None


# ---------------------------------------------------------------------------
# Iteration / len
# ---------------------------------------------------------------------------


def test_iter_and_len_on_dataset(tmp_path: Path):
    """__iter__ and __len__ work correctly on EvalDataset."""
    f = tmp_path / "cases.yaml"
    _write_yaml(f, [_CASE_A, _CASE_B, _CASE_C])

    ds = load_dataset(f)
    assert len(ds) == 3

    collected = list(ds)
    assert len(collected) == 3
    assert all(isinstance(c, EvalCase) for c in collected)


# ---------------------------------------------------------------------------
# Real ecommerce YAML
# ---------------------------------------------------------------------------


def test_load_real_ecommerce_yaml():
    """Load the real ecommerce smoke dataset and verify counts and intent coverage."""
    yaml_path = Path(__file__).parent / "eval" / "datasets" / "ecommerce.yaml"
    assert yaml_path.exists(), f"Smoke dataset not found at {yaml_path}"

    ds = load_dataset(yaml_path)
    assert len(ds) == 30, f"Expected 30 cases, got {len(ds)}"

    intents_present = {c.expected_intent for c in ds}
    required_intents = {"READ", "WRITE", "ANALYZE", "DECIDE", "OPERATE", "UNCLEAR"}
    assert required_intents == intents_present, (
        f"Missing intents: {required_intents - intents_present}"
    )

    # All cases belong to ecommerce domain
    assert all(c.domain == "ecommerce" for c in ds)

    # All have the smoke tag
    assert all("smoke" in c.tags for c in ds)
