from pathlib import Path
import copy
import os
import yaml


DEFAULTS = {
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

ENV_OVERRIDES = {
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


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str = "config.yaml") -> dict:
    config = copy.deepcopy(DEFAULTS)

    path = Path(config_path)
    if path.exists():
        with open(path) as f:
            file_config = yaml.safe_load(f)
            if not isinstance(file_config, dict):
                file_config = {}
        config = _deep_merge(config, file_config)

    # config.local.yaml overrides config.yaml (gitignored, for local secrets)
    local_path = path.parent / (path.stem + ".local.yaml")
    if local_path.exists():
        with open(local_path) as f:
            local_config = yaml.safe_load(f)
            if isinstance(local_config, dict):
                config = _deep_merge(config, local_config)

    for env_var, (section, key) in ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value is not None:
            config[section][key] = value

    return config
