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
    },
    "ollama": {
        "host": "http://localhost:11434",
        "model": "llama3",
        "timeout": 120,
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

    for env_var, (section, key) in ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value is not None:
            config[section][key] = value

    return config
