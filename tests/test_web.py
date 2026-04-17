"""Tests for Streamlit web app helper functions.

Streamlit's full rendering pipeline can't be unit tested easily,
so we test the pure helper functions that contain real logic.
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from src.web.app import _find_ontologies


def test_find_ontologies_returns_rdf_files(tmp_path):
    """_find_ontologies should find all .rdf files and return name->path dict."""
    (tmp_path / "retail.rdf").touch()
    (tmp_path / "healthcare.rdf").touch()
    (tmp_path / "not_rdf.txt").touch()  # Should be ignored

    result = _find_ontologies(str(tmp_path))

    assert "retail" in result
    assert "healthcare" in result
    assert "not_rdf" not in result  # Non-RDF files excluded
    assert result["retail"].endswith("retail.rdf")


def test_find_ontologies_empty_dir(tmp_path):
    """_find_ontologies should return empty dict when no .rdf files exist."""
    result = _find_ontologies(str(tmp_path))
    assert result == {}


def test_find_ontologies_sorted_by_name(tmp_path):
    """_find_ontologies should return domains in alphabetical order."""
    (tmp_path / "zebra.rdf").touch()
    (tmp_path / "apple.rdf").touch()
    (tmp_path / "mango.rdf").touch()

    result = _find_ontologies(str(tmp_path))
    keys = list(result.keys())
    assert keys == sorted(keys)
