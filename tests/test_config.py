import os
from pathlib import Path
from src.config import load_config


def test_load_config_from_yaml(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "llm:\n"
        "  provider: vertex\n"
        "  model: gemini-test\n"
        "  temperature: 0.5\n"
        "vertex:\n"
        "  project: test-project\n"
        "  location: us-central1\n"
        "database:\n"
        "  path: ./data/\n"
        "  mock_rows_per_table: 50\n"
        "permissions:\n"
        "  read: auto\n"
        "  write: confirm\n"
        "  delete: confirm\n"
        "  admin: deny\n"
    )
    config = load_config(str(config_file))
    assert config["llm"]["provider"] == "vertex"
    assert config["llm"]["model"] == "gemini-test"
    assert config["llm"]["temperature"] == 0.5
    assert config["vertex"]["project"] == "test-project"
    assert config["database"]["mock_rows_per_table"] == 50
    assert config["permissions"]["write"] == "confirm"


def test_env_vars_override_yaml(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "llm:\n"
        "  provider: vertex\n"
        "  model: gemini-test\n"
        "  temperature: 0.0\n"
        "vertex:\n"
        "  project: yaml-project\n"
        "  location: global\n"
        "database:\n"
        "  path: ./data/\n"
        "  mock_rows_per_table: 100\n"
        "permissions:\n"
        "  read: auto\n"
        "  write: confirm\n"
        "  delete: confirm\n"
        "  admin: deny\n"
    )
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "llama3")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "env-project")
    config = load_config(str(config_file))
    assert config["llm"]["provider"] == "ollama"
    assert config["llm"]["model"] == "llama3"
    assert config["vertex"]["project"] == "env-project"


def test_load_config_defaults_when_no_file():
    config = load_config("/nonexistent/config.yaml")
    assert config["llm"]["provider"] == "vertex"
    assert config["database"]["mock_rows_per_table"] == 100
    assert config["permissions"]["read"] == "auto"
