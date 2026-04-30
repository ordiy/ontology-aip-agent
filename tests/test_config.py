import os
from pathlib import Path
import pytest
from src.config import load_config, ConfigError, _substitute_placeholders


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


# ─────────────────────────────────────────────────────────────
# Placeholder substitution tests
# ─────────────────────────────────────────────────────────────

def test_placeholder_required_present(monkeypatch):
    monkeypatch.setenv("MY_VAR", "hello")
    result = _substitute_placeholders("${MY_VAR}", dict(os.environ))
    assert result == "hello"


def test_placeholder_required_missing_raises(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    with pytest.raises(ConfigError, match="MISSING_VAR"):
        _substitute_placeholders("${MISSING_VAR}", dict(os.environ))


def test_placeholder_default_used_when_missing(monkeypatch):
    monkeypatch.delenv("ABSENT_VAR", raising=False)
    result = _substitute_placeholders("${ABSENT_VAR:-fallback}", dict(os.environ))
    assert result == "fallback"


def test_placeholder_default_used_when_empty(monkeypatch):
    monkeypatch.setenv("EMPTY_VAR", "")
    result = _substitute_placeholders("${EMPTY_VAR:-fallback}", dict(os.environ))
    assert result == "fallback"


def test_placeholder_default_not_used_when_present(monkeypatch):
    monkeypatch.setenv("SET_VAR", "actual")
    result = _substitute_placeholders("${SET_VAR:-fallback}", dict(os.environ))
    assert result == "actual"


def test_placeholder_error_form_raises_with_message(monkeypatch):
    monkeypatch.delenv("REQUIRED_VAR", raising=False)
    with pytest.raises(ConfigError, match="must be set"):
        _substitute_placeholders("${REQUIRED_VAR:?must be set}", dict(os.environ))


def test_placeholder_multiple_in_one_string(monkeypatch):
    monkeypatch.setenv("PART_A", "foo")
    monkeypatch.delenv("PART_B", raising=False)
    result = _substitute_placeholders("prefix-${PART_A}-${PART_B:-y}-suffix", dict(os.environ))
    assert result == "prefix-foo-y-suffix"


def test_placeholder_no_nested_expansion(monkeypatch):
    """Nested placeholders are NOT supported; behaviour is single-pass, inner-first.

    Given ``${${X}}``:
    * The regex finds the inner ``${X}`` (a valid var-name pattern) and substitutes it.
    * The resulting outer ``${<value>}`` is **not** re-processed (single pass).
    * Callers should never write ``${${X}}``; this test documents the defined behaviour
      so any future change is a deliberate, visible break.
    """
    monkeypatch.setenv("X", "hello")
    result = _substitute_placeholders("${${X}}", dict(os.environ))
    # Inner ${X} → "hello"; outer ${hello} is left as-is (single-pass, no nested eval).
    assert result == "${hello}"


# ─────────────────────────────────────────────────────────────
# .env file loading tests
# ─────────────────────────────────────────────────────────────

def test_dotenv_loaded_when_present(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    (tmp_path / "config.yaml").write_text("")
    (tmp_path / ".env").write_text("LLM_PROVIDER=openai\n")
    config = load_config(str(tmp_path / "config.yaml"))
    assert config["llm"]["provider"] == "openai"


def test_dotenv_local_precedence_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    (tmp_path / "config.yaml").write_text("")
    (tmp_path / ".env").write_text("LLM_PROVIDER=openai\n")
    (tmp_path / ".env.local").write_text("LLM_PROVIDER=vertex\n")
    config = load_config(str(tmp_path / "config.yaml"))
    assert config["llm"]["provider"] == "vertex"


def test_existing_env_var_not_overwritten_by_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    (tmp_path / "config.yaml").write_text("")
    (tmp_path / ".env").write_text("LLM_PROVIDER=openai\n")
    config = load_config(str(tmp_path / "config.yaml"))
    assert config["llm"]["provider"] == "ollama"


# ─────────────────────────────────────────────────────────────
# Secret validation tests
# ─────────────────────────────────────────────────────────────

def test_literal_secret_in_config_yaml_raises(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "openai:\n"
        '  api_key: "sk-literal"\n'
    )
    with pytest.raises(ConfigError, match="openai.api_key"):
        load_config(str(config_file))


def test_placeholder_secret_in_config_yaml_ok(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "openai:\n"
        "  api_key: ${OPENAI_API_KEY:-}\n"
    )
    config = load_config(str(config_file))
    assert config["openai"]["api_key"] == ""


def test_empty_secret_in_config_yaml_ok(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "openai:\n"
        '  api_key: ""\n'
    )
    config = load_config(str(config_file))
    assert config["openai"]["api_key"] == ""


def test_literal_secret_in_local_yaml_allowed(tmp_path, monkeypatch):
    """config.local.yaml is gitignored; literal secrets there must not raise."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / "config.yaml").write_text("")
    (tmp_path / "config.local.yaml").write_text(
        "openai:\n"
        '  api_key: "sk-real-secret"\n'
    )
    config = load_config(str(tmp_path / "config.yaml"))
    assert config["openai"]["api_key"] == "sk-real-secret"


def test_load_config_returns_dict_not_modifies_defaults(tmp_path):
    """Calling load_config twice must not accumulate state in DEFAULTS."""
    from src.config import DEFAULTS
    (tmp_path / "config.yaml").write_text("")
    before = DEFAULTS["llm"]["provider"]
    load_config(str(tmp_path / "config.yaml"))
    load_config(str(tmp_path / "config.yaml"))
    assert DEFAULTS["llm"]["provider"] == before
