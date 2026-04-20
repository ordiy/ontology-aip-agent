"""Tests for OpenAICompatClient (OpenAI and OpenRouter provider).

All tests mock the openai SDK to avoid real network calls.
"""

import pytest
from unittest.mock import MagicMock, patch


def _make_client(api_key="test-key", model="gpt-4o",
                 base_url="https://api.openai.com/v1",
                 provider_name="OpenAI", extra_headers=None):
    """Helper: create OpenAICompatClient with a mocked openai.OpenAI."""
    with patch("src.llm.openai_compat.OpenAICompatClient.__init__", lambda self, **kw: None):
        from src.llm.openai_compat import OpenAICompatClient
        client = OpenAICompatClient.__new__(OpenAICompatClient)

    mock_openai = MagicMock()
    client._client = mock_openai
    client._model_name = model
    client._provider_name = provider_name
    return client, mock_openai


# ──────────────────────────────────────────────
# get_model_name
# ──────────────────────────────────────────────

def test_get_model_name():
    client, _ = _make_client(model="gpt-4o")
    assert client.get_model_name() == "gpt-4o"


def test_get_model_name_openrouter():
    client, _ = _make_client(model="anthropic/claude-3.5-sonnet", provider_name="OpenRouter")
    assert client.get_model_name() == "anthropic/claude-3.5-sonnet"


# ──────────────────────────────────────────────
# chat — basic call
# ──────────────────────────────────────────────

def _mock_response(text: str):
    """Build a mock OpenAI ChatCompletion response object."""
    choice = MagicMock()
    choice.message.content = text
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def test_chat_returns_content():
    client, mock_openai = _make_client()
    mock_openai.chat.completions.create.return_value = _mock_response("Hello!")

    result = client.chat([{"role": "user", "content": "Hi"}])

    assert result == "Hello!"


def test_chat_sends_system_prompt():
    client, mock_openai = _make_client()
    mock_openai.chat.completions.create.return_value = _mock_response("ok")

    client.chat(
        messages=[{"role": "user", "content": "query"}],
        system_prompt="You are a SQL expert.",
    )

    call_args = mock_openai.chat.completions.create.call_args
    messages = call_args.kwargs["messages"]
    assert messages[0] == {"role": "system", "content": "You are a SQL expert."}
    assert messages[1] == {"role": "user", "content": "query"}


def test_chat_no_system_prompt():
    client, mock_openai = _make_client()
    mock_openai.chat.completions.create.return_value = _mock_response("ok")

    client.chat(messages=[{"role": "user", "content": "hello"}])

    call_args = mock_openai.chat.completions.create.call_args
    messages = call_args.kwargs["messages"]
    # No system message prepended
    assert messages[0]["role"] == "user"


def test_chat_passes_temperature():
    client, mock_openai = _make_client()
    mock_openai.chat.completions.create.return_value = _mock_response("ok")

    client.chat([{"role": "user", "content": "q"}], temperature=0.7)

    call_args = mock_openai.chat.completions.create.call_args
    assert call_args.kwargs["temperature"] == 0.7


# ──────────────────────────────────────────────
# "model" role normalization (Gemini → OpenAI)
# ──────────────────────────────────────────────

def test_chat_normalizes_model_role_to_assistant():
    """nodes.py uses role="model" (Gemini convention); we must convert to "assistant"."""
    client, mock_openai = _make_client()
    mock_openai.chat.completions.create.return_value = _mock_response("ok")

    client.chat([
        {"role": "user", "content": "Previous query"},
        {"role": "model", "content": "Understood."},   # ← Gemini-style role
        {"role": "user", "content": "Current query"},
    ])

    call_args = mock_openai.chat.completions.create.call_args
    messages = call_args.kwargs["messages"]
    roles = [m["role"] for m in messages]
    assert "model" not in roles
    assert roles == ["user", "assistant", "user"]


# ──────────────────────────────────────────────
# Error handling
# ──────────────────────────────────────────────

def test_chat_raises_runtime_error_on_api_failure():
    client, mock_openai = _make_client()
    mock_openai.chat.completions.create.side_effect = Exception("rate limit exceeded")

    with pytest.raises(RuntimeError, match="OpenAI API error"):
        client.chat([{"role": "user", "content": "hi"}])


def test_chat_openrouter_error_includes_provider_name():
    client, mock_openai = _make_client(provider_name="OpenRouter")
    mock_openai.chat.completions.create.side_effect = Exception("401 Unauthorized")

    with pytest.raises(RuntimeError, match="OpenRouter API error"):
        client.chat([{"role": "user", "content": "hi"}])


def test_chat_returns_empty_string_for_none_content():
    """Some models return content=None for refusals — should return empty string, not crash."""
    client, mock_openai = _make_client()
    mock_openai.chat.completions.create.return_value = _mock_response(None)

    result = client.chat([{"role": "user", "content": "hi"}])
    assert result == ""
