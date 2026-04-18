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

def test_detect_chart_type_bar_for_category_numeric():
    """detect_chart_type returns 'bar' or 'pie' for text+numeric columns."""
    from src.web.visualizer import detect_chart_type
    import pandas as pd

    df = pd.DataFrame({
        "category": ["A", "B", "C", "D", "E", "F", "G", "H", "I"],
        "total": [10, 20, 15, 30, 5, 25, 12, 8, 18],
    })
    result = detect_chart_type(df)
    assert result in ("bar", "pie")


def test_detect_chart_type_pie_for_few_categories():
    """detect_chart_type returns 'pie' when ≤8 distinct categories with positive values."""
    from src.web.visualizer import detect_chart_type
    import pandas as pd

    df = pd.DataFrame({
        "status": ["active", "pending", "closed"],
        "count": [50, 30, 20],
    })
    result = detect_chart_type(df)
    assert result == "pie"


def test_detect_chart_type_line_for_date_column():
    """detect_chart_type returns 'line' when a date-like column is present."""
    from src.web.visualizer import detect_chart_type
    import pandas as pd

    df = pd.DataFrame({
        "order_date": ["2025-01-01", "2025-01-02", "2025-01-03"],
        "revenue": [100, 200, 150],
    })
    result = detect_chart_type(df)
    assert result == "line"


def test_detect_chart_type_none_for_text_only():
    """detect_chart_type returns None for text-only results (no numeric columns)."""
    from src.web.visualizer import detect_chart_type
    import pandas as pd

    df = pd.DataFrame({
        "name": ["Alice", "Bob"],
        "email": ["a@test.com", "b@test.com"],
    })
    result = detect_chart_type(df)
    assert result is None


def test_detect_chart_type_none_for_empty():
    """detect_chart_type returns None for empty dataframe."""
    from src.web.visualizer import detect_chart_type
    import pandas as pd

    result = detect_chart_type(pd.DataFrame())
    assert result is None


def test_build_chart_returns_figure_for_bar():
    """build_chart returns a plotly figure for bar chart type."""
    from src.web.visualizer import build_chart
    import pandas as pd

    df = pd.DataFrame({
        "product": ["A", "B", "C"],
        "sales": [100, 200, 150],
    })
    fig = build_chart(df, "bar", title="Test")
    assert fig is not None
