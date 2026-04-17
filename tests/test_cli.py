"""Tests for CLI helper functions."""
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
from src.cli.app import _find_ontologies, _handle_system_command

def test_find_ontologies_returns_rdf_files(tmp_path):
    """_find_ontologies should return a dict mapping stem -> path for all .rdf files."""
    # Create fake rdf files
    (tmp_path / "retail.rdf").touch()
    (tmp_path / "healthcare.rdf").touch()
    result = _find_ontologies(str(tmp_path))
    assert "retail" in result
    assert "healthcare" in result
    assert result["retail"].endswith("retail.rdf")

def test_switch_command_unknown_domain():
    """_handle_system_command with .switch <unknown> should print error and return True."""
    mock_schema = MagicMock()
    with patch("src.cli.app.console.print") as mock_print:
        result = _handle_system_command(
            ".switch unknown_domain",
            mock_schema, {}, "test.db",
            ontologies={"retail": "retail.rdf"},
            config={"test": True}, llm=MagicMock()
        )
        assert result is True  # handled
        mock_print.assert_called_once()
        assert "not found" in mock_print.call_args[0][0]

def test_switch_command_valid_domain():
    """_handle_system_command with .switch <valid> should return dict with switch_to key."""
    mock_schema = MagicMock()
    result = _handle_system_command(
        ".switch retail",
        mock_schema, {}, "test.db",
        ontologies={"retail": "retail.rdf"},
        config={"database": {"path": "/tmp", "mock_rows_per_table": 5}, "permissions": {}},
        llm=MagicMock()
    )
    # Should return dict marker for main() to handle
    assert isinstance(result, dict)
    assert result.get("switch_to") == "retail"

def test_switch_no_args_shows_domains():
    """_handle_system_command with .switch alone should list domains and return True."""
    mock_schema = MagicMock()
    with patch("src.cli.app.console.print") as mock_print:
        result = _handle_system_command(
            ".switch",
            mock_schema, {}, "test.db",
            ontologies={"retail": "retail.rdf", "healthcare": "healthcare.rdf"},
            config={"test": True}, llm=MagicMock()
        )
        assert result is True
        # It should print "Available domains:" and then each domain
        calls = [c[0][0] for c in mock_print.call_args_list]
        assert any("Available domains" in call for call in calls)
        assert any("retail" in call for call in calls)
        assert any("healthcare" in call for call in calls)

def test_history_empty(capsys):
    """_handle_system_command .history with empty list prints 'No history yet'."""
    mock_schema = MagicMock()
    result = _handle_system_command(
        ".history",
        mock_schema, {}, "test.db",
        history=[]
    )
    assert result is True
    captured = capsys.readouterr()
    assert "No history" in captured.out

def test_history_shows_entries(capsys):
    """_handle_system_command .history shows past queries."""
    mock_schema = MagicMock()
    history = [
        {"query": "How many customers?", "intent": "READ", "sql": "SELECT COUNT(*) FROM customers", "response": "There are 10 customers."},
    ]
    result = _handle_system_command(
        ".history",
        mock_schema, {}, "test.db",
        history=history
    )
    assert result is True
    captured = capsys.readouterr()
    assert "How many customers?" in captured.out
    assert "SELECT COUNT(*) FROM customers" in captured.out
    assert "There are 10 customers." in captured.out

def test_history_clear_returns_signal():
    """_handle_system_command .history clear returns clear_history signal."""
    mock_schema = MagicMock()
    result = _handle_system_command(
        ".history clear",
        mock_schema, {}, "test.db",
        history=[{"query": "test", "intent": "READ", "sql": "", "response": ""}]
    )
    assert isinstance(result, dict)
    assert result.get("clear_history") is True
