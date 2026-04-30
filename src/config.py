"""Configuration loading for ontology-aip-agent.

Priority chain (high → low):
  1. Environment variables (already in os.environ)
  2. .env.local file (highest .env precedence; gitignored)
  3. .env file (gitignored)
  4. config.local.yaml (gitignored, deep-merged)
  5. config.yaml (git tracked, defaults only — NO secrets)
  6. DEFAULTS dict (fallback)
"""
from __future__ import annotations

import copy
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised for configuration errors such as missing required placeholders
    or literal secret values found in a tracked config file.
    """


DEFAULTS: dict[str, Any] = {
    "llm": {
        "provider": "vertex",
        "model": "gemini-3.1-pro-preview",
        "temperature": 0.0,
    },
    "vertex": {
        "project": "",
        "location": "global",
        "credentials": "",  # Path to service account JSON; empty = use ADC
    },
    "ollama": {
        "host": "http://localhost:11434",
        "model": "llama3",
        "timeout": 120,
    },
    # OpenAI API — https://platform.openai.com/docs/api-reference
    "openai": {
        "api_key": "",    # or set OPENAI_API_KEY env var
        "model": "gpt-4o",
        "base_url": "https://api.openai.com/v1",
    },
    # OpenRouter — 200+ models under one API key: https://openrouter.ai/docs
    "openrouter": {
        "api_key": "",    # or set OPENROUTER_API_KEY env var
        "model": "anthropic/claude-3.5-sonnet",
        "base_url": "https://openrouter.ai/api/v1",
        # Optional: identify your app for OpenRouter analytics / rate-limit tiers
        "site_url": "",
        "app_name": "ontology-aip-agent",
    },
    "database": {
        "path": "./data/",
        "mock_rows_per_table": 100,
    },
    "permissions": {
        "read": "auto",
        "write": "confirm",
        "delete": "confirm",
        "admin": "deny",
    },
}

ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "LLM_PROVIDER": ("llm", "provider"),
    "LLM_MODEL": ("llm", "model"),
    "GOOGLE_CLOUD_PROJECT": ("vertex", "project"),
    "GOOGLE_CLOUD_LOCATION": ("vertex", "location"),
    # Allow credentials path override via env var (useful for Streamlit / Docker)
    "GOOGLE_APPLICATION_CREDENTIALS": ("vertex", "credentials"),
    "OPENAI_API_KEY": ("openai", "api_key"),
    "OPENROUTER_API_KEY": ("openrouter", "api_key"),
    "LANGFUSE_PUBLIC_KEY": ("langfuse", "public_key"),
    "LANGFUSE_SECRET_KEY": ("langfuse", "secret_key"),
    "LANGFUSE_BASE_URL": ("langfuse", "host"),
}

# Fields that must not contain literal (non-placeholder) secrets in config.yaml.
SECRET_FIELDS: tuple[tuple[str, str], ...] = (
    ("vertex", "credentials"),
    ("openai", "api_key"),
    ("openrouter", "api_key"),
    ("langfuse", "secret_key"),
    ("langfuse", "public_key"),
)

# Matches ${VAR}, ${VAR:-default}, ${VAR:?message}.
# Variable names are restricted to [A-Za-z_][A-Za-z0-9_]* so that patterns like
# ${${X}} are never matched and remain literal text (no nested expansion).
# Group layout: (name)(operator)(tail)
#   operator = ':-'  → use tail as default when var is missing or empty
#   operator = ':?'  → raise ConfigError("{name}: {tail}") when var is missing
#   no operator      → required; raise ConfigError if name not in env
_PLACEHOLDER_RE = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:[-?])([^}]*))?\}"
)

# Matches a whole-value placeholder (used to check for literal secrets).
_WHOLE_PLACEHOLDER_RE = re.compile(r"^\s*\$\{.+\}\s*$")


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a copy of *base*.

    Args:
        base: The base dictionary.
        override: Values that take precedence over *base*.

    Returns:
        A new dict with *override* values applied on top of *base*.
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _substitute_placeholders(value: Any, env: dict[str, str]) -> Any:
    """Recursively substitute ``${...}`` placeholders using the given environment.

    Supports three forms:

    * ``${NAME}``          — required; raises :class:`ConfigError` if *NAME* is
      not present in *env*.
    * ``${NAME:-default}`` — uses *default* when *NAME* is missing **or** empty.
    * ``${NAME:?message}`` — raises ``ConfigError("{NAME}: {message}")`` when
      *NAME* is missing or empty.

    Variable names must match ``[A-Za-z_][A-Za-z0-9_]*``.  Substitution is a
    **single pass** (``re.sub``), so nested patterns like ``${${X}}`` are expanded
    inner-first: ``${X}`` is replaced, but the resulting ``${<value>}`` is not
    re-processed.  Do not write nested placeholders.

    A single string may contain multiple placeholders and surrounding text::

        "prefix-${A}-${B:-x}-suffix"

    is fully resolved in one pass via :func:`re.sub`.

    Args:
        value: A dict, list, str, or any scalar value to process.
        env: Mapping of environment variable names to their string values.

    Returns:
        The input with all recognised placeholders substituted.

    Raises:
        ConfigError: When a required placeholder variable is absent.
    """
    if isinstance(value, dict):
        return {k: _substitute_placeholders(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_placeholders(item, env) for item in value]
    if not isinstance(value, str):
        return value

    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        name: str = m.group(1)
        op: str | None = m.group(2)   # ':-', ':?', or None
        tail: str = m.group(3) or ""

        if op == ":-":
            raw = env.get(name)
            return raw if raw else tail
        if op == ":?":
            raw = env.get(name)
            if not raw:
                raise ConfigError(f"{name}: {tail}")
            return raw
        # Plain ${NAME} — required
        if name not in env:
            raise ConfigError(
                f"Missing required placeholder: ${{{name}}}. "
                f"Set the '{name}' environment variable or use ${{name:-default}}."
            )
        return env[name]

    return _PLACEHOLDER_RE.sub(_replace, value)


def _validate_no_literal_secrets(yaml_path: Path, raw_yaml: dict) -> None:
    """Raise :class:`ConfigError` if *config.yaml* contains literal secret values.

    Walks each path in :data:`SECRET_FIELDS` within *raw_yaml*.  A value is
    considered an illegal literal secret when it is a **non-empty string** that
    does **not** look like a placeholder (i.e. does not match ``${...}``).

    Empty strings (``""``) are explicitly allowed as the existing convention for
    "not configured".

    This check applies **only** to the git-tracked ``config.yaml``; it is never
    run against ``config.local.yaml`` (which is gitignored).

    Args:
        yaml_path: Path of the YAML file being validated (used in error messages).
        raw_yaml: The raw parsed dict from *config.yaml* (before any merging).

    Raises:
        ConfigError: When a SECRET_FIELDS path holds a non-empty, non-placeholder
            string value.
    """
    for path in SECRET_FIELDS:
        node: Any = raw_yaml
        for part in path:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node is None:
            continue
        if isinstance(node, str) and node and not _WHOLE_PLACEHOLDER_RE.match(node):
            field_path = ".".join(path)
            raise ConfigError(
                f"{yaml_path}: literal secret found at '{field_path}'. "
                f"Use a placeholder like ${{{field_path.split('.')[-1].upper()}}} "
                f"or leave the value empty (\"\"). "
                f"Store real secrets in config.local.yaml or an environment variable."
            )


def load_config(config_path: str = "config.yaml") -> dict:
    """Load and merge configuration from multiple sources.

    Priority chain (high → low):

    1. Environment variables already present in ``os.environ``
    2. ``.env.local`` (gitignored; loaded via python-dotenv)
    3. ``.env`` (gitignored; loaded via python-dotenv)
    4. ``config.local.yaml`` (gitignored; deep-merged)
    5. ``config.yaml`` (git-tracked; secrets rejected — use placeholders)
    6. :data:`DEFAULTS` dict

    After all sources are merged, ``${...}`` placeholders in string values are
    substituted using the current ``os.environ``.

    Args:
        config_path: Path to the primary config YAML file.  Defaults to
            ``"config.yaml"`` (relative to the current working directory).

    Returns:
        Fully merged and placeholder-substituted configuration dict.

    Raises:
        ConfigError: If a required placeholder is missing from the environment,
            or if *config.yaml* contains a literal secret value.
    """
    config: dict = copy.deepcopy(DEFAULTS)

    path = Path(config_path)
    config_dir = path.parent

    # Step 1: Load config.yaml + validate no literal secrets.
    if path.exists():
        with open(path) as f:
            raw_yaml = yaml.safe_load(f)
        if not isinstance(raw_yaml, dict):
            raw_yaml = {}
        _validate_no_literal_secrets(path, raw_yaml)
        config = _deep_merge(config, raw_yaml)

    # Step 2: Load config.local.yaml (no secret check — it's gitignored).
    local_path = path.parent / (path.stem + ".local.yaml")
    if local_path.exists():
        with open(local_path) as f:
            local_config = yaml.safe_load(f)
        if isinstance(local_config, dict):
            config = _deep_merge(config, local_config)

    # Steps 3a-3b: Load .env files into os.environ.
    # This is the ONLY place in the codebase allowed to call load_dotenv.
    # .env.local is loaded first with override=False so its values enter os.environ
    # before .env; since override=False the second load cannot overwrite the first.
    # Existing os.environ values always win (container-friendly).
    env_local = config_dir / ".env.local"
    env_file = config_dir / ".env"
    if env_local.exists():
        load_dotenv(env_local, override=False)
        logger.debug("Loaded env from %s", env_local)
    if env_file.exists():
        load_dotenv(env_file, override=False)
        logger.debug("Loaded env from %s", env_file)

    # Step 4: Apply ENV_OVERRIDES (backward compat — explicit env-var→config mappings).
    for env_var, (section, key) in ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value is not None:
            if section not in config:
                config[section] = {}
            config[section][key] = value

    # Step 5: Substitute ${...} placeholders recursively.
    config = _substitute_placeholders(config, dict(os.environ))

    return config
